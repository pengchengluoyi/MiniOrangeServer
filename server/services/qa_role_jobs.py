# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""产品角色 Job：需求分析 / 测试脑图 / 用例编写，以及流程 tick。

tick 会改 qa_process 草稿字段，不改 human_verdict，不自动下发真机任务。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from server.services.ai.roles_catalog import (
    CASE_WRITER_SYSTEM_PROMPT,
    MINDMAP_WRITER_SYSTEM_PROMPT,
    REQ_ANALYST_IMPACT_PROMPT,
    REQ_ANALYST_SYSTEM_PROMPT,
)
from server.services.ai import app_atlas as atlas
from server.services.ai import dispatch_log as dispatch
from server.services.qa_process_assist import artifact

LLM_JOBS = ("analyze_req", "draft_mindmap", "draft_cases", "propose_atlas")

DEFAULT_JOBS_BY_KIND = {
    "understand": ("analyze_req", "propose_atlas"),
    "cover": ("draft_mindmap", "draft_cases"),
    "scope": (),
    "dispatch": (),
    "human_verdict": (),
    "archive": (),
    "checkpoint": (),
}


def _workflow_steps(doc: dict, track: str) -> list:
    wf = doc.get("workflow") if isinstance(doc.get("workflow"), dict) else {}
    tracks = wf.get("tracks") if isinstance(wf.get("tracks"), dict) else {}
    row = tracks.get(track) if isinstance(tracks.get(track), dict) else {}
    return [s for s in (row.get("steps") or []) if isinstance(s, dict)]


def _jobs_for_step(doc: dict, track: str, gate: str, kind: str = "") -> set[str]:
    steps = _workflow_steps(doc, track)
    step = next((s for s in steps if str(s.get("id") or "") == str(gate or "")), None)
    kind = str((step or {}).get("kind") or kind or "")
    if not kind:
        if str(gate or "") in ("read", ""):
            kind = "understand"
        elif str(gate or "") == "cases":
            kind = "cover"
    jobs: list[str] = []
    wf = doc.get("workflow") if isinstance(doc.get("workflow"), dict) else {}
    wid = str((step or {}).get("workflow_id") or "").strip()
    if wid:
        for chain in wf.get("chains") or []:
            if not isinstance(chain, dict) or str(chain.get("id") or "") != wid:
                continue
            for item in chain.get("steps") or []:
                if isinstance(item, str):
                    jobs.append(item)
                elif isinstance(item, dict):
                    jobs.append(str(item.get("capability_id") or item.get("id") or item.get("job") or ""))
            break
    if not jobs:
        for item in (step or {}).get("jobs") or []:
            if isinstance(item, str):
                jobs.append(item)
            elif isinstance(item, dict):
                jobs.append(str(item.get("capability_id") or item.get("id") or item.get("job") or ""))
    if not jobs:
        jobs = list(DEFAULT_JOBS_BY_KIND.get(kind) or [])
    return {x for x in jobs if x}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hash(text: str) -> str:
    h = 5381
    for ch in str(text or ""):
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    return format(h, "x")


def _source_text(req: dict) -> str:
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    bits = [
        req.get("title") or "",
        req.get("external_id") or "",
        req.get("source_text") or "",
        und.get("source_excerpt") or "",
        "\n".join(str(x) for x in (und.get("ac") or []) if x),
    ]
    seen = set()
    out = []
    for x in bits:
        s = str(x).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return "\n".join(out)


def _analysis_bundle(req: dict) -> dict:
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    return {
        "source_excerpt": und.get("source_excerpt") or req.get("source_text") or "",
        "summary": req.get("summary") or "",
        "change_kind": und.get("change_kind") or "",
        "baseline": und.get("baseline") or "",
        "delta": und.get("delta") or "",
        "ac": und.get("ac") or [],
        "points": und.get("points") or [],
        "impact": und.get("impact") or {},
        "journeys": und.get("journeys") or [],
        "new_features": und.get("new_features") or [],
        "keep_features": und.get("keep_features") or [],
        "exceptions": und.get("exceptions") or [],
        "surfaces": und.get("surfaces") or [],
        "analyst_feedback": str(req.get("analyst_feedback") or "").strip(),
    }


def _ask_json(system: str, user: str, *, max_tokens: int = 2500, timeout_sec: int = 90, role: str = "", job: str = "") -> tuple[Optional[dict], dict]:
    from server.services.ai.regression.llm_client import call_chat_text, resolve_regression_provider

    provider, gate = resolve_regression_provider()
    tok = dispatch.bind(role=role, job=job)
    if not provider:
        dispatch.record_job(status="skipped", job=job or "llm", role=role, detail=gate.get("reason") or "未配置模型")
        dispatch.reset(tok)
        return None, {"error": gate.get("reason") or "未配置「可用 + 用例」模型", "engine": "none"}
    parsed, meta = call_chat_text(
        provider=provider,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.2,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
    )
    dispatch.reset(tok)
    if not isinstance(parsed, dict):
        return None, {**meta, "error": meta.get("error") or "模型没有返回 JSON", "engine": "llm"}
    from server.services.ai.regression.llm_client import parse_token_usage

    usage = parse_token_usage(meta.get("usage"))
    return parsed, {**meta, "engine": "llm", **usage}


def _with_engine(art: dict, engine: str, meta: dict | None = None) -> dict:
    out = dict(art)
    out["engine"] = engine
    if meta:
        out["usage"] = {
            "prompt_tokens": int(meta.get("prompt_tokens") or 0),
            "completion_tokens": int(meta.get("completion_tokens") or 0),
            "total_tokens": int(meta.get("total_tokens") or 0),
        }
    return out


def _as_dict_list(val) -> list:
    return [x for x in (val or []) if isinstance(x, dict)]


def _rule_analyze(req: dict) -> dict:
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    text = _source_text(req)
    ac = [str(x).strip() for x in (und.get("ac") or []) if str(x).strip()]
    points = und.get("points") or []
    if not ac:
        title = str(req.get("title") or "需求").strip()
        ac = [f"{title} 主流程可完成"]
    if not points:
        points = [{"id": "tp1", "kind": "正向", "text": ac[0], "case_ids": [], "waived": False}]
    platforms = list((und.get("impact") or {}).get("platforms") or [])
    if _looks_web(text) and "web" not in platforms:
        platforms.append("web")
    if _looks_app(text) and "app" not in platforms:
        platforms.append("app")
    impact = und.get("impact") if isinstance(und.get("impact"), dict) else {}
    return {
        "summary": str(req.get("title") or ""),
        "ac": ac,
        "features": [{"name": str(req.get("title") or "功能"), "notes": ""}],
        "points": [
            {
                "id": p.get("id") or f"tp{i + 1}",
                "kind": p.get("kind") or "正向",
                "text": p.get("text") or "",
            }
            for i, p in enumerate(points)
            if isinstance(p, dict)
        ],
        "risks": list(und.get("stale_risks") or []),
        "impact": {
            **impact,
            "platforms": platforms or impact.get("platforms") or [],
        },
        "change_kind": und.get("change_kind") or "",
        "baseline": und.get("baseline") or "",
        "delta": und.get("delta") or "",
        "journeys": und.get("journeys") or [],
        "new_features": und.get("new_features") or [],
        "keep_features": und.get("keep_features") or [],
        "exceptions": und.get("exceptions") or [],
        "surfaces": und.get("surfaces") or [],
        "hang": {"paths": [], "module_names": [], "feature_names": [str(req.get("title") or "").strip()]},
        "atlas_create": [],
    }


