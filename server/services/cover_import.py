# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""把外部脑图 / 用例文本导入当前需求。"""
from __future__ import annotations

import csv
import io
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

from server.services.qa_role_jobs import (
    apply_cases,
    apply_mindmap,
    collect_mindmap_points,
)

# 看起来是一句测试点、而不是一个节点名。图谱节点名是短名词（「定制模版页」），测试点是
# 一句能判定的话（「超过 10MB 时提示图片过大」）。带子节点也一样 —— 人写脑图常把一个点
# 再拆几个子情况出来，那整棵都是覆盖，不是产品结构。
_POINT_MAX_NAME = 14
_SENTENCE_MARKS = "。，；？！,;?!"

_CASE_HEADERS = {
    "case_id": ("用例编号", "编号", "id", "case_id", "caseid"),
    "name": ("名称", "标题", "用例名", "name", "title"),
    "module": ("模块", "路径", "module", "path"),
    "precondition": ("前置", "前置条件", "precondition", "pre"),
    "steps": ("步骤", "测试步骤", "操作步骤", "steps"),
    "expected": ("预期", "期望", "预期结果", "预期效果", "期望结果", "expected"),
    "platform": ("端", "平台", "platform"),
    "aspect": ("情况", "类型", "aspect", "kind"),
    "point_ids": ("测试点", "point_ids", "points"),
}


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _nid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _looks_like_point(text: str) -> bool:
    t = str(text or "").strip()
    return len(t) > _POINT_MAX_NAME or any(ch in t for ch in _SENTENCE_MARKS)


@dataclass(frozen=True)
class Hints:
    """定层级时能参考的东西。都可以缺 —— 缺了就退回纯 depth 推断，不报错。"""

    profile: Any = None   # app_profile.UiProfile，提供端的枚举和别名
    aligner: Any = None   # atlas_align.Aligner，提供「图谱里已经有这个名字」

    def surface(self, text: str) -> str:
        return self.profile.surface_of(text, loose=False) if self.profile is not None else ""

    def atlas_kind(self, text: str) -> str:
        if self.aligner is None:
            return ""
        hit = self.aligner.match(text)
        return hit.kind if (hit.certain and hit.kind in ("module", "feature")) else ""


_NO_HINTS = Hints()


def _kind_for(text: str, depth: int, has_kids: bool, hints: Hints = _NO_HINTS) -> str:
    """给一个导入节点定层级。

    端的枚举来自应用画像，不写死关键词 —— 换个有小程序或桌面端的应用，写死的那几个词
    会把那部分覆盖判成模块。层级则优先采信图谱：图谱里已经有这个名字，就按图谱那边的
    层级来，别让导进来的脑图和图谱各长一套。
    """
    if depth <= 1 and hints.surface(text):
        return "platform"
    if not has_kids:
        return "point"
    if _looks_like_point(text):
        return "point"
    return hints.atlas_kind(text) or ("module" if depth <= 2 else "feature")


def _node(text: str, *, kind: str, kids: list | None = None, **extra) -> dict:
    row = {
        "id": extra.get("id") or _nid("n"),
        "text": str(text or "").strip()[:80],
        "kind": kind,
        "children": kids or [],
    }
    if extra.get("detail"):
        row["detail"] = str(extra["detail"])
    if extra.get("platform"):
        row["platform"] = extra["platform"]
    if extra.get("point_id"):
        row["point_id"] = extra["point_id"]
    return row


def _retag(node: dict, depth: int = 0, hints: Hints = _NO_HINTS) -> dict:
    kids = [_retag(c, depth + 1, hints) for c in (node.get("children") or []) if isinstance(c, dict)]
    text = str(node.get("text") or node.get("title") or "").strip()
    incoming = str(node.get("kind") or "")
    if depth == 0:
        kind = "root"
    elif incoming in ("platform", "module", "feature") or (incoming == "point" and not kids):
        kind = incoming
    else:
        kind = _kind_for(text, depth, bool(kids), hints)
    out = dict(node)
    out["text"] = text or out.get("text") or "导入脑图"
    out["kind"] = kind
    out["children"] = kids
    out.setdefault("id", _nid("n"))
    return out


