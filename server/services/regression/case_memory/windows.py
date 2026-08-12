# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""三段窗口 + plan 总览的构造。

入口
====
- baseline_snippets_from_brief(events_brief) → list[BaselineSnippet]
  把 m_case_baseline.events_brief（dict 列表）还原成 Pydantic 模型。

- BaselineOverview / build_overview_text(...)
  PLAN_OVERVIEW 阶段塞给 AI 的"紧凑总览"（看结构、不被字段绑死）。

- build_baseline_window(...)
  SINGLE_STEP_REPLAN 阶段：基于 alignment，挑出 previous/current/next 三段，
  返回 BaselineContext（schemas.py 里已有的类型）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from server.services.ai.regression.schemas import BaselineContext, BaselineSnippet

# ---------- 还原 ----------


def baseline_snippets_from_brief(events_brief: list[dict[str, Any]]) -> list[BaselineSnippet]:
    """events_brief（来自 MCaseBaseline）→ list[BaselineSnippet]。
    宽容字段缺失；不抛异常，仅过滤掉完全空的项。
    """
    out: list[BaselineSnippet] = []
    if not events_brief:
        return out
    for raw in events_brief:
        if not isinstance(raw, dict):
            continue
        out.append(
            BaselineSnippet(
                seq=int(raw.get("seq") or 0),
                capability_id=str(raw.get("capability_id") or ""),
                event_kind=str(raw.get("event_kind") or ""),
                status=str(raw.get("status") or "unknown"),
                params=raw.get("params") or {},
                summary=str(raw.get("summary") or ""),
                executor_used=str(raw.get("executor_used") or ""),
                ai_reasoning=str(raw.get("ai_reasoning") or ""),
                elapsed_ms=int(raw.get("elapsed_ms") or 0),
            )
        )
    return out


# ---------- PLAN 总览 ----------


@dataclass
class BaselineOverview:
    """喂给 PLAN_OVERVIEW_TEXT 的紧凑总览，独立于 BaselineContext（后者是 prev/curr/next 三段）。"""

    case_id: str = ""
    device_signature: str = ""
    overall_status: str = ""  # 上次整 case 的结果
    event_count: int = 0
    events_brief_text: str = ""  # 已渲染好的文本块
    last_ai_reasoning: str = ""  # 上次 plan 的总体 reasoning
    blessed_at: str = ""

    def to_prompt_block(self) -> str:
        if not self.events_brief_text:
            return ""
        header = (
            f"上次执行（baseline）: status={self.overall_status} events={self.event_count}"
            + (f" @ {self.blessed_at}" if self.blessed_at else "")
        )
        parts: list[str] = [header]
        if self.last_ai_reasoning:
            parts.append(f"last_ai_reasoning: {self.last_ai_reasoning}")
        parts.append("events:")
        parts.append(self.events_brief_text)
        return "\n".join(parts)


def build_overview_text(snippets: list[BaselineSnippet], *, max_lines: int = 30) -> str:
    """把 baseline 事件序列折叠成一段易读文本。"""
    if not snippets:
        return ""
    lines: list[str] = []
    for s in snippets[:max_lines]:
        step = f"[step={s.seq}]"
        cap = s.capability_id
        ex = f"({s.executor_used})" if s.executor_used else ""
        status_tag = f"<{s.status}>"
        summary = s.summary or s.ai_reasoning or ""
        if summary and len(summary) > 80:
            summary = summary[:79] + "…"
        lines.append(f"  {step} {cap}{ex} {status_tag} {summary}".rstrip())
    if len(snippets) > max_lines:
        lines.append(f"  ... and {len(snippets) - max_lines} more events")
    return "\n".join(lines)


def build_baseline_overview(
    *,
    case_id: str,
    device_signature: str,
    overall_status: str,
    snippets: list[BaselineSnippet],
    last_ai_reasoning: str = "",
    blessed_at: str = "",
) -> BaselineOverview:
    return BaselineOverview(
        case_id=case_id,
        device_signature=device_signature,
        overall_status=overall_status,
        event_count=len(snippets),
        events_brief_text=build_overview_text(snippets),
        last_ai_reasoning=last_ai_reasoning or "",
        blessed_at=blessed_at or "",
    )


# ---------- 三段窗口（用于 REPLAN） ----------


def build_baseline_window(
    *,
    baseline_snippets: list[BaselineSnippet],
    alignment: list[Optional[int]],
    current_index: int,
    overall_status: str = "",
    notes: str = "",
) -> BaselineContext:
    """根据 alignment（current → baseline），算出 current_index 这条事件的前/当前/下一段。

    - previous: alignment[current_index-1] 指向的 baseline snippet（或第一条之前的 None）
    - current : alignment[current_index]
    - next    : alignment[current_index+1]
    None 表示对齐缺失（baseline 里没这一条）。
    """
    n_cur = len(alignment)
    n_base = len(baseline_snippets)

    def _pick(idx_in_current: int) -> Optional[BaselineSnippet]:
        if not (0 <= idx_in_current < n_cur):
            return None
        bi = alignment[idx_in_current]
        if bi is None or not (0 <= bi < n_base):
            return None
        return baseline_snippets[bi]

    return BaselineContext(
        previous=_pick(current_index - 1),
        current=_pick(current_index),
        next=_pick(current_index + 1),
        case_overall_status=overall_status or "",
        notes=notes or "",
    )