def _looks_web(text: str) -> bool:
    return bool(re.search(r"web|h5|后台|管理端|网页|运营平台|运营后台|cms", str(text or ""), re.I))


def _looks_app(text: str) -> bool:
    return bool(re.search(r"app|移动端|客户端|ios|android|安卓", str(text or ""), re.I))


def analyze_req(req: dict, cases: list | None = None, atlas_doc: dict | None = None, *, user_note: str = "") -> dict:
    text = _source_text(req)
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    user = json.dumps(
        {
            "title": req.get("title"),
            "external_id": req.get("external_id"),
            "source": text,
            "app_atlas": atlas.compact_atlas(atlas_doc),
            "human_feedback": str(user_note or req.get("analyst_feedback") or "").strip(),
            "previous_analysis": {
                "summary": req.get("summary") or "",
                "change_kind": und.get("change_kind") or "",
                "baseline": und.get("baseline") or "",
                "delta": und.get("delta") or "",
                "impact": und.get("impact") or {},
            },
            "existing_cases": [
                {
                    "case_id": c.get("case_id"),
                    "name": c.get("name") or c.get("title"),
                    "module": c.get("module"),
                }
                for c in (cases or [])[:40]
            ],
            "note": "先对照 app_atlas。必须挖入口（不要默认首页）、新增 vs 维持、上传异常兜底、运营平台/Web。platforms 用 app/web/e2e。human_feedback 必须逐条落实。",
        },
        ensure_ascii=False,
    )
    parsed, meta = _ask_json(
        REQ_ANALYST_SYSTEM_PROMPT,
        user,
        max_tokens=8192,
        timeout_sec=180,
        role="req-analyst",
        job="analyze_req",
    ) if text else (None, {"error": "没有需求原文"})
    payload = parsed if parsed else _rule_analyze(req)
    engine = "llm" if parsed else "rule"
    suggest = "已拆验收标准和测试点" if engine == "llm" else f"规则拆点（{meta.get('error') or '无模型'}）"
    return _with_engine(
        artifact(
            job="analyze_req",
            suggest=suggest,
            citations=[req.get("id") or ""],
            payload=payload,
            input_hash=_hash(text + "\n" + str(user_note or "")),
        ),
        engine,
        meta,
    )


def draft_mindmap(req: dict, cases: list | None = None, atlas_doc: dict | None = None, *, user_note: str = "") -> dict:
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    intent = req.get("atlas_intent") if isinstance(req.get("atlas_intent"), dict) else {}
    hang = intent.get("hang") if isinstance(intent.get("hang"), dict) else {}
    atlas_paths = atlas.paths_for_req(atlas_doc, req.get("id") or "") or hang.get("paths") or []
    bundle = _analysis_bundle(req)
    note = str(user_note or req.get("analyst_feedback") or "").strip()
    prev = req.get("mindmap") if isinstance(req.get("mindmap"), dict) else {}
    user = json.dumps(
        {
            "title": req.get("title"),
            **bundle,
            "atlas_paths": atlas_paths,
            "app_atlas": atlas.compact_atlas(atlas_doc),
            "features": req.get("features") or und.get("features") or [],
            "previous_mindmap": prev if prev.get("children") else {},
            "retry_note": note,
            "human_feedback": note,
            "note": "必须详尽。第一层按端拆，运营平台走 Web。入口跟 journeys，不要默认首页。new_features 加厚，keep_features 回归，exceptions 全部落点。",
        },
        ensure_ascii=False,
    )
    parsed, meta = _ask_json(
        MINDMAP_WRITER_SYSTEM_PROMPT,
        user,
        max_tokens=8192,
        timeout_sec=180,
        role="mindmap-writer",
        job="draft_mindmap",
    )
    if not parsed:
        grouped = {}
        for i, p in enumerate(und.get("points") or []):
            if not isinstance(p, dict):
                continue
            path = p.get("path") or (atlas_paths[min(i, len(atlas_paths) - 1)] if atlas_paths else ["测试点"])
            key = tuple(path) if isinstance(path, list) else tuple(split_label(path))
            grouped.setdefault(key or ("测试点",), []).append(p)
        children = _tree_from_groups(grouped) or [{
            "id": "n1",
            "text": "测试点",
            "kind": "feature",
            "path": ["测试点"],
            "children": [
                {
                    "id": f"n1-{j + 1}",
                    "text": _short_title(p.get("text") or f"点{j + 1}"),
                    "kind": "point",
                    "point_id": p.get("id") or "",
                    "case_ids": list(p.get("case_ids") or []),
                    "children": [],
                }
                for j, p in enumerate(p for p in (und.get("points") or []) if isinstance(p, dict))
            ],
        }]
        parsed = {"title": _short_title(req.get("title") or "需求", 10), "children": children}
    engine = "llm" if meta.get("engine") == "llm" and parsed.get("children") else "rule"
    return _with_engine(
        artifact(
            job="draft_mindmap",
            suggest="已生成测试脑图草稿",
            citations=[req.get("id") or ""],
            payload=parsed,
            input_hash=_hash(user),
        ),
        engine,
        meta,
    )


def split_label(label: str) -> list:
    return [p.strip() for p in str(label or "").replace(">", "/").split("/") if p.strip()]


def _case_text(val) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        lines = []
        n = 0
        for item in val:
            if isinstance(item, dict):
                t = str(item.get("text") or item.get("step") or item.get("expected") or item.get("action") or "").strip()
            else:
                t = str(item).strip()
            if not t:
                continue
            n += 1
            if t[0] not in "0123456789":
                t = f"{n}. {t}"
            lines.append(t)
        return "\n".join(lines)
    return str(val).strip()


def _normalize_draft_case(row: dict, index: int) -> dict:
    out = dict(row)
    steps = _case_text(out.get("steps") or out.get("step") or out.get("test_steps"))
    expected = _case_text(out.get("expected") or out.get("expect") or out.get("expected_result"))
    if not steps:
        steps = f"1. 打开应用\n2. 覆盖「{out.get('name') or '该测试点'}」"
    if not expected:
        expected = "1. 达到该测试点描述的结果"
    out["case_id"] = out.get("case_id") or f"draft-{index + 1}"
    out["name"] = out.get("name") or out.get("title") or out["case_id"]
    out["steps"] = steps
    out["expected"] = expected
    out["precondition"] = _case_text(out.get("precondition") or out.get("pre")) or "账号可用，应用可启动"
    out["platform"] = out.get("platform") or "双端"
    out["aspect"] = str(out.get("aspect") or out.get("kind") or "正向").strip() or "正向"
    return out


