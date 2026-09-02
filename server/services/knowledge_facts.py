# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""带版本的事实层：文档入库、捕获提案、矛盾边作废、as-of 版本窗。

知识条目仍是原料。这里只处理「哪条现在还算数」以及怎么从需求/发版/轨迹长出事实。
绑定值不进简报正文；冲突以屏幕为准。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional

from script.log import SLog

from server.services.knowledge_situation import (
    bind_slot_of,
    normalize_bind,
    normalize_facet,
    normalize_situation,
)

TAG = "KnowledgeFacts"
PROPOSAL_KINDS = ("align", "conflict", "new_fact")
DOC_SOURCES = frozenset({"requirement", "release", "doc"})
TRACE_CAPS = frozenset({
    "inspect_session", "session_align", "assert_visual",
    "tap_element", "input_text",
})
_VER_IN_TITLE = re.compile(r"v?(\d+(?:\.\d+){0,3})", re.I)
_ENSURED: set[str] = set()
PersistFn = Callable[[Dict[str, Any]], Dict[str, Any]]


def version_tuple(value: str) -> tuple[int, ...]:
    parts = [int(x) for x in re.findall(r"\d+", str(value or ""))]
    return tuple(parts[:4]) if parts else (0,)


def version_from_title(title: str) -> str:
    m = _VER_IN_TITLE.search(str(title or ""))
    return str(m.group(1) or "").strip() if m else ""


def version_applies(item: Dict[str, Any], app_version: str = "") -> bool:
    """当前包装进这条事实的有效窗。不知道版本时：已作废的仍排除，其余保留。"""
    inf = str(item.get("invalid_from") or "").strip()
    if inf in ("*", "0", "0.0.0"):
        return False
    vf = str(item.get("valid_from") or "").strip()
    ver = str(app_version or "").strip()
    if not ver:
        return True
    v = version_tuple(ver)
    if vf and v < version_tuple(vf):
        return False
    if inf and v >= version_tuple(inf):
        return False
    return True


def item_is_live(
    item: Dict[str, Any],
    app_version: str = "",
    *,
    require_approved: bool = True,
) -> bool:
    if not isinstance(item, dict):
        return False
    if item.get("enabled") is False:
        return False
    if str(item.get("superseded_by") or "").strip():
        return False
    if require_approved:
        st = str(item.get("review_status") or "").strip().lower()
        if st and st != "approved":
            return False
    return version_applies(item, app_version)


def filter_live(items: Iterable[Dict[str, Any]], app_version: str = "") -> List[Dict[str, Any]]:
    return [x for x in (items or []) if item_is_live(x, app_version)]


def fact_key(item: Dict[str, Any]) -> tuple:
    """同一槽 / 同一壳层角色 / 同一来源引用 视为一条边。"""
    bind = normalize_bind(item.get("bind"))
    sit = normalize_situation(item.get("situation"))
    facet = normalize_facet(item.get("facet"))
    apps = ",".join(sorted(str(a).strip() for a in (item.get("app_ids") or []) if str(a).strip()))
    surface = sit.get("surface") or bind.get("surface") or ""
    env = bind.get("env") or ""
    slot = bind.get("slot") or sit.get("slot") or bind_slot_of(item)
    if slot:
        return ("bind", apps, slot, surface, env)
    src = str(item.get("source") or "").strip()
    ref = str(item.get("source_ref") or "").strip()
    if src and ref:
        return ("ref", apps, src, ref)
    if facet == "chrome" or sit.get("need") == "judge_selected":
        return ("chrome", apps, surface, sit.get("screen_role") or "chrome_nav")
    title = re.sub(r"\s+", "", str(item.get("title") or "").lower())[:48]
    return ("title", apps, title, surface)


def _body(item: Dict[str, Any]) -> str:
    return re.sub(r"\s+", " ", str(item.get("content") or "")).strip()


def _bind_value(item: Dict[str, Any]) -> str:
    return str(normalize_bind(item.get("bind")).get("value") or "").strip()


def _app_ok(item: Dict[str, Any], app_id: str) -> bool:
    if not app_id:
        return True
    ids = [str(a).strip() for a in (item.get("app_ids") or []) if str(a).strip()]
    if not ids:
        return True
    return str(app_id) in ids


def peers_for(draft: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    key = fact_key(draft)
    out: List[Dict[str, Any]] = []
    for row in existing or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("superseded_by") or "").strip():
            continue
        if row.get("enabled") is False:
            continue
        if fact_key(row) != key:
            continue
        out.append(row)
    return out