def _from_json_tree(data: Any, hints: Hints = _NO_HINTS) -> dict:
    if isinstance(data, list):
        kids = [_from_json_tree(x, hints) for x in data if isinstance(x, dict)]
        return _retag(_node("导入脑图", kind="root", kids=kids), 0, hints)
    if not isinstance(data, dict):
        raise ValueError("JSON 脑图需要对象或数组")
    if isinstance(data.get("mindmap"), dict):
        return _from_json_tree(data["mindmap"], hints)
    kids_raw = data.get("children") or data.get("topics") or data.get("nodes") or []
    kids = [_from_json_tree(x, hints) for x in kids_raw if isinstance(x, dict)]
    text = data.get("text") or data.get("title") or data.get("name") or data.get("topic") or "导入脑图"
    node = _node(str(text), kind=str(data.get("kind") or "module"), kids=kids, detail=data.get("detail") or "")
    if data.get("id"):
        node["id"] = str(data["id"])
    depth = 0 if str(data.get("kind") or "") in ("", "root") or not kids_raw else 1
    return _retag(node, depth, hints)


def _from_opml(text: str, hints: Hints = _NO_HINTS) -> dict:
    root_xml = ET.fromstring(text)
    outlines = []
    for body in root_xml.iter():
        if body.tag.lower().endswith("body"):
            outlines = [c for c in list(body) if str(c.tag).lower().endswith("outline")]
            break

    def walk(el, depth: int) -> dict:
        title = el.attrib.get("text") or el.attrib.get("title") or ""
        kids = [walk(c, depth + 1) for c in list(el) if str(c.tag).lower().endswith("outline")]
        return _node(title, kind=_kind_for(title, depth, bool(kids), hints), kids=kids)

    kids = [walk(el, 1) for el in outlines]
    title = "导入脑图"
    for el in root_xml.iter():
        if str(el.tag).lower().endswith("title") and (el.text or "").strip():
            title = el.text.strip()
            break
    return _retag(_node(title, kind="root", kids=kids), 0, hints)


def _outline_rows(text: str) -> list[tuple[int, str]]:
    rows = []
    last_heading = 0
    for raw in str(text or "").splitlines():
        if not raw.strip():
            continue
        heading = re.match(r"^(#{1,6})\s+(.*)$", raw.strip())
        if heading:
            last_heading = len(heading.group(1)) - 1
            rows.append((last_heading, heading.group(2).strip()))
            continue
        indent = 0
        i = 0
        while i < len(raw) and raw[i] in " \t":
            indent += 4 if raw[i] == "\t" else 1
            i += 1
        body = raw.strip()
        is_item = bool(re.match(r"^[-*+]\s+", body) or re.match(r"^\d+[.)、]\s+", body))
        body = re.sub(r"^[-*+]\s+", "", body)
        body = re.sub(r"^\d+[.)、]\s+", "", body)
        if not body:
            continue
        level = indent // 2
        if is_item:
            level = last_heading + 1 + (indent // 2)
        rows.append((level, body))
    return rows


def _from_outline(text: str, hints: Hints = _NO_HINTS) -> dict:
    rows = _outline_rows(text)
    if not rows:
        raise ValueError("没有解析到脑图节点")
    root = _node(rows[0][1] if rows[0][0] == 0 else "导入脑图", kind="root", kids=[])
    stack = [( -1, root)]
    start = 1 if rows[0][0] == 0 else 0
    for level, title in rows[start:]:
        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1]
        node = _node(title, kind="point", kids=[])
        parent.setdefault("children", []).append(node)
        stack.append((level, node))
    return _retag(root, 0, hints)


def parse_mindmap(text: str, filename: str = "", hints: Hints = _NO_HINTS) -> dict:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("脑图内容是空的")
    name = str(filename or "").lower()
    if name.endswith(".opml") or raw[:200].lower().find("<opml") >= 0 or raw.startswith("<?xml"):
        try:
            return _from_opml(raw, hints)
        except Exception as e:
            raise ValueError(f"OPML 解析失败：{e}") from e
    if raw.startswith("{") or raw.startswith("["):
        try:
            return _from_json_tree(json.loads(raw), hints)
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"JSON 脑图解析失败：{e}") from e
    return _from_outline(raw, hints)


def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", "", str(cell or "").strip().lower())


def _header_map(row: list[str]) -> dict[str, int]:
    mapping = {}
    for idx, cell in enumerate(row):
        key = _norm_header(cell)
        for field, aliases in _CASE_HEADERS.items():
            if key in {_norm_header(a) for a in aliases} and field not in mapping:
                mapping[field] = idx
    return mapping