def _short_title(text: str, max_len: int = 40) -> str:
    s = " ".join(str(text or "").split())
    if not s:
        return ""
    return s if len(s) <= max_len else s[:max_len]


def _clip_mindmap(node: dict) -> dict:
    out = dict(node)
    title = out.get("text") or out.get("title") or ""
    kind = out.get("kind") or ""
    cap = 40 if kind == "point" else 20
    if title:
        clipped = _short_title(title, cap)
        if "text" in out or not out.get("title"):
            out["text"] = clipped
        if out.get("title"):
            out["title"] = _short_title(out.get("title") or "", 20)
    kids = out.get("children")
    if isinstance(kids, list):
        out["children"] = [_clip_mindmap(c) for c in kids if isinstance(c, dict)]
    return out


def _tree_from_groups(grouped: dict) -> list:
    root_kids: list[dict] = []

    def ensure(path: tuple, pts: list) -> None:
        kids = root_kids
        acc: list[str] = []
        for i, part in enumerate(path):
            acc.append(part)
            found = next((x for x in kids if x.get("text") == part), None)
            if not found:
                found = {
                    "id": f"n-{'-'.join(acc)}",
                    "text": _short_title(part, 20),
                    "kind": "feature" if i == len(path) - 1 else "module",
                    "path": list(acc),
                    "children": [],
                }
                kids.append(found)
            kids = found["children"]
        for j, p in enumerate(pts):
            if not isinstance(p, dict):
                continue
            kids.append(
                {
                    "id": f"{found.get('id')}-{j + 1}",
                    "text": _short_title(p.get("text") or f"点{j + 1}"),
                    "kind": "point",
                    "point_id": p.get("id") or "",
                    "platform": str(p.get("platform") or ""),
                    "case_ids": list(p.get("case_ids") or []),
                    "children": [],
                }
            )

    for path, pts in grouped.items():
        parts = path if isinstance(path, tuple) else tuple(split_label(path))
        ensure(parts or ("测试点",), pts)
    return root_kids


def apply_mindmap(req: dict, payload: dict) -> dict:
    next_req = dict(req)
    raw = payload if isinstance(payload, dict) else {}
    next_req["mindmap"] = _clip_mindmap(raw)
    next_req = _sync_points_from_mindmap(next_req)
    next_req["updated_at"] = _now()
    return next_req


def collect_mindmap_points(mindmap) -> list:
    """测试点 = kind=point（含带子节点的）以及没有子节点的叶子。中间层 module/feature 不算。"""
    out = []

    def walk(node, path):
        if not isinstance(node, dict):
            return
        text = str(node.get("text") or node.get("title") or "").strip()
        kind = str(node.get("kind") or "")
        kids = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        next_path = list(path)
        if text and kind not in ("root", "platform"):
            next_path = path + [text]
        leaf = not kids
        is_point = bool(text) and kind not in ("root", "platform") and (
            kind == "point" or (leaf and kind not in ("module", "feature"))
        )
        if is_point:
            row = dict(node)
            row["path"] = node.get("path") or next_path
            out.append(row)
        for child in kids:
            walk(child, next_path)

    walk(mindmap if isinstance(mindmap, dict) else {}, [])
    return out


def _sync_points_from_mindmap(req: dict) -> dict:
    leaves = collect_mindmap_points(req.get("mindmap"))
    if not leaves:
        return req
    und = dict(req.get("understanding") or {})
    old = [p for p in (und.get("points") or []) if isinstance(p, dict)]
    old_by_id = {str(p.get("id") or ""): p for p in old}
    old_by_text = {str(p.get("text") or "").strip(): p for p in old}
    points = []
    used = set()
    for i, leaf in enumerate(leaves):
        pid = str(leaf.get("point_id") or leaf.get("id") or f"tp{i + 1}").strip() or f"tp{i + 1}"
        if pid in used:
            pid = f"{pid}-{i + 1}"
        used.add(pid)
        text = str(leaf.get("text") or leaf.get("title") or "").strip()
        prev = old_by_id.get(pid) or old_by_text.get(text) or {}
        points.append(
            {
                "id": pid,
                "kind": str(leaf.get("point_kind") or prev.get("kind") or "正向"),
                "text": text or str(prev.get("text") or ""),
                "detail": str(leaf.get("detail") or prev.get("detail") or ""),
                "path": leaf.get("path") or prev.get("path") or [],
                "platform": str(leaf.get("platform") or prev.get("platform") or ""),
                "case_ids": list(prev.get("case_ids") or leaf.get("case_ids") or []),
                "waived": bool(prev.get("waived")),
                "waive_reason": prev.get("waive_reason") or "",
            }
        )
    und["points"] = points
    next_req = dict(req)
    next_req["understanding"] = und
    return next_req


def _gap_points(req: dict) -> list:
    out = []
    for p in ((req.get("understanding") or {}).get("points") or []):
        if not isinstance(p, dict) or p.get("waived"):
            continue
        if p.get("case_ids"):
            continue
        out.append(p)
    return out


def _count_mindmap_points(payload) -> int:
    return len(collect_mindmap_points(payload if isinstance(payload, dict) else {}))


def _append_cover_history(req: dict, *, job: str, kind: str, note: str, art: dict) -> None:
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    if job == "draft_mindmap":
        summary = f"{_count_mindmap_points(payload)} 个测试点"
        snap = payload
    else:
        rows = [x for x in (payload.get("cases") or []) if isinstance(x, dict)]
        summary = f"{len(rows)} 条用例"
        snap = {"cases": rows}
    hist = [x for x in (req.get("cover_history") or []) if isinstance(x, dict)]
    hist.append(
        {
            "id": f"gen-{int(time.time() * 1000):x}-{job[-6:]}",
            "at": _now(),
            "job": job,
            "kind": kind if kind in ("retry", "generate") else "generate",
            "note": str(note or "").strip(),
            "engine": art.get("engine") or "",
            "suggest": art.get("suggest") or "",
            "summary": summary,
            "payload": snap,
        }
    )
    req["cover_history"] = hist[-24:]


def _apply_cover_art(req: dict, art: dict, *, job: str, user_note: str = "", replace: bool = False) -> dict:
    had_map = isinstance(req.get("mindmap"), dict) and bool((req.get("mindmap") or {}).get("children"))
    had_cases = bool(req.get("draft_cases"))
    if job == "draft_mindmap":
        next_req = apply_mindmap(req, art.get("payload") or {})
        kind = "retry" if (replace or had_map) else "generate"
    else:
        next_req = apply_cases(req, art.get("payload") or {}, replace=replace)
        kind = "retry" if (replace or had_cases) else "generate"
    _append_cover_history(next_req, job=job, kind=kind, note=user_note, art=art)
    next_req["ai_artifacts"] = [*(next_req.get("ai_artifacts") or []), art][-12:]
    return next_req


