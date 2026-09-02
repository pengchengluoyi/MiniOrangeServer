# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""iOS 执行通道：经 IOSEngine / IOSAppiumEngine，不直连 facebook-wda 客户端 API。"""
from __future__ import annotations

import time

from script.log import SLog

from server.services.ai.regression.schemas import EventResult, EventStatus, PlanEvent
from server.services.regression.executors.base import (
    ExecutorContext,
    _now_iso,
    make_event_result,
)
from server.services.runtime.ios_wda_session import get_ios_engine

TAG = "IosWdaExecutor"

_SUPPORTED_CAPS: set[str] = {
    "launch_app",
    "close_app",
    "press_key",
    "wait_ms",
    "swipe_direction",
    "swipe_element_to_element",
    "tap_element",
    "multi_tap",
    "long_press_element",
    "input_text",
}


class IosWdaExecutor:
    id = "ios_wda"

    def supports(self, capability_id: str) -> bool:
        return capability_id in _SUPPORTED_CAPS

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        started_at = _now_iso()
        t0 = time.time()
        cap = event.capability_id
        udid = str(ctx.run_context.ios.get("udid") or ctx.run_context.sn or "")
        try:
            if not udid:
                return self._fail(event, started_at, t0, "iOS UDID 为空")
            engine = get_ios_engine(udid)
            if getattr(engine, "driver", None) is None:
                return self._fail(event, started_at, t0, "iOS driver 未就绪")

            if cap == "wait_ms":
                ms = int((event.params or {}).get("duration_ms") or 0)
                time.sleep(max(0, ms) / 1000.0)
                return self._ok(event, started_at, t0, f"等待 {ms}ms")
            if cap == "tap_element":
                x, y = self._xy(event)
                engine.click(None, (x, y))
                return self._ok(event, started_at, t0, f"tap ({x},{y})")
            if cap == "multi_tap":
                from server.services.regression.executors.multi_tap import parse_multi_tap

                parsed, err = parse_multi_tap(event.params)
                if err:
                    return self._fail(event, started_at, t0, err)
                x, y, count, interval = parsed
                for i in range(count):
                    engine.click(None, (x, y))
                    if i + 1 < count:
                        time.sleep(interval / 1000.0)
                return self._ok(event, started_at, t0, f"连点 ({x},{y}) ×{count}")
            if cap == "long_press_element":
                x, y = self._xy(event)
                dur = float((event.params or {}).get("duration_ms") or 800) / 1000.0
                engine.long_press(None, (x, y), duration=dur)
                return self._ok(event, started_at, t0, f"long_press ({x},{y})")
            if cap == "swipe_direction":
                direction = str((event.params or {}).get("direction") or "up").lower()
                engine.swipe_ext(direction)
                return self._ok(event, started_at, t0, f"swipe {direction}")
            if cap == "swipe_element_to_element":
                p = event.params or {}
                x1, y1 = int(p.get("from_x") or p.get("x") or 0), int(p.get("from_y") or p.get("y") or 0)
                x2, y2 = int(p.get("to_x") or p.get("x2") or 0), int(p.get("to_y") or p.get("y2") or 0)
                w, h = engine.screen_size()
                if w <= 0 or h <= 0:
                    return self._fail(event, started_at, t0, "无法读取屏幕尺寸")
                engine.swipe_norm(x1 / w, y1 / h, x2 / w, y2 / h, 0.5)
                return self._ok(event, started_at, t0, f"swipe ({x1},{y1})→({x2},{y2})")
            if cap == "input_text":
                text = str((event.params or {}).get("text") or "")
                try:
                    x, y = self._xy(event)
                    engine.click(None, (x, y))
                    time.sleep(0.2)
                except ValueError:
                    pass
                engine._type_text(text)
                return self._ok(event, started_at, t0, f"input {text[:24]}")
            if cap == "press_key":
                key = str((event.params or {}).get("key") or (event.params or {}).get("keycode") or "home").lower()
                mapped = {
                    "home": "home",
                    "back": "home",
                    "volumeup": "volume_up",
                    "volumedown": "volume_down",
                    "volume_up": "volume_up",
                    "volume_down": "volume_down",
                }.get(key, "home")
                engine.press_key(mapped)
                return self._ok(event, started_at, t0, f"press {mapped}")
            if cap == "launch_app":
                bundle = str((event.params or {}).get("package") or (event.params or {}).get("bundle") or "")
                if not bundle:
                    return self._fail(event, started_at, t0, "launch_app 缺 package/bundle")
                engine.start_app(bundle)
                return self._ok(event, started_at, t0, f"launch {bundle}")
            if cap == "close_app":
                bundle = str((event.params or {}).get("package") or (event.params or {}).get("bundle") or "")
                if bundle:
                    engine.stop_app(bundle)
                return self._ok(event, started_at, t0, f"close {bundle or 'app'}")
            return self._fail(event, started_at, t0, f"IosWdaExecutor 不处理 {cap}")
        except Exception as e:
            SLog.e(TAG, f"execute exception cap={cap} udid={udid}: {e}")
            return self._fail(event, started_at, t0, f"exception: {e}")

    def _xy(self, event: PlanEvent) -> tuple[int, int]:
        p = event.params or {}
        if p.get("x") is None or p.get("y") is None:
            raise ValueError("缺坐标 x/y")
        return int(p["x"]), int(p["y"])

    def _ok(self, event, started_at, t0, summary: str) -> EventResult:
        return make_event_result(
            event,
            status=EventStatus.PASS,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000),
            summary=summary,
        )

    def _fail(self, event, started_at, t0, error: str) -> EventResult:
        return make_event_result(
            event,
            status=EventStatus.FAIL,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000),
            summary=error,
            error=error,
        )
