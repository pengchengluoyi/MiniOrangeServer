# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""VLM 执行通道：跑 assert_visual / wait_screen_ready / persona_subtask 等"看图"事件。

它和 AdbExecutor/RemoteExecutor 互补：
  - Adb/Remote 负责"做"（点、滑、按、装）
  - VLM 负责"看"（看到、确认、定位）

注意：tap_element / input_text / long_press_element / swipe_element_to_element
本身不在 VLM 这里执行，它们由 Router 在分发前调 locate_element() 把坐标注入到 event.params，
再交给 adb 或 remote。所以本 executor 只处理"纯视觉"事件。
"""
from __future__ import annotations

import time
from typing import Any

from script.log import SLog

from server.services.ai.regression.planner import assert_visual
from server.services.ai.regression.schemas import EventResult, EventStatus, PlanEvent
from server.services.regression.executors.base import (
    Executor,
    ExecutorContext,
    _now_iso,
    make_event_result,
)
from server.services.regression.screen import screenshot_failure_meta

TAG = "VlmExecutor"

_SUPPORTED_CAPS: set[str] = {
    "assert_visual",
    "wait_screen_ready",
    "persona_subtask",  # Step 7 才真接 PERSONA_TASK，先 stub 返回 DECLINED
}


class VlmExecutor:
    id = "vlm"

    def supports(self, capability_id: str) -> bool:
        return capability_id in _SUPPORTED_CAPS

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        started_at = _now_iso()
        t0 = time.time()
        cap = event.capability_id
        try:
            if ctx.screen is None or not ctx.screen.has_image():
                err = (ctx.screen.error if ctx.screen else "no screen captured")
                return self._fail(event, started_at, t0, f"VLM 事件无可用截图: {err}", ctx=ctx)

            if cap == "assert_visual":
                return self._assert_visual(event, ctx, started_at, t0)
            if cap == "wait_screen_ready":
                return self._wait_screen_ready(event, ctx, started_at, t0)
            if cap == "persona_subtask":
                return self._persona_subtask(event, ctx, started_at, t0)
            return self._fail(event, started_at, t0, f"VlmExecutor 不处理 capability={cap}", ctx=ctx)
        except Exception as e:
            SLog.e(TAG, f"execute exception cap={cap}: {e}")
            return self._fail(event, started_at, t0, f"exception: {e}", ctx=ctx)

    def _assert_visual(self, event, ctx, started_at, t0):
        params = event.params or {}
        expectation = params.get("expectation") or params.get("description") or event.label or ""
        ai_hint = event.ai_reasoning or ""
        result = assert_visual(
            expectation=expectation,
            image_base64=ctx.screen.image_base64,
            image_mime=ctx.screen.image_mime,
            ai_hint=ai_hint,
        )
        elapsed = int((time.time() - t0) * 1000)
        status = EventStatus.PASS if result.passed else EventStatus.FAIL
        summary = (
            f"断言成立: {result.evidence[:80]}" if result.passed
            else f"断言失败: {result.ai_reasoning[:80]}"
        )
        return make_event_result(
            event, status=status, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=summary,
            error=("" if result.passed else result.ai_reasoning[:240]),
            vlm_meta={
                "kind": "assert_visual",
                "confidence": result.confidence,
                "evidence": result.evidence,
                "ai_reasoning": result.ai_reasoning,
                "parse_warnings": result.parse_warnings,
            },
            screenshot_path=ctx.screen.image_path,
        )

    def _wait_screen_ready(self, event, ctx, started_at, t0):
        """等当前屏幕"看起来稳定 / 是预期界面"。Step 4 实现 = assert_visual 复用。

        params:
          expectation: "进入首页"等期望描述
          poll_interval_ms: 默认 800
          max_wait_ms: 默认 5000
        Step 4：只看一次（不重复抓图），Step 4b 接 capture_screen 轮询。
        """
        params = event.params or {}
        expectation = params.get("expectation") or params.get("description") or event.label or "屏幕进入预期状态"
        result = assert_visual(
            expectation=expectation,
            image_base64=ctx.screen.image_base64,
            image_mime=ctx.screen.image_mime,
            ai_hint=event.ai_reasoning or "",
        )
        elapsed = int((time.time() - t0) * 1000)
        if result.passed:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"屏幕就绪: {result.evidence[:80]}",
                vlm_meta={
                    "kind": "wait_screen_ready",
                    "confidence": result.confidence,
                    "evidence": result.evidence,
                    "ai_reasoning": result.ai_reasoning,
                },
                screenshot_path=ctx.screen.image_path,
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"屏幕未就绪: {result.ai_reasoning[:80]}",
            error=result.ai_reasoning[:240],
            vlm_meta={
                "kind": "wait_screen_ready",
                "confidence": result.confidence,
                "ai_reasoning": result.ai_reasoning,
            },
            screenshot_path=ctx.screen.image_path,
        )

    def _persona_subtask(self, event, ctx, started_at, t0):
        """Step 7 真接 PERSONA_TASK prompt。Step 4 占位：返回 DECLINED 让 orchestrator 触发 replan。"""
        return make_event_result(
            event, status=EventStatus.DECLINED, executor_used=self.id, started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000),
            summary="persona_subtask 未接入（Step 7 才实现）",
            error="persona_subtask is a stub in Step 4",
            screenshot_path=ctx.screen.image_path if ctx.screen else "",
        )

    def _fail(
        self,
        event,
        started_at,
        t0,
        msg: str,
        *,
        ctx: ExecutorContext | None = None,
    ) -> EventResult:
        vlm_meta: dict[str, Any] = {}
        if ctx is not None and ctx.screen is not None:
            vlm_meta["screenshot_capture"] = screenshot_failure_meta(ctx.screen)
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000), summary=msg, error=msg,
            vlm_meta=vlm_meta,
        )