def _norm_points(items) -> list:
    out = []
    used = set()
    for i, p in enumerate(items or []):
        if not isinstance(p, dict) or p.get("waived"):
            continue
        pid = str(p.get("id") or p.get("point_id") or f"tp{i + 1}").strip() or f"tp{i + 1}"
        if pid in used:
            pid = f"{pid}-{i + 1}"
        used.add(pid)
        text = str(p.get("text") or p.get("title") or "").strip()
        if not text:
            continue
        out.append(
            {
                "id": pid,
                "kind": str(p.get("kind") or p.get("point_kind") or "正向"),
                "text": text,
                "detail": str(p.get("detail") or ""),
                "path": p.get("path") or [],
                "platform": str(p.get("platform") or ""),
            }
        )
    return out


def _case_point_ids(rows) -> set:
    ids = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for pid in row.get("point_ids") or []:
            if pid:
                ids.add(str(pid))
    return ids


def _stub_case(req: dict, point: dict, index: int, *, aspect: str = "正向") -> dict:
    pid = str(point.get("id") or f"tp{index + 1}")
    text = str(point.get("text") or "测试点").strip()
    path = [str(x).strip() for x in (point.get("path") or []) if str(x).strip()]
    module = "-".join(path) or str(req.get("title") or "功能")
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    journeys = [x for x in (und.get("journeys") or []) if isinstance(x, dict)]
    steps = []
    n = 1
    if journeys:
        j = journeys[0]
        entry = str(j.get("entry") or "").strip()
        if entry:
            steps.append(f"{n}. 打开应用，进入「{entry}」")
            n += 1
        for via in j.get("via") or []:
            name = str(via or "").strip()
            if name:
                steps.append(f"{n}. 进入「{name}」")
                n += 1
        page = str(j.get("page") or "").strip()
        if page:
            steps.append(f"{n}. 打开「{page}」")
            n += 1
    if not steps:
        steps.append(f"{n}. 打开应用，按「{module}」进入对应页面")
        n += 1
    if aspect == "异常":
        steps.append(f"{n}. 触发「{text}」的失败/取消/超时")
        n += 1
        expected = "有明确失败或兜底提示，且可以返回重试"
        name = f"{text[:28]}失败兜底"
    elif aspect == "边界":
        steps.append(f"{n}. 用空值、超限或非法格式覆盖「{text}」")
        n += 1
        expected = "非法输入不被提交，提示可理解"
        name = f"{text[:28]}边界"
    else:
        steps.append(f"{n}. 按正向路径覆盖「{text}」")
        n += 1
        expected = "达到该测试点描述的结果"
        name = text[:40]
    steps.append(f"{n}. 核对页面展示与提示")
    plat = str(point.get("platform") or "").strip() or "双端"
    if plat in ("运营平台", "后台"):
        plat = "web"
    suffix = {"正向": "ok", "异常": "ex", "边界": "bd"}.get(aspect, "ok")
    return {
        "case_id": f"draft-{pid}-{suffix}",
        "name": name,
        "module": module,
        "aspect": aspect,
        "precondition": "账号可用，应用可启动" if plat != "web" else "运营平台账号可登录",
        "steps": "\n".join(steps),
        "expected": f"1. {expected}",
        "point_ids": [pid],
        "platform": plat,
    }


def _stub_cases_for_point(req: dict, point: dict, index: int) -> list:
    blob = f"{point.get('kind') or ''}{point.get('text') or ''}{point.get('detail') or ''}"
    aspects = ["正向"]
    if any(k in blob for k in ("上传", "保存", "下单", "提交", "失败", "兜底", "权限", "登录")):
        aspects.append("异常")
    if any(k in blob for k in ("上传", "输入", "数量", "空", "格式", "大小")):
        aspects.append("边界")
    return [_stub_case(req, point, index, aspect=a) for a in aspects]


def _case_writer_context(req: dict) -> dict:
    bundle = _analysis_bundle(req)
    return {
        "title": req.get("title"),
        "journeys": bundle.get("journeys") or [],
        "new_features": bundle.get("new_features") or [],
        "keep_features": bundle.get("keep_features") or [],
        "exceptions": bundle.get("exceptions") or [],
        "surfaces": bundle.get("surfaces") or [],
        "retry_note": bundle.get("analyst_feedback") or "",
    }


