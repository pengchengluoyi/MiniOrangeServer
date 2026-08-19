# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""用例/任务结束后生成待审核知识草稿。未审核不得注入执行。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from script.log import SLog

from server.services.ai.regression import prompts as P
from server.services.ai.regression.llm_client import call_chat_text, resolve_regression_provider
from server.services.system_settings_service import upsert_knowledge_item

TAG = "KnowledgeCapture"
_CATEGORIES = {"业务逻辑", "UI导航", "登录注册", "Tab切换", "交互规范", "其他"}


def _step_lines(events: list, *, limit: int = 8) -> str:
    lines: list[str] = []
    for e in (events or [])[-limit:]:
        if not isinstance(e, dict):
            continue
        cap = e.get("capability_id") or e.get("event_kind") or ""
        st = e.get("status") or ""
        thought = str(e.get("ai_reasoning") or "")[:120]
        summary = str(e.get("summary") or e.get("error") or "")[:120]
        bit = f"- {st} {cap}"
        if thought:
            bit += f" | {thought}"
        if summary:
            bit += f" → {summary}"
        lines.append(bit)
    return "\n".join(lines) or "（无步骤）"


def _fallback_items(*, failed: bool, case_id: str, name: str, summary: str) -> list[dict[str, Any]]:
    if failed:
        return [{
            "title": f"失败处理：{case_id or name or '用例'}"[:48],
            "category": "业务逻辑",
            "tags": [case_id] if case_id else [],
            "question": "这种情况该如何操作？请补充正确路径或需要等待/点击的控件。",
            "content": (
                f"【失败现象】{summary or '用例未通过'}\n"
                f"【用例】{case_id} {name}\n"
                "【请补充】当前界面下正确的操作方式（点击哪里、等待什么、如何判断成功）。"
            ),
        }]
    return [{
        "title": f"界面事实：{case_id or name or '用例'}"[:48],
        "category": "业务逻辑",
        "tags": [case_id] if case_id else [],
        "question": "",
        "content": (
            f"【用例】{case_id} {name} 已通过。\n"
            f"【摘要】{summary or '—'}\n"
            "【请核对】本版本关键入口/文案/加载态是否仍准确，不准确请改后再审核。"
        ),
    }]


def _parse_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        items = raw.get("items") or raw.get("knowledge") or []
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    out: list[dict[str, Any]] = []
    for it in items[:3]:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        content = str(it.get("content") or "").strip()
        if not title or not content:
            continue
        cat = str(it.get("category") or "").strip() or "其他"
        if cat not in _CATEGORIES:
            cat = "其他"
        tags = [str(t).strip() for t in (it.get("tags") or []) if str(t).strip()][:8]
        out.append({
            "title": title[:48],
            "category": cat,
            "tags": tags,
            "content": content[:4000],
            "question": str(it.get("question") or "").strip()[:200],
        })
    return out


def _ask_llm(context: str, *, provider_id: str = "") -> list[dict[str, Any]]:
    provider, gate = resolve_regression_provider(provider_id or None)
    if provider is None:
        SLog.w(TAG, f"skip capture, no provider: {gate.get('reason')}")
        return []
    messages = P.build_knowledge_capture_messages(context=context)
    raw, meta = call_chat_text(
        provider=provider, messages=messages,
        temperature=0.2, max_tokens=1200, timeout_sec=45,
    )
    if raw is None:
        SLog.w(TAG, f"capture LLM failed: {meta.get('error')}")
        return []
    return _parse_items(raw)


def _persist(
    drafts: list[dict[str, Any]],
    *,
    app_id: str,
    source: str,
    origin_task_id: str = "",
    origin_case_id: str = "",
) -> list[dict[str, Any]]:
    saved: list[dict[str, Any]] = []
    for draft in drafts:
        row = {
            **draft,
            "app_ids": [app_id] if app_id else [],
            "enabled": True,
            "source": source,
            "review_status": "pending",
            "origin_task_id": origin_task_id,
            "origin_case_id": origin_case_id,
        }
        try:
            saved.append(upsert_knowledge_item(row))
        except Exception as exc:
            SLog.w(TAG, f"upsert draft failed: {exc}")
    return saved


def capture_case_knowledge(
    *,
    app_id: str,
    task_id: str,
    case_id: str,
    case_name: str = "",
    status: str = "",
    summary: str = "",
    events: Optional[list] = None,
    provider_id: str = "",
) -> list[dict[str, Any]]:
    failed = str(status or "").lower() not in ("pass", "success", "done")
    context = (
        f"范围：单条用例结束后的知识草稿\n"
        f"用例：{case_id} {case_name}\n"
        f"结果：{status}\n"
        f"摘要：{summary}\n"
        f"最近步骤：\n{_step_lines(list(events or []))}\n"
    )
    drafts = _ask_llm(context, provider_id=provider_id)
    if not drafts:
        drafts = _fallback_items(failed=failed, case_id=case_id, name=case_name, summary=summary)
    return _persist(
        drafts, app_id=app_id, source="case_run",
        origin_task_id=task_id, origin_case_id=case_id,
    )


def capture_task_knowledge(
    *,
    app_id: str,
    task_id: str,
    cases: Optional[list] = None,
    provider_id: str = "",
) -> list[dict[str, Any]]:
    rows = [c for c in (cases or []) if isinstance(c, dict)]
    if not rows:
        return []
    lines = []
    for c in rows[:40]:
        lines.append(
            f"- {c.get('case_id')} {c.get('status')} {(c.get('summary') or '')[:80]}"
        )
    context = (
        f"范围：整次任务结束后的汇总知识（跨用例共性，不要逐条复述）\n"
        f"任务：{task_id}\n"
        f"用例结果：\n" + "\n".join(lines)
    )
    drafts = _ask_llm(context, provider_id=provider_id)
    if not drafts:
        return []
    return _persist(
        drafts, app_id=app_id, source="task_run",
        origin_task_id=task_id,
    )
