# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""内置执行通道：不依赖 adb/remote，用于 wait_ms 等纯本地等待。"""
from __future__ import annotations

import time

from server.services.ai.regression.schemas import EventResult, EventStatus, PlanEvent
from server.services.regression.executors.base import (
    ExecutorContext,
    _now_iso,
    make_event_result,
)

_SUPPORTED_CAPS = {"wait_ms"}


class InternalExecutor:
    id = "internal"

    def supports(self, capability_id: str) -> bool:
        return capability_id in _SUPPORTED_CAPS

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        started_at = _now_iso()
        t0 = time.time()
        if event.capability_id != "wait_ms":
            return make_event_result(
                event,
                status=EventStatus.DECLINED,
                executor_used=self.id,
                started_at=started_at,
                elapsed_ms=0,
                summary=f"internal 不处理 {event.capability_id}",
            )
        ms = int((event.params or {}).get("duration_ms") or (event.params or {}).get("ms") or 500)
        ms = max(0, min(ms, 60_000))
        if ms > 0:
            time.sleep(ms / 1000.0)
        return make_event_result(
            event,
            status=EventStatus.PASS,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000),
            summary=f"等待 {ms}ms",
        )