def _graft_missing_points(mindmap: dict, missing: list) -> dict:
    root = dict(mindmap or {})
    kids = list(root.get("children") or []) if isinstance(root.get("children"), list) else []
    plat_label = {"app": "App", "web": "Web", "e2e": "端到端"}
    for i, item in enumerate(missing or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        plat = str(item.get("platform") or "app").strip().lower()
        if plat not in plat_label:
            plat = "app"
        path = [str(x).strip() for x in (item.get("path") or []) if str(x).strip()]
        platform_node = next(
            (c for c in kids if isinstance(c, dict) and (c.get("platform") == plat or c.get("text") == plat_label[plat])),
            None,
        )
        if not platform_node:
            platform_node = {"id": f"p-{plat}", "text": plat_label[plat], "kind": "platform", "platform": plat, "children": []}
            kids.append(platform_node)
        cursor = platform_node
        parts = [p for p in path if p not in (plat_label[plat], text)]
        for j, part in enumerate(parts):
            children = cursor.setdefault("children", [])
            if not isinstance(children, list):
                children = []
                cursor["children"] = children
            found = next((c for c in children if isinstance(c, dict) and c.get("text") == part), None)
            if not found:
                found = {
                    "id": f"bf-{i}-{j}",
                    "text": part,
                    "kind": "feature" if j == len(parts) - 1 else "module",
                    "path": parts[: j + 1],
                    "children": [],
                }
                children.append(found)
            cursor = found
        children = cursor.setdefault("children", [])
        if not isinstance(children, list):
            children = []
            cursor["children"] = children
        if any(isinstance(c, dict) and str(c.get("text") or "") == text for c in children):
            continue
        children.append(
            {
                "id": f"bf-pt-{i + 1}",
                "text": text[:40],
                "kind": "point",
                "point_id": f"tp-bf-{i + 1}",
                "point_kind": item.get("kind") or "异常",
                "platform": plat,
                "detail": str(item.get("reason") or ""),
                "case_ids": [],
                "children": [],
            }
        )
    root["children"] = kids
    return root


def _append_unique_cases(rows: list, incoming: list, batch_ids: set | None = None) -> None:
    used = {str(x.get("case_id") or "") for x in rows}
    for row in incoming:
        if not isinstance(row, dict):
            continue
        row = dict(row)
        pids = [str(x) for x in (row.get("point_ids") or []) if x]
        if batch_ids is not None:
            pids = [pid for pid in pids if pid in batch_ids][:1] or pids[:1]
        if not pids:
            continue
        row["point_ids"] = pids[:1]
        cid = str(row.get("case_id") or "").strip() or f"draft-{row['point_ids'][0]}-{len(rows) + 1}"
        if cid in used:
            cid = f"{cid}-{len(rows) + 1}"
        used.add(cid)
        row["case_id"] = cid
        rows.append(row)


CASE_BATCH = 4


def _fill_cases(req: dict, target: list, rows: list, ctx: dict, usage: dict) -> tuple[list, dict, str]:
    last_meta: dict = {}
    engine = "rule"
    rounds = 0
    missing: list = []
    pending = [p for p in target if p["id"] not in _case_point_ids(rows)]
    while pending and rounds < 16:
        rounds += 1
        batch = pending[:CASE_BATCH]
        batch_ids = {p["id"] for p in batch}
        user = json.dumps(
            {
                **ctx,
                "points": batch,
                "note": "每个测试点按多种情况展开成多条用例，不要一条点一条。发现脑图没有的必须测场景写入 missing_points。",
            },
            ensure_ascii=False,
        )
        parsed, meta = _ask_json(
            CASE_WRITER_SYSTEM_PROMPT,
            user,
            max_tokens=4096,
            timeout_sec=90,
            role="case-writer",
            job="draft_cases",
        )
        last_meta = meta or {}
        _add_usage(usage, last_meta)
        got = []
        if isinstance(parsed, dict):
            got = [x for x in (parsed.get("cases") or []) if isinstance(x, dict)]
            missing.extend([x for x in (parsed.get("missing_points") or []) if isinstance(x, dict)])
            if last_meta.get("engine") == "llm" and got:
                engine = "llm"
        covered_before = _case_point_ids(rows)
        _append_unique_cases(rows, got, batch_ids)
        covered_after = _case_point_ids(rows)
        if not (covered_after - covered_before) & batch_ids:
            for p in batch:
                if p["id"] not in covered_after:
                    _append_unique_cases(rows, _stub_cases_for_point(req, p, len(rows)))
        pending = [p for p in target if p["id"] not in _case_point_ids(rows)]
    for p in pending:
        _append_unique_cases(rows, _stub_cases_for_point(req, p, len(rows)))
    return missing, last_meta, engine


def draft_cases(req: dict, cases: list | None = None, *, user_note: str = "", replace: bool = False) -> dict:
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    leaves = collect_mindmap_points(req.get("mindmap"))
    und_points = [p for p in (und.get("points") or []) if isinstance(p, dict)]
    target = _norm_points(leaves or und_points or _gap_points(req))
    note = str(user_note or req.get("analyst_feedback") or "").strip()
    ctx = _case_writer_context(req)
    ctx["retry_note"] = note or ctx.get("retry_note") or ""
    rows: list[dict] = [] if replace else [x for x in (req.get("draft_cases") or []) if isinstance(x, dict)]
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    missing, last_meta, engine = _fill_cases(req, target, rows, ctx, usage)
    mindmap = req.get("mindmap") if isinstance(req.get("mindmap"), dict) else {}
    if missing:
        mindmap = _graft_missing_points(mindmap, missing)
        extra_req = dict(req)
        extra_req["mindmap"] = mindmap
        extra_req = _sync_points_from_mindmap(extra_req)
        extra = _norm_points(collect_mindmap_points(extra_req.get("mindmap")))
        extra = [p for p in extra if p["id"] not in _case_point_ids(rows)]
        if extra:
            more, meta2, eng2 = _fill_cases(extra_req, extra, rows, ctx, usage)
            last_meta = meta2 or last_meta
            if eng2 == "llm":
                engine = "llm"
            missing.extend(more)
    last_meta = {**last_meta, **usage}
    covered = len(_case_point_ids(rows))
    suggest = f"按多种情况写了 {len(rows)} 条用例，覆盖 {covered} 个测试点"
    if missing:
        suggest += f"；反推补了 {len(missing)} 个脑图测试点"
    payload = {"cases": rows, "missing_points": missing}
    if missing:
        payload["mindmap"] = mindmap
    return _with_engine(
        artifact(
            job="draft_cases",
            suggest=suggest,
            citations=[req.get("id") or ""],
            payload=payload,
            input_hash=_hash(json.dumps({"ids": [p["id"] for p in target], "note": note}, ensure_ascii=False)),
        ),
        engine if rows else "none",
        last_meta,
    )


def apply_analyze(req: dict, payload: dict) -> dict:
    next_req = dict(req)
    und = dict(next_req.get("understanding") or {})
    ac = [str(x).strip() for x in (payload.get("ac") or []) if str(x).strip()]
    if ac:
        und["ac"] = ac
    points = []
    old = {str(p.get("id")): p for p in (und.get("points") or []) if isinstance(p, dict)}
    for i, p in enumerate(payload.get("points") or []):
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or f"tp{i + 1}")
        prev = old.get(pid) or {}
        points.append(
            {
                "id": pid,
                "kind": p.get("kind") or prev.get("kind") or "正向",
                "text": p.get("text") or prev.get("text") or "",
                "detail": p.get("detail") or prev.get("detail") or "",
                "path": p.get("path") or prev.get("path") or [],
                "platform": str(p.get("platform") or prev.get("platform") or ""),
                "case_ids": list(prev.get("case_ids") or []),
                "waived": bool(prev.get("waived")),
            }
        )
    if points:
        und["points"] = points
    if not und.get("source_excerpt"):
        und["source_excerpt"] = str(req.get("source_text") or _source_text(req) or "")[:20000]
    und["extracted_at"] = _now()
    und["engine"] = "llm" if payload else und.get("engine") or "rule"
    if payload.get("change_kind"):
        und["change_kind"] = str(payload.get("change_kind") or "")
    if payload.get("baseline"):
        und["baseline"] = str(payload.get("baseline") or "")
    if payload.get("delta"):
        und["delta"] = str(payload.get("delta") or "")
    for key in ("journeys", "new_features", "keep_features", "exceptions", "surfaces"):
        rows = _as_dict_list(payload.get(key))
        if rows:
            und[key] = rows
    impact = payload.get("impact") if isinstance(payload.get("impact"), dict) else {}
    prev_impact = und.get("impact") if isinstance(und.get("impact"), dict) else {}
    und["impact"] = {
        "platforms": list(impact.get("platforms") or prev_impact.get("platforms") or []),
        "notes": impact.get("notes") or prev_impact.get("notes") or "",
        "modules": list(impact.get("modules") or prev_impact.get("modules") or []),
        "features": list(impact.get("features") or prev_impact.get("features") or []),
        "e2e": bool(impact["e2e"]) if "e2e" in impact else bool(prev_impact.get("e2e")),
        "how_to_run": str(impact.get("how_to_run") or prev_impact.get("how_to_run") or ""),
    }
    hang = payload.get("hang") if isinstance(payload.get("hang"), dict) else {}
    creates = payload.get("atlas_create") if isinstance(payload.get("atlas_create"), list) else []
    paths = []
    for item in hang.get("paths") or []:
        if isinstance(item, (list, tuple)):
            parts = [str(x).strip() for x in item if str(x).strip()]
        else:
            parts = [p.strip() for p in str(item).replace(">", "/").replace("-", "/").split("/") if p.strip()]
        if parts:
            paths.append(parts)
    next_req["atlas_intent"] = {
        "hang": {
            "paths": paths,
            "module_names": [str(x).strip() for x in (hang.get("module_names") or []) if str(x).strip()],
            "feature_names": [str(x).strip() for x in (hang.get("feature_names") or []) if str(x).strip()],
        },
        "create": [x for x in creates if isinstance(x, dict)],
    }
    if payload.get("risks"):
        und["stale_risks"] = [str(x) for x in payload.get("risks") if x]
    next_req["understanding"] = und
    next_req["summary"] = payload.get("summary") or next_req.get("summary") or next_req.get("title")
    feats = []
    for i, f in enumerate(payload.get("features") or []):
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "").strip()
        if name:
            feats.append({"id": f.get("id") or f"feat-{i + 1}", "name": name, "notes": f.get("notes") or ""})
    if feats:
        next_req["features"] = feats
    next_req["updated_at"] = _now()
    return next_req