def infer_proposal_kind(draft: Dict[str, Any], peers: List[Dict[str, Any]]) -> str:
    kind = str(draft.get("proposal_kind") or "").strip().lower()
    if kind in PROPOSAL_KINDS:
        return kind
    if not peers:
        return "new_fact"
    peer = peers[0]
    nv, ov = _bind_value(draft), _bind_value(peer)
    if nv and ov:
        return "align" if nv == ov else "conflict"
    nb, ob = _body(draft), _body(peer)
    if nb and ob and nb == ob:
        return "align"
    if nb and ob and fact_key(draft)[0] in ("bind", "chrome") and nb != ob:
        return "conflict"
    return "new_fact"


def should_hold_proposal(item: Dict[str, Any], expert: Optional[Dict[str, Any]] = None) -> bool:
    if str(item.get("proposal_kind") or "").strip().lower() == "conflict":
        return True
    if expert and (expert.get("conflicts") or []):
        return True
    return False


def invalidate_item(
    item: Dict[str, Any],
    *,
    superseded_by: str = "",
    invalid_from: str = "",
    persist_fn: Optional[PersistFn] = None,
) -> Dict[str, Any]:
    row = dict(item)
    if superseded_by:
        row["superseded_by"] = str(superseded_by)
    row["invalid_from"] = str(invalid_from or row.get("invalid_from") or "*").strip() or "*"
    if persist_fn:
        return persist_fn(row)
    from server.services.system_settings_service import upsert_knowledge_item

    return upsert_knowledge_item(row, skip_extract=True)


def on_fact_approved(
    row: Dict[str, Any],
    *,
    app_version: str = "",
    existing: Optional[List[Dict[str, Any]]] = None,
    persist_fn: Optional[PersistFn] = None,
) -> Dict[str, Any]:
    """人审/机审通过冲突提案时，作废旧边。"""
    cid = str(row.get("conflicts_with") or "").strip()
    if not cid:
        return row
    pool = existing
    if pool is None:
        from server.services.system_settings_service import list_testing_knowledge

        pool = list_testing_knowledge()
    old = next((x for x in pool if str(x.get("id") or "") == cid), None)
    if not old:
        return row
    invalidate_item(
        old,
        superseded_by=str(row.get("id") or ""),
        invalid_from=str(app_version or "*"),
        persist_fn=persist_fn,
    )
    return row


