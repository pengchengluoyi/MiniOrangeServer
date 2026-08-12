# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Executor 接口与共享执行上下文。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from server.services.ai.regression.schemas import (
    EventResult,
    EventStatus,
    PlanEvent,
)
from server.services.regression.screen import CapturedScreen
from server.services.runtime.run_context import RunContext


@dataclass
class ExecutorContext:
    """传给每个 Executor.execute() 的上下文容器。"""

    run_context: RunContext
    run_id: str = ""
    case_id: str = ""

    # 若 Router 在分发前抓了截图（needs_vlm=True 的事件），放在这里
    screen: Optional[CapturedScreen] = None

    # Router 抓图时使用的通道优先级（adb/remote）。executor 需在派发中途重新抓图
    # （如 persona 打开应用详情页后刷新截图）时沿用同一优先级。
    capture_prefer: tuple[str, ...] = ("adb", "remote")

    # 可被 Executor 共享的 KV（如 last_locate_result，下个事件可复用）
    shared: dict[str, Any] = field(default_factory=dict)

    # 用例上下文摘要（喂给 HITL composer / persona prompt 等）。
    # 由 Orchestrator 一次性构造，避免每个 executor 自己拼。
    case_brief: str = ""

    # 路由器把"选中的 Implementation 元数据"塞进来（Step 7 起被 AiPersonaExecutor 使用，
    # 用于取 prompt_template / expands_to_events 等字段）。dict 形态避免循环依赖 plugins.models。
    selected_impl: Optional[dict[str, Any]] = None

    # 子事件 dispatch 回调（Step 7 起被 AiPersonaExecutor 使用）。
    # 形参是单条 PlanEvent，返回 EventResult；底层就是 router.dispatch 的瘦封装。
    dispatch_subevent: Optional[Callable[[PlanEvent], "EventResult"]] = None


@runtime_checkable
class Executor(Protocol):
    """所有执行通道的统一接口。"""

    id: str  # adb / remote / vlm / hitl / ai_persona

    def supports(self, capability_id: str) -> bool:
        """该 executor 是否处理这条 capability。Router 会用它做硬校验。"""
        ...

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        """跑这条事件，返回 EventResult。

        必须捕获自身异常并返回 status=FAIL，不要抛出去；
        若需要人工介入返回 status=BLOCKED；
        若是 AI 主动放弃返回 status=DECLINED。
        """
        ...


# ============== 共享辅助 ==============


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def make_event_result(
    event: PlanEvent,
    *,
    status: EventStatus,
    executor_used: str,
    started_at: str,
    elapsed_ms: int,
    summary: str = "",
    error: str = "",
    vlm_meta: Optional[dict[str, Any]] = None,
    screenshot_path: str = "",
    raw_response: Optional[dict[str, Any]] = None,
) -> EventResult:
    """统一构造 EventResult，避免每个 executor 重复填字段。"""
    return EventResult(
        seq=event.seq,
        capability_id=event.capability_id,
        event_kind=event.event_kind or event.capability_id,
        status=status,
        executor_used=executor_used,
        elapsed_ms=elapsed_ms,
        summary=summary,
        error=error,
        ai_reasoning=event.ai_reasoning or "",
        plan_event=event.model_dump(exclude_none=True),
        vlm_meta=vlm_meta or {},
        screenshot_path=screenshot_path,
        raw_response=raw_response or {},
        started_at=started_at,
        finished_at=_now_iso(),
    )
