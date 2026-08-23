# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""应用图谱：按真实产品结构长出来的多层模块树。

模块可以嵌套（社区 → 帖子详情页），功能挂在叶子模块上（点赞 / 评论 / 分享）。
飞书分区只作对照，骨架变更必须人审。
"""
from __future__ import annotations

import copy
import json
import time
from typing import Any, Optional


MAX_DEPTH = 6


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hash(text: str) -> str:
    h = 5381
    for ch in str(text or ""):
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    return format(h, "x")


def _new_id(prefix: str, *parts: str) -> str:
    return f"{prefix}-{_hash('|'.join(str(x or '') for x in parts))[:10]}"


def empty_atlas() -> dict:
    return {"modules": [], "updated_at": ""}


def _uniq_ids(values) -> list:
    out = []
    for item in values or []:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def split_path(raw) -> list[str]:
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    for sep in (" / ", "/", " > ", "→", "->", "-"):
        if sep in text:
            return [p.strip() for p in text.split(sep) if p.strip()]
    return [text]


def normalize_feature(raw: dict | None, module_id: str = "") -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    return {
        "id": str(raw.get("id") or "").strip() or _new_id("feat", module_id, name),
        "name": name,
        "summary": str(raw.get("summary") or "").strip(),
        "req_ids": _uniq_ids(raw.get("req_ids")),
        "case_ids": _uniq_ids(raw.get("case_ids")),
    }


def _child_items(raw: dict) -> list:
    children = raw.get("children")
    if isinstance(children, list) and children:
        return [x for x in children if isinstance(x, dict)]
    nested = raw.get("modules")
    if isinstance(nested, list):
        return [x for x in nested if isinstance(x, dict)]
    return []


def normalize_module(raw: dict | None, *, depth: int = 0, parent_id: str = "") -> dict | None:
    if not isinstance(raw, dict) or depth > MAX_DEPTH:
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    mid = str(raw.get("id") or "").strip() or _new_id("mod", parent_id, name)
    feats = []
    seen_f = set()
    for item in raw.get("features") or []:
        feat = normalize_feature(item, mid)
        if not feat or feat["id"] in seen_f:
            continue
        seen_f.add(feat["id"])
        feats.append(feat)
    children = []
    seen_c = set()
    for item in _child_items(raw):
        child = normalize_module(item, depth=depth + 1, parent_id=mid)
        if not child or child["id"] in seen_c or child["id"] == mid:
            continue
        seen_c.add(child["id"])
        children.append(child)
    return {
        "id": mid,
        "name": name,
        "summary": str(raw.get("summary") or "").strip(),
        "parent_id": str(parent_id or raw.get("parent_id") or ""),
        "feishu_hints": [str(x).strip() for x in (raw.get("feishu_hints") or []) if str(x).strip()][:12],
        "req_ids": _uniq_ids(raw.get("req_ids")),
        "children": children,
        "features": feats,
    }


def normalize_atlas(raw) -> dict:
    if not isinstance(raw, dict):
        return empty_atlas()
    modules = []
    seen = set()
    for item in raw.get("modules") or []:
        mod = normalize_module(item, depth=0, parent_id="")
        if not mod or mod["id"] in seen:
            continue
        seen.add(mod["id"])
        modules.append(mod)
    return {"modules": modules, "updated_at": str(raw.get("updated_at") or "")}


def normalize_patch(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    pid = str(raw.get("id") or "").strip()
    if not pid:
        return None
    status = raw.get("status") if raw.get("status") in ("pending", "accepted", "rejected") else "pending"
    return {
        "id": pid,
        "at": str(raw.get("at") or ""),
        "role": str(raw.get("role") or "req-analyst"),
        "job": str(raw.get("job") or "propose_atlas"),
        "reason": str(raw.get("reason") or "").strip(),
        "source": raw.get("source") if isinstance(raw.get("source"), dict) else {},
        "before": normalize_atlas(raw.get("before")),
        "after": normalize_atlas(raw.get("after")),
        "diff": [x for x in (raw.get("diff") or []) if isinstance(x, dict)][:120],
        "lines": [str(x) for x in (raw.get("lines") or []) if str(x).strip()][:120],
        "case_changes": [x for x in (raw.get("case_changes") or []) if isinstance(x, dict)][:40],
        "status": status,
        "decided_at": str(raw.get("decided_at") or ""),
    }


def normalize_patches(raw) -> list:
    rows = []
    seen = set()
    for item in raw or []:
        patch = normalize_patch(item)
        if not patch or patch["id"] in seen:
            continue
        seen.add(patch["id"])
        rows.append(patch)
    return rows[-20:]


def atlas_has_nodes(atlas: dict | None) -> bool:
    return bool((normalize_atlas(atlas).get("modules") or []))


def iter_modules(atlas: dict | None):
    """Walk the live tree. Do not normalize first — callers mutate returned nodes."""
    def walk(mod: dict, depth: int, path: list[str]):
        yield mod, depth, path
        for child in mod.get("children") or []:
            if isinstance(child, dict):
                yield from walk(child, depth + 1, path + [child.get("name") or ""])

    root = atlas if isinstance(atlas, dict) else {}
    for mod in root.get("modules") or []:
        if isinstance(mod, dict):
            yield from walk(mod, 0, [mod.get("name") or ""])


def flatten_tree(atlas: dict | None) -> list[dict]:
    rows = []
    for mod, depth, path in iter_modules(normalize_atlas(atlas)):
        rows.append(
            {
                "id": mod["id"],
                "kind": "module",
                "name": mod["name"],
                "summary": mod.get("summary") or "",
                "depth": depth,
                "path": " / ".join(path),
                "parent_id": mod.get("parent_id") or "",
                "req_ids": list(mod.get("req_ids") or []),
                "case_ids": [],
                "child_modules": len(mod.get("children") or []),
                "feature_count": len(mod.get("features") or []),
            }
        )
        for feat in mod.get("features") or []:
            rows.append(
                {
                    "id": feat["id"],
                    "kind": "feature",
                    "name": feat["name"],
                    "summary": feat.get("summary") or "",
                    "depth": depth + 1,
                    "path": " / ".join(path + [feat["name"]]),
                    "parent_id": mod["id"],
                    "module_id": mod["id"],
                    "req_ids": list(feat.get("req_ids") or []),
                    "case_ids": list(feat.get("case_ids") or []),
                    "child_modules": 0,
                    "feature_count": 0,
                }
            )
    return rows


def paths_for_req(atlas: dict | None, req_id: str) -> list[str]:
    rid = str(req_id or "").strip()
    if not rid:
        return []
    return [row["path"] for row in flatten_tree(atlas) if rid in (row.get("req_ids") or [])]


def intent_needs_patch(requirements: list | None, atlas_doc: dict | None) -> bool:
    current = flatten_tree(atlas_doc)
    names = {row["name"] for row in current}
    hung = set()
    for row in current:
        hung.update(row.get("req_ids") or [])
    for req in requirements or []:
        if not isinstance(req, dict):
            continue
        rid = str(req.get("id") or "")
        intent = req.get("atlas_intent") if isinstance(req.get("atlas_intent"), dict) else {}
        if intent.get("create"):
            return True
        hang = intent.get("hang") if isinstance(intent.get("hang"), dict) else {}
        for path in hang.get("paths") or []:
            parts = split_path(path)
            if parts and parts[-1] not in names:
                return True
        for name in list(hang.get("module_names") or []) + list(hang.get("feature_names") or []):
            if str(name).strip() and str(name).strip() not in names:
                return True
        if rid and rid not in hung and (hang.get("paths") or hang.get("module_names") or hang.get("feature_names") or req.get("features")):
            return True
    return False


def has_seed_material(requirements: list | None, cases: list | None) -> bool:
    for req in requirements or []:
        if not isinstance(req, dict):
            continue
        if str(req.get("title") or "").strip():
            return True
        if req.get("draft_cases"):
            return True
        und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
        if und.get("points") or und.get("ac") or und.get("source_excerpt"):
            return True
        if req.get("features"):
            return True
    for case in cases or []:
        if isinstance(case, dict) and (case.get("case_id") or case.get("name") or case.get("title")):
            return True
    return False


def _canon_module(mod: dict) -> dict:
    return {
        "id": mod["id"],
        "name": mod["name"],
        "summary": mod.get("summary") or "",
        "req_ids": list(mod.get("req_ids") or []),
        "children": [_canon_module(c) for c in (mod.get("children") or [])],
        "features": [
            {
                "id": f["id"],
                "name": f["name"],
                "summary": f.get("summary") or "",
                "req_ids": list(f.get("req_ids") or []),
            }
            for f in (mod.get("features") or [])
        ],
    }


def _canon(atlas: dict | None) -> str:
    payload = [_canon_module(m) for m in (normalize_atlas(atlas).get("modules") or [])]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def find_module(atlas: dict, *, module_id: str = "", name: str = "") -> dict | None:
    mid = str(module_id or "").strip()
    name = str(name or "").strip()
    named = None
    for mod, _depth, _path in iter_modules(atlas):
        if mid and mod.get("id") == mid:
            return mod
        if name and mod.get("name") == name and named is None:
            named = mod
    return named


def find_feature(atlas: dict, *, feature_id: str = "", name: str = "", module_id: str = "") -> tuple[dict | None, dict | None]:
    fid = str(feature_id or "").strip()
    name = str(name or "").strip()
    module_id = str(module_id or "").strip()
    named = (None, None)
    for mod, _depth, _path in iter_modules(atlas):
        if module_id and mod.get("id") != module_id:
            continue
        for feat in mod.get("features") or []:
            if fid and feat.get("id") == fid:
                return mod, feat
            if name and feat.get("name") == name and named[1] is None:
                named = (mod, feat)
        if module_id:
            continue
    return named


def ensure_module(atlas: dict, name: str, *, parent: dict | None = None, summary: str = "", module_id: str = "") -> dict:
    name = str(name or "").strip()
    if parent is None:
        existing = None
        if module_id:
            existing = find_module(atlas, module_id=module_id)
        if existing is None:
            for mod in atlas.setdefault("modules", []):
                if mod.get("name") == name:
                    existing = mod
                    break
        if existing:
            if summary and not existing.get("summary"):
                existing["summary"] = summary
            return existing
        row = normalize_module({"id": module_id, "name": name, "summary": summary, "features": [], "children": []}, depth=0)
        atlas.setdefault("modules", []).append(row)
        return row
    existing = None
    if module_id:
        existing = next((c for c in (parent.get("children") or []) if c.get("id") == module_id), None)
    if existing is None:
        existing = next((c for c in (parent.get("children") or []) if c.get("name") == name), None)
    if existing:
        if summary and not existing.get("summary"):
            existing["summary"] = summary
        return existing
    depth = 1
    cursor = parent
    while cursor.get("parent_id"):
        depth += 1
        found = find_module(atlas, module_id=cursor.get("parent_id") or "")
        if not found:
            break
        cursor = found
    row = normalize_module(
        {"id": module_id, "name": name, "summary": summary, "features": [], "children": []},
        depth=min(depth, MAX_DEPTH),
        parent_id=parent.get("id") or "",
    )
    parent.setdefault("children", []).append(row)
    return row


def ensure_feature(atlas: dict, module: dict, name: str, *, summary: str = "", feature_id: str = "") -> dict:
    name = str(name or "").strip()
    _, existing = find_feature(atlas, feature_id=feature_id, name=name, module_id=module.get("id") or "")
    if existing:
        if summary and not existing.get("summary"):
            existing["summary"] = summary
        return existing
    row = normalize_feature({"id": feature_id, "name": name, "summary": summary}, module.get("id") or "")
    module.setdefault("features", []).append(row)
    return row


def ensure_path(atlas: dict, names: list[str], *, last_is_feature: bool = False, summary: str = "") -> tuple[dict | None, dict | None]:
    parts = [str(x).strip() for x in names if str(x).strip()]
    if not parts:
        return None, None
    if last_is_feature and len(parts) == 1:
        mod = ensure_module(atlas, "业务功能")
        return mod, ensure_feature(atlas, mod, parts[0], summary=summary)
    parent = None
    mod_names = parts[:-1] if last_is_feature else parts
    feat_name = parts[-1] if last_is_feature else ""
    for name in mod_names:
        parent = ensure_module(atlas, name, parent=parent)
    feat = ensure_feature(atlas, parent, feat_name, summary=summary) if parent and feat_name else None
    return parent, feat


def hang_req(atlas: dict, req_id: str, *, module_id: str = "", feature_id: str = "") -> None:
    rid = str(req_id or "").strip()
    if not rid:
        return
    if feature_id:
        mod, feat = find_feature(atlas, feature_id=feature_id)
        if feat and rid not in feat["req_ids"]:
            feat["req_ids"].append(rid)
        if mod and rid not in mod["req_ids"]:
            mod["req_ids"].append(rid)
        cursor = mod
        while cursor and cursor.get("parent_id"):
            parent = find_module(atlas, module_id=cursor.get("parent_id") or "")
            if not parent:
                break
            if rid not in parent["req_ids"]:
                parent["req_ids"].append(rid)
            cursor = parent
        return
    if module_id:
        mod = find_module(atlas, module_id=module_id)
        if mod and rid not in mod["req_ids"]:
            mod["req_ids"].append(rid)


def diff_atlas(before, after) -> list[dict]:
    left = {row["path"]: row for row in flatten_tree(before)}
    right = {row["path"]: row for row in flatten_tree(after)}
    rows = []
    for path, node in right.items():
        prev = left.get(path)
        if not prev:
            rows.append({"op": "add", "kind": node["kind"], "name": path, "after": node.get("summary") or "", "depth": node.get("depth") or 0})
        elif (prev.get("summary") or "") != (node.get("summary") or ""):
            rows.append(
                {
                    "op": "update",
                    "kind": node["kind"],
                    "name": path,
                    "before": prev.get("summary") or "",
                    "after": node.get("summary") or "",
                    "depth": node.get("depth") or 0,
                }
            )
        old_reqs = set(prev.get("req_ids") or []) if prev else set()
        for rid in node.get("req_ids") or []:
            if rid not in old_reqs:
                rows.append({"op": "hang", "kind": node["kind"], "name": path, "req_id": rid, "depth": node.get("depth") or 0})
    for path, node in left.items():
        if path in right:
            continue
        rows.append({"op": "remove", "kind": node["kind"], "name": path, "before": node.get("summary") or "", "depth": node.get("depth") or 0})
    return rows[:120]


def diff_lines(diff: list | None, reqs: list | None = None) -> list[str]:
    titles = {str(r.get("id") or ""): str(r.get("title") or r.get("id") or "") for r in (reqs or []) if isinstance(r, dict)}
    lines = []
    for row in diff or []:
        op = row.get("op")
        kind = "模块" if row.get("kind") == "module" else "功能"
        name = row.get("name") or ""
        if op == "add":
            extra = f"：{row.get('after')}" if row.get("after") else ""
            lines.append(f"+ {kind} {name}{extra}")
        elif op == "remove":
            lines.append(f"- {kind} {name}")
        elif op == "update":
            lines.append(f"~ {kind} {name}「{row.get('before') or '（空）'}」→「{row.get('after') or '（空）'}」")
        elif op == "hang":
            title = titles.get(row.get("req_id") or "", row.get("req_id") or "需求")
            lines.append(f"+ 需求「{title}」挂到{kind} {name}")
    return lines


def flatten_features(atlas: dict | None, requirements: list | None = None) -> list:
    """兼容旧 features 表：只从已确认图谱展开，不用飞书模块。"""
    req_map = {str(r.get("id") or ""): r for r in (requirements or []) if isinstance(r, dict)}
    rows = []
    for row in flatten_tree(atlas):
        case_ids = list(row.get("case_ids") or [])
        points = 0
        for rid in row.get("req_ids") or []:
            req = req_map.get(rid) or {}
            case_ids.extend(str(c.get("case_id") or "") for c in (req.get("draft_cases") or []) if isinstance(c, dict))
            points += len(((req.get("understanding") or {}).get("points") or []))
        item = {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "source": "atlas",
            "summary": row.get("summary") or "",
            "path": row.get("path") or "",
            "depth": row.get("depth") or 0,
            "parent_id": row.get("parent_id") or "",
            "req_ids": list(row.get("req_ids") or []),
            "case_ids": _uniq_ids(case_ids),
            "point_count": points,
        }
        if row["kind"] == "feature":
            item["module_id"] = row.get("module_id") or row.get("parent_id") or ""
            item["module_name"] = (row.get("path") or "").rsplit(" / ", 1)[0]
        rows.append(item)
    return rows


def rule_case_changes(before, after, cases: list | None, requirements: list | None) -> list:
    old_names = {row["name"] for row in flatten_tree(before)}
    new_names = []
    for row in flatten_tree(after):
        if row["kind"] == "feature" and row["name"] not in old_names:
            new_names.append(row["name"])
        if row["kind"] == "module" and row["name"] not in old_names:
            new_names.append(row["name"])
    out = []
    seen = set()
    for case in list(cases or []):
        if not isinstance(case, dict):
            continue
        blob = f"{case.get('name') or ''} {case.get('title') or ''} {case.get('module') or ''}"
        if not any(n and n in blob for n in new_names):
            continue
        cid = str(case.get("case_id") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append({"case_id": cid, "name": case.get("name") or case.get("title") or cid, "reason": "需求改了相关功能，步骤/预期可能过时"})
    for req in requirements or []:
        if not isinstance(req, dict):
            continue
        for case in req.get("draft_cases") or []:
            if not isinstance(case, dict):
                continue
            blob = f"{case.get('name') or ''} {case.get('module') or ''}"
            if not any(n and n in blob for n in new_names):
                continue
            cid = str(case.get("case_id") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            out.append({"case_id": cid, "name": case.get("name") or cid, "reason": "草稿用例挂在受影响功能上，确认后需要复核"})
    return out[:40]


def pending_patches(patches: list | None) -> list:
    return [p for p in (patches or []) if isinstance(p, dict) and p.get("status") == "pending"]


def enqueue_patch(
    patches: list,
    *,
    before: dict,
    after: dict,
    reason: str,
    source: dict | None = None,
    role: str = "req-analyst",
    reqs: list | None = None,
    case_changes: list | None = None,
    force: bool = False,
) -> Optional[dict]:
    before = normalize_atlas(before)
    after = normalize_atlas(after)
    changes = [x for x in (case_changes or []) if isinstance(x, dict)][:40]
    diff = diff_atlas(before, after)
    same = _canon(before) == _canon(after)
    if same and not changes and not force:
        return None
    if not atlas_has_nodes(after) and not atlas_has_nodes(before) and not changes and not force:
        return None
    if not diff and not changes and not force:
        return None
    fingerprint = _hash(
        _canon(after)
        + reason
        + json.dumps(changes, ensure_ascii=False, sort_keys=True)[:200]
        + str((source or {}).get("human_feedback") or "")
    )
    for row in pending_patches(patches):
        if row.get("id") == f"ap-{fingerprint[:12]}":
            return None
    patch = {
        "id": f"ap-{fingerprint[:12]}",
        "at": _now(),
        "role": role,
        "job": "propose_atlas",
        "reason": reason or "需求可能影响模块、功能或用例",
        "source": source or {},
        "before": before,
        "after": after,
        "diff": diff,
        "lines": diff_lines(diff, reqs),
        "case_changes": changes,
        "status": "pending",
        "decided_at": "",
    }
    patches.append(patch)
    return patch


def apply_hangs_to_reqs(requirements: list, atlas: dict) -> list:
    hung_mod: dict[str, list] = {}
    hung_feat: dict[str, list] = {}
    hung_path: dict[str, list] = {}
    for row in flatten_tree(atlas):
        for rid in row.get("req_ids") or []:
            hung_path.setdefault(rid, []).append(row.get("path") or "")
            if row["kind"] == "module":
                hung_mod.setdefault(rid, []).append(row["id"])
            else:
                hung_feat.setdefault(rid, []).append(row["id"])
                if row.get("parent_id"):
                    hung_mod.setdefault(rid, []).append(row["parent_id"])
    out = []
    for req in requirements or []:
        if not isinstance(req, dict):
            continue
        row = dict(req)
        rid = str(row.get("id") or "")
        if rid in hung_mod:
            row["module_ids"] = _uniq_ids(hung_mod.get(rid) or [])
        if rid in hung_feat:
            row["feature_ids"] = _uniq_ids(hung_feat.get(rid) or [])
        if rid in hung_path:
            row["atlas_paths"] = _uniq_ids(hung_path.get(rid) or [])
        out.append(row)
    return out


def sort_releases(releases: list | None) -> list:
    rows = [r for r in (releases or []) if isinstance(r, dict)]
    return sorted(rows, key=lambda r: str(r.get("created_at") or r.get("updated_at") or ""))


def stamp_atlas_on_release(doc: dict, atlas: dict, release_id: str = "") -> dict:
    """把已确认图谱写到指定版本；没指定就写到最新版本。app_atlas 始终是当前工作稿。"""
    next_doc = dict(doc or {})
    atlas = normalize_atlas(atlas)
    next_doc["app_atlas"] = atlas
    rels = [dict(r) for r in (next_doc.get("releases") or []) if isinstance(r, dict)]
    rid = str(release_id or next_doc.get("atlas_release_id") or "").strip()
    if not rid and rels:
        rid = str((sort_releases(rels)[-1] or {}).get("id") or "")
    for rel in rels:
        if str(rel.get("id") or "") != rid:
            continue
        rel["atlas"] = atlas
        rel["atlas_at"] = _now()
        break
    next_doc["releases"] = rels
    if rid:
        next_doc["atlas_release_id"] = rid
    return next_doc


def atlas_for_release(doc: dict, release_id: str = "") -> dict:
    rid = str(release_id or "").strip()
    if rid:
        for rel in doc.get("releases") or []:
            if isinstance(rel, dict) and str(rel.get("id") or "") == rid and isinstance(rel.get("atlas"), dict):
                return normalize_atlas(rel.get("atlas"))
    return normalize_atlas(doc.get("app_atlas"))


def patch_followup_req_ids(patch: dict | None) -> list[str]:
    row = patch if isinstance(patch, dict) else {}
    ids: list[str] = []
    src = row.get("source") if isinstance(row.get("source"), dict) else {}
    if src.get("req_id"):
        ids.append(str(src.get("req_id") or "").strip())
    for item in row.get("diff") or []:
        if isinstance(item, dict) and item.get("op") == "hang" and item.get("req_id"):
            ids.append(str(item.get("req_id") or "").strip())
    return list(dict.fromkeys(x for x in ids if x))


def patch_is_structural(patch: dict | None) -> bool:
    for item in (patch or {}).get("diff") or []:
        if isinstance(item, dict) and item.get("op") in ("add", "remove", "update"):
            return True
    return False


def accept_patch(doc: dict, patch_id: str) -> tuple[dict, dict | None]:
    patches = [dict(x) for x in (doc.get("atlas_patches") or []) if isinstance(x, dict)]
    found = None
    for row in patches:
        if row.get("id") == patch_id:
            found = row
            break
    if not found or found.get("status") != "pending":
        return doc, None
    after = normalize_atlas(found.get("after"))
    after["updated_at"] = _now()
    found["status"] = "accepted"
    found["decided_at"] = _now()
    next_doc = dict(doc)
    next_doc["app_atlas"] = after
    next_doc["requirements"] = apply_hangs_to_reqs(next_doc.get("requirements") or [], after)
    next_doc["features"] = flatten_features(after, next_doc.get("requirements") or [])
    next_doc["atlas_patches"] = patches
    next_doc["updated_at"] = _now()
    return next_doc, found


def reject_patch(doc: dict, patch_id: str, note: str = "") -> tuple[dict, dict | None]:
    patches = [dict(x) for x in (doc.get("atlas_patches") or []) if isinstance(x, dict)]
    found = None
    for row in patches:
        if row.get("id") == patch_id:
            found = row
            break
    if not found or found.get("status") != "pending":
        return doc, None
    found["status"] = "rejected"
    found["decided_at"] = _now()
    found["reject_note"] = str(note or "").strip()
    next_doc = dict(doc)
    next_doc["atlas_patches"] = patches
    next_doc["updated_at"] = _now()
    return next_doc, found


def apply_reject_feedback(doc: dict, patch: dict | None, note: str) -> tuple[dict, list[str]]:
    """驳回后把人的说明写到相关需求上，并清掉旧分析哈希，好让下一轮重跑。"""
    text = str(note or "").strip()
    hung = patch_followup_req_ids(patch)
    if not hung:
        src = (patch or {}).get("source") if isinstance((patch or {}).get("source"), dict) else {}
        sid = str(src.get("req_id") or "").strip()
        if sid:
            hung = [sid]
    if not hung:
        return dict(doc), []
    reqs = []
    touched: list[str] = []
    for raw in doc.get("requirements") or []:
        if not isinstance(raw, dict):
            continue
        row = dict(raw)
        rid = str(row.get("id") or "")
        if hung and rid not in hung:
            reqs.append(row)
            continue
        if text:
            notes = [x for x in (row.get("analyst_notes") or []) if isinstance(x, dict)]
            notes.append({"at": _now(), "text": text, "patch_id": (patch or {}).get("id") or ""})
            row["analyst_notes"] = notes[-8:]
            row["analyst_feedback"] = text
        und = dict(row.get("understanding") or {})
        und["source_hash"] = ""
        row["understanding"] = und
        row["mindmap"] = {}
        reqs.append(row)
        if rid:
            touched.append(rid)
    next_doc = dict(doc)
    next_doc["requirements"] = reqs
    return next_doc, touched


def compact_atlas(atlas: dict | None) -> list:
    def pack(mod: dict) -> dict:
        return {
            "id": mod["id"],
            "name": mod["name"],
            "summary": mod.get("summary") or "",
            "children": [pack(c) for c in (mod.get("children") or [])],
            "features": [
                {"id": f["id"], "name": f["name"], "summary": f.get("summary") or ""}
                for f in (mod.get("features") or [])
            ],
        }

    return [pack(m) for m in (normalize_atlas(atlas).get("modules") or [])]


def _merge_module_item(atlas: dict, item: dict, parent: dict | None = None) -> dict | None:
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    mod = ensure_module(
        atlas,
        name,
        parent=parent,
        summary=str(item.get("summary") or ""),
        module_id=str(item.get("id") or ""),
    )
    for child in _child_items(item):
        _merge_module_item(atlas, child, parent=mod)
    for feat in item.get("features") or []:
        if not isinstance(feat, dict):
            continue
        fname = str(feat.get("name") or "").strip()
        if not fname:
            continue
        row = ensure_feature(
            atlas,
            mod,
            fname,
            summary=str(feat.get("summary") or ""),
            feature_id=str(feat.get("id") or ""),
        )
        for rid in feat.get("req_ids") or []:
            hang_req(atlas, rid, feature_id=row["id"])
    for rid in item.get("req_ids") or []:
        hang_req(atlas, rid, module_id=mod["id"])
    return mod


def merge_payload(current: dict, payload: dict, requirements: list | None = None) -> dict:
    """把需求分析师 JSON 合成一版 after 图谱。已有同名节点保留 id。"""
    after = copy.deepcopy(normalize_atlas(current))
    modules = payload.get("modules") if isinstance(payload.get("modules"), list) else []
    for item in modules:
        if isinstance(item, dict):
            _merge_module_item(after, item, parent=None)

    hangs = payload.get("hang") if isinstance(payload.get("hang"), list) else []
    req_map = {str(r.get("id") or ""): r for r in (requirements or []) if isinstance(r, dict)}
    for hang in hangs:
        if not isinstance(hang, dict):
            continue
        rid = str(hang.get("req_id") or "").strip()
        if not rid or rid not in req_map:
            continue
        paths = hang.get("paths") or hang.get("path")
        if isinstance(paths, (str, list)) and paths and not (isinstance(paths, list) and paths and isinstance(paths[0], (list, tuple))):
            paths = [paths]
        for path in paths or []:
            parts = split_path(path)
            if len(parts) >= 2:
                _mod, feat = ensure_path(after, parts, last_is_feature=True)
                if feat:
                    hang_req(after, rid, feature_id=feat["id"])
            elif parts:
                mod = find_module(after, name=parts[0]) or ensure_module(after, parts[0])
                hang_req(after, rid, module_id=mod["id"])
        for mname in hang.get("module_names") or []:
            mod = find_module(after, name=str(mname or "").strip())
            if mod:
                hang_req(after, rid, module_id=mod["id"])
        for fname in hang.get("feature_names") or []:
            _mod, feat = find_feature(after, name=str(fname or "").strip())
            if feat:
                hang_req(after, rid, feature_id=feat["id"])
    return normalize_atlas(after)


def rule_propose(current: dict, requirements: list | None, cases: list | None) -> dict:
    """没有模型时的骨架草稿。飞书分区只进 hints，不直接当权威模块名。"""
    after = copy.deepcopy(normalize_atlas(current))
    for req in requirements or []:
        if not isinstance(req, dict):
            continue
        rid = str(req.get("id") or "").strip()
        intent = req.get("atlas_intent") if isinstance(req.get("atlas_intent"), dict) else {}
        creates = intent.get("create") or []
        hang = intent.get("hang") if isinstance(intent.get("hang"), dict) else {}
        for path in hang.get("paths") or []:
            parts = split_path(path)
            if len(parts) >= 2:
                _mod, feat = ensure_path(after, parts, last_is_feature=True)
                if feat and rid:
                    hang_req(after, rid, feature_id=feat["id"])
            elif parts:
                mod = ensure_module(after, parts[0])
                if rid:
                    hang_req(after, rid, module_id=mod["id"])
        for item in creates:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind") or item.get("type")
            name = str(item.get("name") or "").strip()
            parent_name = str(item.get("parent_name") or item.get("module_name") or "").strip()
            path = split_path(item.get("path") or [])
            if kind == "module" and name:
                parent = find_module(after, name=parent_name) if parent_name else None
                if parent is None and path:
                    parent, _feat = ensure_path(after, path, last_is_feature=False)
                ensure_module(after, name, parent=parent, summary=str(item.get("summary") or ""))
            elif kind == "feature" and name:
                if path:
                    mod, _feat = ensure_path(after, path, last_is_feature=False)
                elif parent_name:
                    parts = split_path(parent_name)
                    mod, _feat = ensure_path(after, parts, last_is_feature=False)
                    if mod is None:
                        mod = ensure_module(after, parent_name)
                else:
                    mod = ensure_module(after, "业务功能")
                if mod:
                    ensure_feature(after, mod, name, summary=str(item.get("summary") or ""))
        feat_names = [str(x.get("name") or "").strip() for x in (req.get("features") or []) if isinstance(x, dict)]
        feat_names += [str(x).strip() for x in (hang.get("feature_names") or []) if str(x).strip()]
        feat_names = [x for x in feat_names if x]
        mod_names = [str(x).strip() for x in (hang.get("module_names") or []) if str(x).strip()]
        if not flatten_tree(after) and not mod_names and feat_names:
            mod_names = ["业务功能"]
        for mname in mod_names:
            parts = split_path(mname)
            mod, _feat = ensure_path(after, parts, last_is_feature=False)
            if mod and rid:
                hang_req(after, rid, module_id=mod["id"])
        if len(mod_names) == 1 and feat_names:
            parent = find_module(after, name=split_path(mod_names[0])[-1]) or ensure_module(after, mod_names[0])
            for fname in feat_names:
                feat = ensure_feature(after, parent, fname)
                if rid:
                    hang_req(after, rid, feature_id=feat["id"])

    if not atlas_has_nodes(after):
        hints = []
        for case in cases or []:
            if not isinstance(case, dict):
                continue
            module = str(case.get("module") or "").strip()
            if module and module not in hints:
                hints.append(module)
        if hints:
            mod = ensure_module(after, "待整理", summary="飞书分区只作对照。确认前请按真实页面改成模块树，不要把表格分区当成骨架。")
            mod["feishu_hints"] = hints[:16]

    after["updated_at"] = _now()
    return after
