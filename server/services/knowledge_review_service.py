# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""知识草稿机审：产品专家先给应用事实，知识审核员再过/驳。低置信留人工。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from script.log import SLog

from server.services.ai.regression.llm_client import call_chat_text, resolve_regression_provider
from server.services.ai.roles_catalog import get_role
from server.services.system_settings_service import (
    delete_knowledge_item,
    list_testing_knowledge,
    upsert_knowledge_item,
)

TAG = "KnowledgeReview"
CONFIDENCE_AUTO = 80


def _parse_verdict(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "").strip().lower()
    if action not in ("approve", "reject", "hold"):
        return None
    try:
        confidence = int(raw.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    return {
        "action": action,
        "confidence": max(0, min(100, confidence)),
        "reason": str(raw.get("reason") or "").strip()[:400],
    }


def _parse_expert(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    def _lines(key: str) -> list[str]:
        rows = raw.get(key) or []
        if isinstance(rows, str):
            return [rows.strip()][:6] if rows.strip() else []
        return [str(x).strip() for x in rows if str(x).strip()][:6]
    usable = raw.get("usable")
    return {
        "aligned": _lines("aligned"),
        "conflicts": _lines("conflicts"),
        "missing": _lines("missing"),
        "usable": usable is not False,
        "note": str(raw.get("note") or "").strip()[:400],
    }


def _flatten_atlas_paths(atlas: dict) -> list[str]:
    from server.services.ai.app_atlas import flatten_tree

    rows = []
    for item in flatten_tree(atlas)[:80]:
        path = str(item.get("path") or item.get("name") or "").strip()
        if path:
            rows.append(path)
    return rows


def _req_lines(requirements: list) -> list[str]:
    out: list[str] = []
    for req in requirements or []:
        if not isinstance(req, dict):
            continue
        title = str(req.get("title") or req.get("external_id") or "").strip()
        if not title:
            continue
        und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
        excerpt = str(und.get("source_excerpt") or "").strip().replace("\n", " ")[:80]
        ac = und.get("ac") if isinstance(und.get("ac"), list) else []
        ac0 = str(ac[0] or "").strip()[:40] if ac else ""
        bit = title
        if excerpt:
            bit += f"：{excerpt}"
        elif ac0:
            bit += f"：{ac0}"
        out.append(bit)
        if len(out) >= 16:
            break
    return out


def load_app_brief(app_id: str = "") -> str:
    aid = str(app_id or "").strip()
    if not aid:
        return "没有绑定具体应用。只根据知识正文判断，不要编造产品能力。"
    try:
        from sqlalchemy.orm import joinedload

        from server.core.database import SessionLocal
        from server.models.project import App
        from server.services.app_automation_service import get_automation_config

        db = SessionLocal()
        try:
            app = db.query(App).options(joinedload(App.project)).filter(App.id == aid).first()
            if not app:
                return f"应用 {aid} 不存在。只根据知识正文判断。"
            cfg = get_automation_config(app)
            qp = cfg.get("qa_process") or {}
            atlas = qp.get("app_atlas") or {}
            reqs = qp.get("requirements") or []
            paths = _flatten_atlas_paths(atlas)
            req_lines = _req_lines(reqs)
            known = [
                str(x.get("title") or "").strip()
                for x in list_testing_knowledge()
                if str(x.get("review_status") or "") == "approved"
                and aid in (x.get("app_ids") or [])
                and str(x.get("title") or "").strip()
            ][:12]
            lines = [
                f"应用：{app.name}（{aid}）",
                f"项目：{getattr(getattr(app, 'project', None), 'name', '') or '—'}",
                f"图谱路径：{'；'.join(paths) if paths else '还没有确认的骨架'}",
                f"需求：{'；'.join(req_lines) if req_lines else '还没有需求'}",
                f"已通过知识：{'；'.join(known) if known else '暂无'}",
            ]
            return "\n".join(lines)
        finally:
            db.close()
    except Exception as exc:
        SLog.w(TAG, f"load_app_brief failed {aid}: {exc}")
        return f"应用 {aid} 简报读取失败，只根据知识正文判断。"


def _item_block(item: dict[str, Any]) -> str:
    return (
        f"标题：{item.get('title') or ''}\n"
        f"分类：{item.get('category') or ''}\n"
        f"来源：{item.get('source') or ''}\n"
        f"标签：{', '.join(item.get('tags') or [])}\n"
        f"内容：\n{item.get('content') or ''}\n"
    )


def _chat_role(role_id: str, user: str, *, max_tokens: int = 600, timeout_sec: int = 45):
    role = get_role(role_id) or {}
    system = str(role.get("system_prompt") or "").strip()
    if not system:
        return None, {"error": f"{role_id} Prompt 缺失"}
    provider, gate = resolve_regression_provider()
    if provider is None:
        return None, gate
    from server.services.ai import dispatch_log as dispatch
    tok = dispatch.bind(
        trigger="knowledge_review",
        source="knowledge_review",
        role=role_id,
        job="knowledge-review",
        skill="knowledge-review",
    )
    try:
        parsed, meta = call_chat_text(
            provider=provider,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.1,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
        )
    finally:
        dispatch.reset(tok)
    return parsed, meta


def ask_product_expert(item: dict[str, Any], brief: str) -> dict[str, Any]:
    user = f"【应用简报】\n{brief}\n\n【待审知识】\n{_item_block(item)}"
    parsed, meta = _chat_role("product-expert", user, max_tokens=500, timeout_sec=35)
    if parsed is None:
        SLog.w(TAG, f"product-expert skip {item.get('id')}: {meta.get('error') or meta.get('reason')}")
        return {}
    return _parse_expert(parsed)


def _expert_block(expert: dict[str, Any]) -> str:
    if not expert:
        return "产品专家没有返回意见，请更谨慎，必要时 hold。"
    lines = [f"可用：{'是' if expert.get('usable') is not False else '否'}"]
    if expert.get("note"):
        lines.append(f"说明：{expert['note']}")
    if expert.get("aligned"):
        lines.append("对齐：" + "；".join(expert["aligned"]))
    if expert.get("conflicts"):
        lines.append("冲突：" + "；".join(expert["conflicts"]))
    if expert.get("missing"):
        lines.append("缺失：" + "；".join(expert["missing"]))
    return "\n".join(lines)


def _ask(item: dict[str, Any], *, brief: str = "", expert: Optional[dict[str, Any]] = None):
    user = (
        f"【应用简报】\n{brief or '没有应用简报'}\n\n"
        f"【产品专家意见】\n{_expert_block(expert or {})}\n\n"
        f"【待审知识】\n{_item_block(item)}"
    )
    parsed, meta = _chat_role("knowledge-reviewer", user)
    return _parse_verdict(parsed), meta


def _mark_task(item: dict[str, Any], status: str) -> None:
    origin = str(item.get("origin_task_id") or "").strip()
    kid = str(item.get("id") or "").strip()
    if not origin or not kid:
        return
    try:
        from server.services.regression import task_store

        task_store.mark_knowledge_proposal(origin, kid, status)
    except Exception as exc:
        SLog.w(TAG, f"mark_knowledge_proposal failed {origin} {kid}: {exc}")


def apply_verdict(item: dict[str, Any], verdict: dict[str, Any], *, expert: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    kid = str(item.get("id") or "").strip()
    action = verdict["action"]
    confidence = int(verdict.get("confidence") or 0)
    reason = str(verdict.get("reason") or "").strip()
    auto = action in ("approve", "reject") and confidence >= CONFIDENCE_AUTO
    expert = expert or {}
    meta = {
        "review_method": "machine",
        "review_decision": action if auto else "hold",
        "review_reason": reason or ("置信不足，留给人工" if not auto else ""),
        "review_confidence": confidence,
        "reviewed_by": "knowledge-reviewer",
        "expert_note": expert.get("note") or "",
        "expert_aligned": expert.get("aligned") or [],
        "expert_conflicts": expert.get("conflicts") or [],
    }
    if not auto:
        return upsert_knowledge_item({**item, "review_status": "pending", **meta})
    if action == "reject":
        if kid:
            delete_knowledge_item(kid)
        _mark_task(item, "rejected")
        return {**item, **meta, "review_status": "rejected", "deleted": True}
    row = upsert_knowledge_item({**item, "review_status": "approved", **meta})
    _mark_task(row, "approved")
    return row


def review_item(item: dict[str, Any], *, brief: str = "") -> dict[str, Any]:
    app_id = ""
    ids = item.get("app_ids") or []
    if isinstance(ids, list) and ids:
        app_id = str(ids[0] or "").strip()
    brief = brief or load_app_brief(app_id)
    expert = ask_product_expert(item, brief)
    verdict, meta = _ask(item, brief=brief, expert=expert)
    if not verdict:
        SLog.w(TAG, f"skip {item.get('id')}: {meta.get('error') or meta.get('reason') or 'no verdict'}")
        return item
    return apply_verdict(item, verdict, expert=expert)


def review_new_items(items: List[dict[str, Any]]) -> List[dict[str, Any]]:
    briefs: dict[str, str] = {}
    kept: List[dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        ids = item.get("app_ids") or []
        app_id = str(ids[0] or "").strip() if isinstance(ids, list) and ids else ""
        if app_id not in briefs:
            briefs[app_id] = load_app_brief(app_id)
        row = review_item(item, brief=briefs[app_id])
        if row.get("deleted"):
            continue
        kept.append(row)
    return kept


def review_pending(*, app_id: str = "") -> dict[str, Any]:
    aid = str(app_id or "").strip()
    pool = [x for x in list_testing_knowledge() if str(x.get("review_status") or "") == "pending"]
    if aid:
        pool = [x for x in pool if aid in (x.get("app_ids") or [])]
    briefs: dict[str, str] = {}
    out = {"approved": 0, "rejected": 0, "held": 0, "skipped": 0, "items": []}
    for item in pool:
        ids = item.get("app_ids") or []
        item_app = str(ids[0] or "").strip() if isinstance(ids, list) and ids else aid
        if item_app not in briefs:
            briefs[item_app] = load_app_brief(item_app)
        row = review_item(item, brief=briefs[item_app])
        method = str(row.get("review_method") or "")
        status = str(row.get("review_status") or "")
        if row.get("deleted") or status == "rejected":
            out["rejected"] += 1
        elif status == "approved" and method == "machine":
            out["approved"] += 1
        elif method == "machine":
            out["held"] += 1
        else:
            out["skipped"] += 1
        out["items"].append({
            "id": row.get("id") or item.get("id"),
            "title": row.get("title") or item.get("title"),
            "review_status": status,
            "review_decision": row.get("review_decision"),
            "review_confidence": row.get("review_confidence"),
            "review_reason": row.get("review_reason"),
            "expert_note": row.get("expert_note"),
        })
    return out