def apply_cases(req: dict, payload: dict, *, replace: bool = False) -> dict:
    next_req = dict(req)
    rows = []
    missing = []
    if isinstance(payload, dict):
        rows = [x for x in (payload.get("cases") or []) if isinstance(x, dict)]
        missing = [x for x in (payload.get("missing_points") or []) if isinstance(x, dict)]
        if isinstance(payload.get("mindmap"), dict) and payload["mindmap"].get("children"):
            next_req["mindmap"] = payload["mindmap"]
        elif missing:
            next_req["mindmap"] = _graft_missing_points(next_req.get("mindmap") or {}, missing)
        if missing:
            next_req = _sync_points_from_mindmap(next_req)
            next_req["mindmap_backfill"] = missing
    next_req["draft_cases"] = [_normalize_draft_case(row, i) for i, row in enumerate(rows)]
    und = dict(next_req.get("understanding") or {})
    points = []
    for p in und.get("points") or []:
        if not isinstance(p, dict):
            continue
        p = dict(p)
        hung = [] if replace else list(p.get("case_ids") or [])
        if replace:
            hung = []
        for row in next_req["draft_cases"]:
            if p.get("id") in (row.get("point_ids") or []) and row.get("case_id") and row.get("case_id") not in hung:
                hung.append(row.get("case_id"))
        p["case_ids"] = hung
        points.append(p)
    if points:
        und["points"] = points
        next_req["understanding"] = und
    next_req["updated_at"] = _now()
    return next_req


def propose_atlas(qa_process: dict, cases: list | None = None, *, user_note: str = "") -> dict:
    current = atlas.normalize_atlas(qa_process.get("app_atlas"))
    reqs = [r for r in (qa_process.get("requirements") or []) if isinstance(r, dict)]
    module_hints = []
    seen_mod = set()
    for c in cases or []:
        if not isinstance(c, dict):
            continue
        module = str(c.get("module") or "").strip()
        if not module or module in seen_mod:
            continue
        seen_mod.add(module)
        module_hints.append(
            {
                "module": module,
                "cases": [
                    str(x.get("name") or x.get("title") or "")
                    for x in (cases or [])
                    if isinstance(x, dict) and str(x.get("module") or "") == module
                ][:8],
            }
        )
        if len(module_hints) >= 16:
            break
    user = json.dumps(
        {
            "current_atlas": atlas.compact_atlas(current),
            "requirements": [
                {
                    "id": r.get("id"),
                    "title": r.get("title"),
                    "summary": r.get("summary"),
                    "features": r.get("features") or [],
                    "atlas_intent": r.get("atlas_intent") or {},
                    "module_ids": r.get("module_ids") or [],
                    "feature_ids": r.get("feature_ids") or [],
                    "change_kind": (r.get("understanding") or {}).get("change_kind") if isinstance(r.get("understanding"), dict) else "",
                    "baseline": (r.get("understanding") or {}).get("baseline") if isinstance(r.get("understanding"), dict) else "",
                    "delta": (r.get("understanding") or {}).get("delta") if isinstance(r.get("understanding"), dict) else "",
                    "impact": (r.get("understanding") or {}).get("impact") if isinstance(r.get("understanding"), dict) else {},
                    "analyst_feedback": r.get("analyst_feedback") or "",
                }
                for r in reqs[:40]
            ],
            "existing_cases": [
                {
                    "case_id": c.get("case_id"),
                    "name": c.get("name") or c.get("title"),
                    "module": c.get("module"),
                }
                for c in (cases or [])[:40]
            ],
            "draft_cases": [
                {"case_id": c.get("case_id"), "name": c.get("name"), "module": c.get("module")}
                for r in reqs[:20]
                for c in (r.get("draft_cases") or [])
                if isinstance(c, dict)
            ][:40],
            "module_hints": module_hints,
            "human_feedback": str(user_note or "").strip(),
            "note": "module_hints 只是参考，不要当成权威模块划分。优化需求优先挂已有节点。有 human_feedback 必须按人的理解重提。",
        },
        ensure_ascii=False,
    )
    parsed, meta = _ask_json(REQ_ANALYST_IMPACT_PROMPT, user, max_tokens=4500, role="req-analyst", job="propose_atlas")
    case_changes = []
    if parsed:
        after = atlas.merge_payload(current, parsed, reqs)
        reason = str(parsed.get("reason") or "建议更新应用图谱或相关用例")
        if user_note:
            reason = f"{reason}｜按人的补充重提"
        engine = "llm"
        case_changes = [x for x in (parsed.get("case_changes") or []) if isinstance(x, dict)]
        if not case_changes:
            case_changes = atlas.rule_case_changes(current, after, cases or [], reqs)
    else:
        after = atlas.rule_propose(current, reqs, cases or [])
        reason = "规则草稿：按需求功能点搭骨架，用例模块仅作参考"
        engine = "rule"
        meta = meta or {}
        case_changes = atlas.rule_case_changes(current, after, cases or [], reqs)
    return _with_engine(
        artifact(
            job="propose_atlas",
            suggest=reason,
            citations=[r.get("id") or "" for r in reqs if r.get("id")],
            payload={"atlas": after, "reason": reason, "case_changes": case_changes},
            input_hash=_hash(user),
        ),
        engine if atlas.atlas_has_nodes(after) or atlas.atlas_has_nodes(current) or case_changes else "none",
        meta,
    )


def run_llm_job(job: str, *, requirement=None, cases=None, qa_process=None) -> dict:
    req = requirement or {}
    if job == "analyze_req":
        return analyze_req(req, cases or [], (qa_process or {}).get("app_atlas"))
    if job == "draft_mindmap":
        return draft_mindmap(req, cases or [], (qa_process or {}).get("app_atlas"))
    if job == "propose_atlas":
        return propose_atlas(qa_process or {}, cases or [])
    return draft_cases(req, cases or [])