_XML_CTRL = re.compile(r"_x([0-9A-Fa-f]{4})_", re.I)
_NUM_ITEM = re.compile(r"\d+[.、．)）]\s+")


def _normalize_cell_text(val: Any) -> str:
    """单元格原文。Numbers / Excel 里 Alt+Enter 可能是 \\n、\\r 或 _x000D_。"""
    s = str(val if val is not None else "")
    s = _XML_CTRL.sub(lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\u2028", "\n").replace("\u2029", "\n")
    return s.strip()


def _restore_numbered_breaks(text: str) -> str:
    """CSV / 展示层常把「1. … 2. …」压成一行。有两个以上序号就拆回换行。"""
    s = _normalize_cell_text(text)
    if not s or "\n" in s:
        return s
    out = re.sub(r"\s*(?=\d+[.、．)）]\s+)", "\n", s).strip()
    lines = [ln.strip() for ln in out.split("\n") if ln.strip()]
    if len(lines) <= 1:
        return s
    numbered = sum(1 for ln in lines if _NUM_ITEM.match(ln))
    if numbered < 2:
        return s
    return "\n".join(lines)


def _cell(row: list, idx: int | None) -> str:
    if idx is None or idx >= len(row):
        return ""
    return _restore_numbered_breaks(_normalize_cell_text(row[idx]))


def _csv_dialect(text: str):
    first = ""
    for line in text.splitlines():
        if line.strip():
            first = line
            break
    tabs = first.count("\t")
    commas = first.count(",")
    # 表头带 tab 就按 TSV 读。Sniffer 碰到单元格内换行容易把分隔符认错。
    if tabs >= 1 and tabs >= commas:
        return csv.excel_tab
    try:
        return csv.Sniffer().sniff(text[:800], delimiters=",\t;|")
    except Exception:
        return csv.excel_tab if tabs else csv.excel


def _split_csv(text: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text), _csv_dialect(text))
    rows = []
    for row in reader:
        cells = [_normalize_cell_text(c) for c in row]
        if any(cells):
            rows.append(cells)
    return rows