def apply_proposal(
    draft: Dict[str, Any],
    *,
    existing: Optional[List[Dict[str, Any]]] = None,
    app_id: str = "",
    source: str = "case_run",
    origin_task_id: str = "",
    origin_case_id: str = "",
    app_version: str = "",
    auto_review: bool = True,
    persist_fn: Optional[PersistFn] = None,
    review_fn: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """捕获结果变成提案：对齐只计数；冲突 pending 且不作废直到通过；新事实才可机审。"""
    if not isinstance(draft, dict):
        return []
    title = str(draft.get("title") or "").strip()
    content = str(draft.get("content") or "").strip()
    if not title or not content:
        return []
    pool = list(existing or [])
    row = dict(draft)
    row["title"] = title[:48]
    row["content"] = content[:4000]
    if app_id:
        row["app_ids"] = [app_id]
    row["source"] = source
    row["enabled"] = True
    if origin_task_id:
        row["origin_task_id"] = origin_task_id
    if origin_case_id:
        row["origin_case_id"] = origin_case_id
    if app_version and not str(row.get("valid_from") or "").strip():
        row["valid_from"] = app_version
    peers = peers_for(row, pool)
    kind = infer_proposal_kind(row, peers)
    row["proposal_kind"] = kind

    def _save(item: Dict[str, Any], *, skip_extract: bool = False) -> Dict[str, Any]:
        if persist_fn:
            return persist_fn(item)
        from server.services.system_settings_service import upsert_knowledge_item

        return upsert_knowledge_item(item, skip_extract=skip_extract)

    if kind == "align" and peers:
        peer = dict(peers[0])
        peer["aligned_count"] = int(peer.get("aligned_count") or 0) + 1
        saved = _save(peer, skip_extract=True)
        SLog.i(TAG, f"align fact {saved.get('id')} kind={kind}")
        return [saved]

    row["review_status"] = "pending"
    if kind == "conflict" and peers:
        row["conflicts_with"] = str(peers[0].get("id") or "")
        saved = _save(row)
        SLog.i(TAG, f"conflict proposal {saved.get('id')} vs {row['conflicts_with']}")
        return [saved]

    saved = _save(row)
    if auto_review and kind == "new_fact":
        fn = review_fn
        if fn is None and persist_fn is None:
            try:
                from server.services.knowledge_review_service import review_new_items

                fn = review_new_items
            except Exception:
                fn = None
        if fn:
            try:
                reviewed = fn([saved])
                return list(reviewed or [saved])
            except Exception as exc:
                SLog.w(TAG, f"auto review skipped: {exc}")
    return [saved]


def drafts_from_requirement(req: Dict[str, Any], *, app_id: str = "") -> List[Dict[str, Any]]:
    """需求/AC 抽成事实，不把整篇 PRD 塞进去。"""
    if not isinstance(req, dict):
        return []
    rid = str(req.get("id") or req.get("external_id") or "").strip()
    title = str(req.get("title") or req.get("external_id") or "").strip()
    if not title:
        return []
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    acs = und.get("ac") if isinstance(und.get("ac"), list) else []
    points = und.get("points") if isinstance(und.get("points"), list) else []
    excerpt = str(und.get("source_excerpt") or "").strip().replace("\n", " ")
    lines: List[tuple[str, str]] = []
    for i, ac in enumerate(acs[:8]):
        text = str(ac or "").strip()
        if text:
            lines.append((f"{rid or title}:ac:{i}", text[:400]))
    for i, pt in enumerate(points[:6]):
        if isinstance(pt, dict):
            text = str(pt.get("text") or "").strip()
        else:
            text = str(pt or "").strip()
        if text:
            lines.append((f"{rid or title}:pt:{i}", text[:400]))
    if excerpt and not lines:
        lines.append((f"{rid or title}:ex", excerpt[:240]))
    out: List[Dict[str, Any]] = []
    for ref, body in lines:
        out.append({
            "title": f"需求口径：{title}"[:48],
            "content": body,
            "category": "业务逻辑",
            "tags": ["需求", rid] if rid else ["需求"],
            "app_ids": [app_id] if app_id else [],
            "source": "requirement",
            "source_ref": ref,
            "review_status": "approved",
            "review_method": "ingest",
            "proposal_kind": "new_fact",
            "facet": "hybrid",
            "situation": {"need": "judge", "lane": "expect"},
            "enabled": True,
        })
    return out


def drafts_from_release(
    rel: Dict[str, Any],
    *,
    app_id: str = "",
    valid_from: str = "",
    invalid_from: str = "",
    hung_titles: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    if not isinstance(rel, dict):
        return []
    rid = str(rel.get("id") or "").strip()
    title = str(rel.get("title") or "").strip()
    if not title:
        return []
    ver = str(rel.get("version") or "").strip() or version_from_title(title) or valid_from
    bits: List[str] = [title]
    for key in ("notes", "changelog", "summary", "desc"):
        bit = str(rel.get(key) or "").strip()
        if bit:
            bits.append(bit[:800])
    hung = [str(x).strip() for x in (hung_titles or []) if str(x).strip()]
    if hung:
        bits.append("包含需求：" + "；".join(hung[:12]))
    body = "\n".join(bits).strip()
    if not body:
        return []
    row = {
        "title": f"发版说明：{title}"[:48],
        "content": body[:2000],
        "category": "业务逻辑",
        "tags": ["发版", ver] if ver else ["发版"],
        "app_ids": [app_id] if app_id else [],
        "source": "release",
        "source_ref": rid or title,
        "review_status": "approved",
        "review_method": "ingest",
        "proposal_kind": "new_fact",
        "facet": "hybrid",
        "situation": {"need": "judge", "lane": "expect"},
        "enabled": True,
    }
    if ver:
        row["valid_from"] = ver
    elif valid_from:
        row["valid_from"] = valid_from
    if invalid_from:
        row["invalid_from"] = invalid_from
    return [row]


def _upsert_ingest(
    draft: Dict[str, Any],
    existing: List[Dict[str, Any]],
    persist_fn: Optional[PersistFn],
) -> Optional[Dict[str, Any]]:
    ref = str(draft.get("source_ref") or "").strip()
    src = str(draft.get("source") or "").strip()
    found = None
    for row in existing:
        if str(row.get("source") or "") == src and str(row.get("source_ref") or "") == ref:
            found = row
            break
    if found and _body(found) == _body(draft) and str(found.get("valid_from") or "") == str(draft.get("valid_from") or "") and str(found.get("invalid_from") or "") == str(draft.get("invalid_from") or ""):
        return found
    payload = dict(found or {})
    payload.update(draft)
    if found:
        payload["id"] = found.get("id")
        payload["review_status"] = found.get("review_status") or draft.get("review_status") or "approved"
    if persist_fn:
        saved = persist_fn(payload)
    else:
        from server.services.system_settings_service import upsert_knowledge_item

        saved = upsert_knowledge_item(payload, skip_extract=True)
    if found:
        found.update(saved)
    else:
        existing.append(saved)
    return saved


def ingest_requirement_facts(
    app_id: str,
    requirements: Optional[List[Dict[str, Any]]] = None,
    *,
    existing: Optional[List[Dict[str, Any]]] = None,
    persist_fn: Optional[PersistFn] = None,
) -> List[Dict[str, Any]]:
    pool = list(existing if existing is not None else [])
    seen_refs: set[str] = set()
    saved: List[Dict[str, Any]] = []
    for req in requirements or []:
        for draft in drafts_from_requirement(req, app_id=app_id):
            seen_refs.add(str(draft.get("source_ref") or ""))
            row = _upsert_ingest(draft, pool, persist_fn)
            if row:
                saved.append(row)
    for row in list(pool):
        if str(row.get("source") or "") != "requirement":
            continue
        if not _app_ok(row, app_id):
            continue
        ref = str(row.get("source_ref") or "")
        if ref and ref not in seen_refs and not str(row.get("superseded_by") or "").strip():
            invalidate_item(row, superseded_by="dropped", invalid_from="*", persist_fn=persist_fn)
    return saved


def ingest_release_facts(
    app_id: str,
    releases: Optional[List[Dict[str, Any]]] = None,
    requirements: Optional[List[Dict[str, Any]]] = None,
    *,
    existing: Optional[List[Dict[str, Any]]] = None,
    persist_fn: Optional[PersistFn] = None,
) -> List[Dict[str, Any]]:
    req_by_id = {
        str(r.get("id") or ""): r
        for r in (requirements or [])
        if isinstance(r, dict) and r.get("id")
    }
    rows: List[tuple[str, Dict[str, Any]]] = []
    for rel in releases or []:
        if not isinstance(rel, dict):
            continue
        ver = str(rel.get("version") or "").strip() or version_from_title(str(rel.get("title") or ""))
        rows.append((ver, rel))
    rows.sort(key=lambda x: version_tuple(x[0]) if x[0] else (0,))
    pool = list(existing if existing is not None else [])
    saved: List[Dict[str, Any]] = []
    for i, (ver, rel) in enumerate(rows):
        next_ver = rows[i + 1][0] if i + 1 < len(rows) else ""
        hung = []
        for rid in rel.get("requirement_ids") or []:
            req = req_by_id.get(str(rid) or "")
            if req:
                hung.append(str(req.get("title") or rid))
        for draft in drafts_from_release(
            rel, app_id=app_id, valid_from=ver, invalid_from=next_ver, hung_titles=hung,
        ):
            row = _upsert_ingest(draft, pool, persist_fn)
            if row:
                saved.append(row)
    return saved


def ingest_app_docs(
    app_id: str,
    *,
    qp: Optional[Dict[str, Any]] = None,
    existing: Optional[List[Dict[str, Any]]] = None,
    persist_fn: Optional[PersistFn] = None,
) -> Dict[str, int]:
    aid = str(app_id or "").strip()
    if not aid:
        return {"requirements": 0, "releases": 0}
    proc = qp
    if proc is None:
        try:
            from sqlalchemy.orm import joinedload

            from server.core.database import SessionLocal
            from server.models.project import App
            from server.services.app_automation_service import get_automation_config

            db = SessionLocal()
            try:
                app = db.query(App).options(joinedload(App.project)).filter(App.id == aid).first()
                proc = (get_automation_config(app).get("qa_process") or {}) if app else {}
            finally:
                db.close()
        except Exception as exc:
            SLog.w(TAG, f"ingest_app_docs load failed {aid}: {exc}")
            proc = {}
    pool = existing
    if pool is None and persist_fn is None:
        from server.services.system_settings_service import list_testing_knowledge

        pool = [x for x in list_testing_knowledge() if _app_ok(x, aid)]
    elif pool is None:
        pool = []
    reqs = [x for x in (proc.get("requirements") or []) if isinstance(x, dict)]
    rels = [x for x in (proc.get("releases") or []) if isinstance(x, dict)]
    r1 = ingest_requirement_facts(aid, reqs, existing=pool, persist_fn=persist_fn)
    r2 = ingest_release_facts(aid, rels, reqs, existing=pool, persist_fn=persist_fn)
    return {"requirements": len(r1), "releases": len(r2)}


def drafts_from_traces(
    traces: Optional[List[Any]] = None,
    *,
    app_id: str = "",
) -> List[Dict[str, Any]]:
    """历史通过轨迹压成入门提案，不自动通过。"""
    session_bits: List[str] = []
    path_bits: List[str] = []
    for tr in traces or []:
        events = getattr(tr, "event_results", None)
        if events is None and isinstance(tr, dict):
            events = tr.get("event_results")
        for ev in (events or [])[:48]:
            if not isinstance(ev, dict):
                continue
            cap = str(ev.get("capability_id") or ev.get("event_kind") or "").strip()
            if cap not in TRACE_CAPS:
                continue
            summary = str(ev.get("summary") or ev.get("ai_reasoning") or "").strip()
            if not summary:
                continue
            line = f"{cap}：{summary[:160]}"
            if cap in ("inspect_session", "session_align"):
                session_bits.append(line)
            else:
                path_bits.append(line)
            if len(session_bits) + len(path_bits) >= 16:
                break
    out: List[Dict[str, Any]] = []
    if session_bits:
        out.append({
            "title": "入门：会话与登录壳",
            "content": "\n".join(session_bits[:6]),
            "category": "应用基础逻辑",
            "tags": ["入门", "轨迹"],
            "app_ids": [app_id] if app_id else [],
            "source": "trace",
            "source_ref": f"{app_id}:onboard:session",
            "review_status": "pending",
            "proposal_kind": "new_fact",
            "facet": "chrome",
            "situation": {"need": "howto", "lane": "prep", "screen_role": "auth_form"},
            "enabled": True,
            "question": "这些登录/会话观察是否仍适用于当前包？",
        })
    if path_bits:
        out.append({
            "title": "入门：已走过的界面",
            "content": "\n".join(path_bits[:8]),
            "category": "UI导航",
            "tags": ["入门", "轨迹"],
            "app_ids": [app_id] if app_id else [],
            "source": "trace",
            "source_ref": f"{app_id}:onboard:path",
            "review_status": "pending",
            "proposal_kind": "new_fact",
            "facet": "chrome",
            "situation": {"need": "howto", "lane": "prep"},
            "enabled": True,
            "question": "这些路径观察是否仍适用于当前包？",
        })
    return out


def _chrome_howto_count(items: Iterable[Dict[str, Any]], app_id: str) -> int:
    n = 0
    for row in items or []:
        if not item_is_live(row) or not _app_ok(row, app_id):
            continue
        facet = normalize_facet(row.get("facet"))
        need = normalize_situation(row.get("situation")).get("need")
        if facet == "chrome" or need in ("howto", "judge_selected"):
            n += 1
    return n


def bootstrap_from_traces(
    app_id: str,
    *,
    traces: Optional[List[Any]] = None,
    existing: Optional[List[Dict[str, Any]]] = None,
    persist_fn: Optional[PersistFn] = None,
    min_live: int = 2,
) -> List[Dict[str, Any]]:
    aid = str(app_id or "").strip()
    if not aid:
        return []
    pool = existing
    if pool is None and persist_fn is None:
        from server.services.system_settings_service import list_testing_knowledge

        pool = [x for x in list_testing_knowledge() if _app_ok(x, aid)]
    elif pool is None:
        pool = []
    if _chrome_howto_count(pool, aid) >= min_live:
        return []
    if any(str(x.get("source") or "") == "trace" and str(x.get("source_ref") or "").startswith(f"{aid}:onboard") for x in pool):
        return []
    loaded = traces
    if loaded is None:
        try:
            from server.core.database import SessionLocal
            from server.services.regression.case_memory.repo import list_run_traces

            db = SessionLocal()
            try:
                loaded = list_run_traces(db, app_id=aid, only_pass=True, limit=8)
            finally:
                db.close()
        except Exception as exc:
            SLog.w(TAG, f"bootstrap traces load failed {aid}: {exc}")
            loaded = []
    drafts = drafts_from_traces(loaded, app_id=aid)
    saved: List[Dict[str, Any]] = []
    for draft in drafts:
        row = _upsert_ingest(draft, pool, persist_fn)
        if row:
            saved.append(row)
    return saved


def ensure_app_facts(app_id: str, *, traces: bool = True) -> None:
    aid = str(app_id or "").strip()
    if not aid or aid in _ENSURED:
        return
    try:
        ingest_app_docs(aid)
        if traces:
            bootstrap_from_traces(aid)
    except Exception as exc:
        SLog.w(TAG, f"ensure_app_facts failed {aid}: {exc}")
    _ENSURED.add(aid)