def _autonomy(doc: dict) -> dict:
    raw = doc.get("autonomy") if isinstance(doc.get("autonomy"), dict) else {}
    return {
        "enabled": raw.get("enabled", True) is not False,
        "auto_analyze": raw.get("auto_analyze", True) is not False,
        "auto_mindmap": raw.get("auto_mindmap", True) is not False,
        "auto_cases": raw.get("auto_cases", True) is not False,
        "auto_atlas": raw.get("auto_atlas", True) is not False,
        "auto_dispatch": raw.get("auto_dispatch") is True,
    }


def _add_usage(total: dict, extra) -> None:
    if not isinstance(extra, dict):
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        total[key] = int(total.get(key) or 0) + int(extra.get(key) or 0)


def tick(*, qa_process: dict, cases: list | None = None, requirement_id: str = "", requirement_ids: list | None = None, app_id: str = "", app_name: str = "", user_note: str = "", force: bool = False, jobs: list | None = None) -> dict:
    """推进未完成的分析/脑图/用例草稿。不改验收门禁、不自动下发设备。jobs 指定时按重试处理。"""
    tok = dispatch.bind(trigger="qa_tick", app_id=app_id, app_name=app_name, pipeline_id=dispatch.new_pipeline_id())
    try:
        result = _tick_body(
            qa_process=qa_process,
            cases=cases,
            requirement_id=requirement_id,
            requirement_ids=requirement_ids,
            user_note=user_note,
            force=force,
            jobs=jobs,
        )
        actions = result.get("actions") or []
        did = [a for a in actions if a.get("action") not in ("skip", "skipped", "blocked", "")]
        if did:
            dispatch.record_job(
                status="done",
                job="qa_tick",
                role=did[0].get("role") or "req-analyst",
                detail="流程推进：" + " → ".join(str(a.get("action") or "") for a in did),
                input_data={"requirement_id": requirement_id or ""},
                output_data={"actions": [a.get("action") for a in did]},
            )
        return result
    except Exception as e:
        dispatch.record_job(status="error", job="qa_tick", role="req-analyst", error=str(e)[:240])
        raise
    finally:
        dispatch.reset(tok)


def _tick_body(*, qa_process: dict, cases: list | None = None, requirement_id: str = "", requirement_ids: list | None = None, user_note: str = "", force: bool = False, jobs: list | None = None) -> dict:
    """推进未完成的分析/脑图/用例草稿。不改验收门禁、不自动下发设备。"""
    doc = dict(qa_process or {})
    reqs = [dict(r) for r in (doc.get("requirements") or []) if isinstance(r, dict)]
    auto = _autonomy(doc)
    log = [x for x in (doc.get("role_log") or []) if isinstance(x, dict)]
    actions: list[dict] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    target = str(requirement_id or "").strip()
    want = {str(x).strip() for x in (requirement_ids or []) if str(x).strip()}
    if target:
        want.add(target)
    note = str(user_note or "").strip()
    retry_jobs = {str(x).strip() for x in (jobs or []) if str(x).strip() in LLM_JOBS}
    cover_retry = bool(retry_jobs & {"draft_mindmap", "draft_cases"})
    if cover_retry and note:
        retry_jobs.add("analyze_req")
        if "draft_mindmap" in retry_jobs:
            retry_jobs.add("draft_cases")

    if not auto["enabled"]:
        return {"qa_process": doc, "actions": [{"role": "system", "action": "skipped", "detail": "autonomy.enabled=false"}]}

    for i, req in enumerate(reqs):
        if want and str(req.get("id") or "") not in want:
            continue
        rid = str(req.get("id") or "")
        text = _source_text(req)
        if not text:
            actions.append({"role": "req-analyst", "req_id": rid, "action": "skip", "detail": "没有需求原文"})
            continue
        src_hash = _hash(text)
        und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
        if note:
            req["analyst_feedback"] = note
        gate = str(req.get("gate") or "")
        allowed = set(retry_jobs) if retry_jobs else _jobs_for_step(doc, "req", gate)
        if force and not retry_jobs:
            allowed |= {"analyze_req", "propose_atlas"}

        if "analyze_req" in allowed and (bool(retry_jobs) or auto["auto_analyze"]) and (
            force or retry_jobs or und.get("source_hash") != src_hash
        ):
            art = analyze_req(req, cases or [], doc.get("app_atlas"), user_note=note)
            req = apply_analyze(req, art.get("payload") or {})
            und = dict(req.get("understanding") or {})
            und["engine"] = art.get("engine") or "rule"
            und["source_hash"] = src_hash
            req["understanding"] = und
            req["ai_artifacts"] = [*(req.get("ai_artifacts") or []), art][-12:]
            actions.append({"role": "req-analyst", "req_id": rid, "action": "analyze_req", "engine": art.get("engine"), "step_id": gate})
            log.append({"at": _now(), "role": "req-analyst", "job": "analyze_req", "req_id": rid, "engine": art.get("engine"), "step_id": gate, "output": art.get("suggest") or "已拆验收标准"})
            _add_usage(usage, art.get("usage"))

        want_map = "draft_mindmap" in allowed and (bool(retry_jobs) or auto["auto_mindmap"])
        empty_map = not (isinstance(req.get("mindmap"), dict) and req["mindmap"].get("children"))
        if want_map and (retry_jobs or (not force and empty_map)):
            art = draft_mindmap(req, cases or [], doc.get("app_atlas"), user_note=note)
            req = _apply_cover_art(req, art, job="draft_mindmap", user_note=note, replace=bool(retry_jobs))
            actions.append({"role": "mindmap-writer", "req_id": rid, "action": "draft_mindmap", "engine": art.get("engine"), "step_id": gate})
            log.append({"at": _now(), "role": "mindmap-writer", "job": "draft_mindmap", "req_id": rid, "engine": art.get("engine"), "step_id": gate, "output": art.get("suggest") or "已写脑图"})
            _add_usage(usage, art.get("usage"))

        want_cases = "draft_cases" in allowed and (bool(retry_jobs) or auto["auto_cases"])
        empty_cases = not req.get("draft_cases")
        if want_cases and (retry_jobs or (not force and (empty_cases or _gap_points(req)))):
            art = draft_cases(req, cases or [], user_note=note, replace=bool(retry_jobs))
            req = _apply_cover_art(req, art, job="draft_cases", user_note=note, replace=bool(retry_jobs))
            actions.append({"role": "case-writer", "req_id": rid, "action": "draft_cases", "engine": art.get("engine"), "step_id": gate})
            log.append({"at": _now(), "role": "case-writer", "job": "draft_cases", "req_id": rid, "engine": art.get("engine"), "step_id": gate, "output": art.get("suggest") or "已写用例草稿"})
            _add_usage(usage, art.get("usage"))

        reqs[i] = req
        req["_allowed_jobs"] = list(allowed)

    atlas_step = next((str(r.get("gate") or "") for r in reqs if "propose_atlas" in (r.get("_allowed_jobs") or []) and (not want or str(r.get("id") or "") in want)), "")
    for r in reqs:
        r.pop("_allowed_jobs", None)
    doc["requirements"] = reqs
    patches = [x for x in (doc.get("atlas_patches") or []) if isinstance(x, dict)]
    current_atlas = atlas.normalize_atlas(doc.get("app_atlas"))
    analyzed = any(a.get("action") == "analyze_req" for a in actions)
    if retry_jobs and "propose_atlas" not in retry_jobs:
        allow_atlas = False
    else:
        allow_atlas = bool(atlas_step) or force
    need_atlas = allow_atlas and atlas.has_seed_material(reqs, cases or []) and (
        not atlas.atlas_has_nodes(current_atlas) or analyzed or force or atlas.intent_needs_patch(reqs, current_atlas)
    )
    if auto.get("auto_atlas") is not False and need_atlas and (not atlas.pending_patches(patches) or force):
        art = propose_atlas({"app_atlas": current_atlas, "requirements": reqs}, cases or [], user_note=note)
        after = (art.get("payload") or {}).get("atlas") or {}
        reason = (art.get("payload") or {}).get("reason") or art.get("suggest") or "建议更新应用图谱"
        patch = atlas.enqueue_patch(
            patches,
            before=current_atlas,
            after=after,
            reason=reason,
            source={
                "req_id": target or next((str(a.get("req_id") or "") for a in actions if a.get("req_id")), ""),
                "engine": art.get("engine"),
                "human_feedback": note,
            },
            reqs=reqs,
            case_changes=(art.get("payload") or {}).get("case_changes") or [],
            force=bool(note or force),
        )
        if patch:
            actions.append(
                {
                    "role": "req-analyst",
                    "action": "propose_atlas",
                    "engine": art.get("engine"),
                    "detail": f"{len(patch.get('lines') or patch.get('diff') or [])} 处骨架 · {len(patch.get('case_changes') or [])} 条用例待确认",
                }
            )
            log.append({
                "at": _now(),
                "role": "req-analyst",
                "job": "propose_atlas",
                "engine": art.get("engine"),
                "req_id": target or next((str(a.get("req_id") or "") for a in actions if a.get("req_id")), ""),
                "step_id": atlas_step,
                "output": reason,
            })
            _add_usage(usage, art.get("usage"))
        elif art.get("engine") and art.get("engine") != "none":
            actions.append({"role": "req-analyst", "action": "skip", "detail": "影响范围无需变更"})

    doc["app_atlas"] = current_atlas
    doc["atlas_patches"] = patches[-20:]
    doc["features"] = atlas.flatten_features(current_atlas, reqs)
    doc["autonomy"] = auto
    doc["role_log"] = log[-80:]
    doc["updated_at"] = _now()
    if auto.get("auto_dispatch"):
        actions.append({"role": "test-engineer", "action": "blocked", "detail": "自动下发未开：需要设备选择，避免误跑"})
    return {"qa_process": doc, "actions": actions, "autonomy": auto, "usage": usage}