def _md_table(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and re.match(r"^:?-{3,}:?$", cells[0] or ""):
            continue
        if all(re.match(r"^:?-+:?$", c or "") for c in cells):
            continue
        rows.append(cells)
    return rows


def _from_case_rows(rows: list[list[str]]) -> list[dict]:
    if not rows:
        return []
    header_i = 0
    mapping = _header_map(rows[0])
    if mapping:
        header_i = 0
        body = rows[1:]
    else:
        mapping = {"name": 0, "steps": 1, "expected": 2}
        body = rows
    out = []
    for i, row in enumerate(body):
        name = _cell(row, mapping.get("name"))
        steps = _cell(row, mapping.get("steps"))
        expected = _cell(row, mapping.get("expected"))
        cid = _cell(row, mapping.get("case_id"))
        if not (name or steps or cid):
            continue
        pre = _cell(row, mapping.get("precondition"))
        point_raw = _cell(row, mapping.get("point_ids"))
        points = [p.strip() for p in re.split(r"[,，;；\s]+", point_raw) if p.strip()] if point_raw else []
        out.append(
            {
                "case_id": cid or f"imp-{i + 1}",
                "name": name or cid or f"导入用例 {i + 1}",
                "module": _cell(row, mapping.get("module")),
                "precondition": pre,
                "steps": steps,
                "expected": expected,
                "precondition_raw": pre,
                "steps_raw": steps,
                "expected_raw": expected,
                "platform": _cell(row, mapping.get("platform")) or "双端",
                "aspect": _cell(row, mapping.get("aspect")) or "正向",
                "point_ids": points,
            }
        )
    return out


def _from_case_blocks(text: str) -> list[dict]:
    chunks = re.split(r"\n(?=#{1,3}\s+|\d+[.)、]\s+)", text.strip())
    out = []
    for i, chunk in enumerate(chunks):
        lines = [ln.rstrip() for ln in chunk.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        title = re.sub(r"^#{1,3}\s+", "", lines[0])
        title = re.sub(r"^\d+[.)、]\s+", "", title).strip()
        fields = {"name": title}
        key = ""
        buf = []

        def flush():
            if key:
                fields[key] = "\n".join(buf).strip()

        for line in lines[1:]:
            m = re.match(r"^(编号|名称|模块|前置|前置条件|步骤|测试步骤|预期|期望|预期效果|期望结果|端|情况)[:：]\s*(.*)$", line)
            if m:
                flush()
                label = m.group(1)
                key = {
                    "编号": "case_id",
                    "名称": "name",
                    "模块": "module",
                    "前置": "precondition",
                    "前置条件": "precondition",
                    "步骤": "steps",
                    "测试步骤": "steps",
                    "预期": "expected",
                    "期望": "expected",
                    "预期效果": "expected",
                    "期望结果": "expected",
                    "端": "platform",
                    "情况": "aspect",
                }[label]
                buf = [m.group(2)] if m.group(2) else []
            else:
                buf.append(line)
        flush()
        if fields.get("name") or fields.get("steps"):
            fields.setdefault("case_id", f"imp-{i + 1}")
            out.append(fields)
    return out


def _polish_case(row: dict) -> dict:
    out = dict(row)
    for key in ("precondition", "steps", "expected"):
        if key in out:
            out[key] = _restore_numbered_breaks(_normalize_cell_text(out.get(key)))
    return out


def parse_cases(text: str, filename: str = "") -> list[dict]:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("用例内容是空的")
    name = str(filename or "").lower()
    if raw.startswith("{") or raw.startswith("["):
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("table"), list):
            grid = [
                [_normalize_cell_text(c) for c in row]
                for row in data["table"]
                if isinstance(row, list)
            ]
            return [_polish_case(x) for x in _from_case_rows(grid)]
        if isinstance(data, dict):
            rows = data.get("cases") or data.get("draft_cases") or []
        else:
            rows = data
        if not isinstance(rows, list):
            raise ValueError("JSON 用例需要数组")
        return [_polish_case(x) for x in rows if isinstance(x, dict)]
    if "|" in raw and re.search(r"^\|.+\|", raw, re.M):
        rows = _from_case_rows(_md_table(raw))
        if rows:
            return [_polish_case(x) for x in rows]
    if name.endswith((".csv", ".tsv")) or "\t" in raw or raw.count(",") >= 2:
        rows = _from_case_rows(_split_csv(raw))
        if rows:
            return [_polish_case(x) for x in rows]
    blocks = _from_case_blocks(raw)
    if blocks:
        return [_polish_case(x) for x in blocks]
    raise ValueError("无法识别用例格式。可用 Excel（请用「选择文件」上传 .xlsx）/ CSV / Markdown 表 / JSON，或「标题 + 步骤：/预期：」分段")


def _stamp(req: dict, *, job: str, summary: str, payload: dict) -> dict:
    hist = [x for x in (req.get("cover_history") or []) if isinstance(x, dict)]
    hist.append(
        {
            "id": f"imp-{uuid.uuid4().hex[:10]}",
            "at": _now(),
            "job": job,
            "kind": "import",
            "note": "外部导入",
            "engine": "import",
            "suggest": summary,
            "summary": summary,
            "payload": payload,
        }
    )
    next_req = dict(req)
    next_req["cover_history"] = hist[-24:]
    return next_req


def _import_mindmap(
    doc: dict, req: dict, text: str, filename: str, package: str, *, app_id: str = ""
) -> tuple[dict, dict, dict | None]:
    """导入脑图并反推图谱。返回 (需求, 统计, 可直接合并的图谱或 None)。

    图谱怎么落地按确定性分两条路：全是精确/别名命中就直接合并（人已经确认过这些节点了，
    再拦一道只是徒增点击）；只要有新建节点或模糊对齐，就入 `atlas_patches` 等人审 ——
    凭一份外部脑图往图谱里加分支，必须有人点头。
    """
    from server.services.ai import app_atlas as atlas
    from server.services.ai import atlas_align as align
    from server.services.ai import app_profile as ap
    from server.services.ai import atlas_from_mindmap as afm

    current = atlas.normalize_atlas(doc.get("app_atlas"))
    aligner = align.aligner_for(current, package=package, app_id=app_id)
    hints = Hints(profile=ap.current(package), aligner=aligner)
    tree = parse_mindmap(text, filename, hints)

    rid = str(req.get("id") or "")
    out = afm.infer(
        current,
        tree,
        req_id=rid,
        package=package,
        aliases=aligner.aliases,
        rejected=aligner.rejected,
    )
    # 别名命中累加 hits，管理页按热度排序才有意义
    if app_id and out.matched:
        try:
            from server.services.ai import atlas_alias_repo as alias_repo
            from server.services.ai.atlas_align import norm_name

            for row in out.matched:
                if str(row.get("how") or "") == "alias":
                    alias_repo.record_hit(app_id, norm_name(row.get("text") or ""))
        except Exception:
            pass
    req = apply_mindmap(req, out.mindmap)
    points = len(collect_mindmap_points(req.get("mindmap")))

    stats = {
        "kind": "mindmap",
        "points": points,
        "nodes": out.nodes,
        "matched": len(out.matched),
        "created": len(out.created),
        "review": len(out.review),
        "platforms": out.platforms,
        "atlas": "patch" if out.needs_review else ("merged" if out.matched else "unchanged"),
    }
    summary = f"导入脑图 {points} 个测试点（{afm.summary_line(out)}）"
    req = _stamp(req, job="draft_mindmap", summary=summary, payload=tree)

    if not atlas.atlas_has_nodes(out.atlas):
        return req, stats, None
    if not out.needs_review:
        return req, stats, out.atlas

    alias_rows = afm.alias_suggestions(out)
    patches = [x for x in (doc.get("atlas_patches") or []) if isinstance(x, dict)]
    patch = atlas.enqueue_patch(
        patches,
        before=current,
        after=out.atlas,
        reason=f"外部脑图「{str(req.get('title') or '需求')}」反推：{afm.summary_line(out)}",
        source={"kind": "mindmap_import", "req_id": rid, "filename": filename},
        reqs=[r for r in (doc.get("requirements") or []) if isinstance(r, dict)],
        aliases=alias_rows,
        force=bool(alias_rows),
    )
    doc["atlas_patches"] = patches[-20:]
    stats["patch_id"] = (patch or {}).get("id") or ""
    stats["aliases"] = len(alias_rows)
    if not patch:
        # 同样的建议已经在队列里躺着了，别再堆一条
        stats["atlas"] = "pending"
    return req, stats, None


def import_cover(
    *,
    qa_process: dict,
    kind: str,
    requirement_id: str,
    text: str,
    filename: str = "",
    replace: bool = True,
    package: str = "",
    app_id: str = "",
) -> dict:
    doc = dict(qa_process or {})
    reqs = [dict(r) for r in (doc.get("requirements") or []) if isinstance(r, dict)]
    rid = str(requirement_id or "").strip()
    idx = next((i for i, r in enumerate(reqs) if str(r.get("id") or "") == rid), -1)
    if idx < 0:
        raise ValueError("请先选一条需求再导入")
    req = reqs[idx]
    k = str(kind or "").strip()
    merged_atlas = None
    if k in ("mindmap", "mind"):
        req, stats, merged_atlas = _import_mindmap(doc, req, text, filename, package, app_id=app_id)
    elif k in ("cases", "case"):
        incoming = parse_cases(text, filename)
        if not incoming:
            raise ValueError("没有解析到用例")
        # 导入的用例既不是模型写的也不是模板桩：标 import 并锁定，重试不许覆盖人导进来的东西。
        incoming = [{**c, "origin": "import", "locked": True} for c in incoming if isinstance(c, dict)]
        existing = [x for x in (req.get("draft_cases") or []) if isinstance(x, dict)]
        rows = incoming if replace else existing + incoming
        req = apply_cases(req, {"cases": rows}, replace=True)
        req = _stamp(
            req,
            job="draft_cases",
            summary=f"导入用例 {len(incoming)} 条" + ("（覆盖原草稿）" if replace else "（追加）"),
            payload={"cases": incoming},
        )
        stats = {"kind": "cases", "cases": len(incoming), "total": len(req.get("draft_cases") or [])}
    else:
        raise ValueError("kind 只能是 mindmap 或 cases")
    reqs[idx] = req
    doc["requirements"] = reqs
    if merged_atlas is not None:
        # 全是确定命中才走到这。挂载关系要回写到需求上（module_ids / atlas_paths），
        # 顺序上必须等 requirements 更新完，否则回写的是旧那份。
        from server.services.ai import app_atlas as atlas

        doc["app_atlas"] = merged_atlas
        doc["requirements"] = atlas.apply_hangs_to_reqs(doc["requirements"], merged_atlas)
        doc["features"] = atlas.flatten_features(merged_atlas, doc["requirements"])
        req = next((r for r in doc["requirements"] if str(r.get("id") or "") == rid), req)
    doc["updated_at"] = _now()
    return {"qa_process": doc, "requirement": req, **stats}