def run_followup_pipeline(
    *,
    qa_process: dict,
    cases: list | None = None,
    requirement_ids: list | None = None,
    trigger: str = "atlas_edit",
    app_id: str = "",
    app_name: str = "",
    pipeline_id: str = "",
    force: bool = False,
) -> dict:
    """骨架确认或人手改完之后：缺脑图就写脑图，缺用例就写用例。已有内容默认不重跑。"""
    doc = dict(qa_process or {})
    reqs = [dict(r) for r in (doc.get("requirements") or []) if isinstance(r, dict)]
    want = {str(x).strip() for x in (requirement_ids or []) if str(x).strip()}
    if not want:
        want = {str(r.get("id") or "") for r in reqs if r.get("id")}
    targets = [r for r in reqs if str(r.get("id") or "") in want]
    pipeline_id = pipeline_id or dispatch.new_pipeline_id()
    planned = 0
    for req in targets:
        has_map = isinstance(req.get("mindmap"), dict) and bool(req["mindmap"].get("children"))
        has_cases = bool(req.get("draft_cases"))
        if force or not has_map:
            planned += 1
        if force or not has_cases:
            planned += 1
    tok = dispatch.bind(
        trigger=trigger,
        app_id=app_id,
        app_name=app_name,
        pipeline_id=pipeline_id,
        step_total=max(1, planned),
    )
    actions: list[dict] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    log = [x for x in (doc.get("role_log") or []) if isinstance(x, dict)]
    step = 0
    by_id = {str(r.get("id") or ""): i for i, r in enumerate(reqs)}
    try:
        for req in targets:
            rid = str(req.get("id") or "")
            idx = by_id.get(rid)
            if idx is None:
                continue
            has_map = isinstance(req.get("mindmap"), dict) and bool(req["mindmap"].get("children"))
            has_cases = bool(req.get("draft_cases"))
            if force or not has_map:
                step += 1
                dispatch.bind(step_index=step, role="mindmap-writer", job="draft_mindmap")
                art = draft_mindmap(req, cases or [], doc.get("app_atlas"))
                req = _apply_cover_art(req, art, job="draft_mindmap", replace=force)
                actions.append({"role": "mindmap-writer", "req_id": rid, "action": "draft_mindmap", "engine": art.get("engine")})
                log.append({"at": _now(), "role": "mindmap-writer", "job": "draft_mindmap", "req_id": rid, "engine": art.get("engine"), "pipeline_id": pipeline_id})
                _add_usage(usage, art.get("usage"))
            if force or not has_cases:
                step += 1
                dispatch.bind(step_index=step, role="case-writer", job="draft_cases")
                art = draft_cases(req, cases or [], replace=force)
                req = _apply_cover_art(req, art, job="draft_cases", replace=force)
                actions.append({"role": "case-writer", "req_id": rid, "action": "draft_cases", "engine": art.get("engine")})
                log.append({"at": _now(), "role": "case-writer", "job": "draft_cases", "req_id": rid, "engine": art.get("engine"), "pipeline_id": pipeline_id})
                _add_usage(usage, art.get("usage"))
            reqs[idx] = req
        dispatch.bind(step_index=0, role="req-analyst", job="atlas_followup")
        dispatch.record_job(
            status="done",
            job="atlas_followup",
            role="req-analyst",
            detail=f"已补 {len(actions)} 步：脑图/用例" if actions else "这条需求已有脑图和用例，没有重跑",
            output_data={"actions": [a.get("action") for a in actions], "requirement_ids": list(want)},
        )
    except Exception as e:
        dispatch.bind(step_index=0, role="req-analyst", job="atlas_followup")
        dispatch.record_job(status="error", job="atlas_followup", role="req-analyst", error=str(e)[:240])
        raise
    finally:
        dispatch.reset(tok)
    doc["requirements"] = reqs
    doc["features"] = atlas.flatten_features(doc.get("app_atlas"), reqs)
    doc["role_log"] = log[-80:]
    doc["updated_at"] = _now()
    return {"qa_process": doc, "actions": actions, "usage": usage, "pipeline_id": pipeline_id, "autonomy": _autonomy(doc)}

