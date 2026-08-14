# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""iOS WebDriverAgent 执行通道（usbmuxd USB / 已配对 Wi‑Fi）。"""
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
            driver = engine.driver
            if driver is None:
                return self._fail(event, started_at, t0, "WDA driver 未就绪")

            if cap == "wait_ms":
                ms = int((event.params or {}).get("duration_ms") or 0)
                time.sleep(max(0, ms) / 1000.0)
                return self._ok(event, started_at, t0, f"等待 {ms}ms")
            if cap == "tap_element":
                x, y = self._xy(event)
                from driver.tentacle.engine.mobile.wda_touch import wda_tap
                wda_tap(driver, x, y)
                return self._ok(event, started_at, t0, f"tap ({x},{y})")
            if cap == "long_press_element":
                x, y = self._xy(event)
                dur = float((event.params or {}).get("duration_ms") or 800) / 1000.0
                driver.swipe(x, y, x, y, dur)
                return self._ok(event, started_at, t0, f"long_press ({x},{y})")
            if cap == "swipe_direction":
                return self._swipe_direction(event, driver, started_at, t0)
            if cap == "swipe_element_to_element":
                p = event.params or {}
                x1, y1 = int(p.get("from_x") or p.get("x") or 0), int(p.get("from_y") or p.get("y") or 0)
                x2, y2 = int(p.get("to_x") or p.get("x2") or 0), int(p.get("to_y") or p.get("y2") or 0)
                driver.swipe(x1, y1, x2, y2, 0.5)
                return self._ok(event, started_at, t0, f"swipe ({x1},{y1})→({x2},{y2})")
            if cap == "input_text":
                text = str((event.params or {}).get("text") or "")
                try:
                    x, y = self._xy(event)
                    from driver.tentacle.engine.mobile.wda_touch import wda_tap
                    wda_tap(driver, x, y)
                    time.sleep(0.2)
                except ValueError:
                    pass
                driver.send_keys(text)
                return self._ok(event, started_at, t0, f"input {text[:24]}")
            if cap == "press_key":
                key = str((event.params or {}).get("key") or (event.params or {}).get("keycode") or "home").lower()
                mapped = {"home": "home", "back": "home", "volumeup": "volumeUp", "volumedown": "volumeDown"}.get(key, "home")
                driver.press(mapped)
                return self._ok(event, started_at, t0, f"press {mapped}")
            if cap == "launch_app":
                bundle = str((event.params or {}).get("package") or (event.params or {}).get("bundle") or "")
                if not bundle:
                    return self._fail(event, started_at, t0, "launch_app 缺 package/bundle")
                driver.app_launch(bundle)
                return self._ok(event, started_at, t0, f"launch {bundle}")
            if cap == "close_app":
                bundle = str((event.params or {}).get("package") or (event.params or {}).get("bundle") or "")
                if bundle:
                    try:
                        driver.app_terminate(bundle)
                    except Exception:
                        driver.app_stop(bundle)
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

    def _swipe_direction(self, event, driver, started_at, t0) -> EventResult:
        direction = str((event.params or {}).get("direction") or "up").lower()
        try:
            sz = driver.window_size()
            w = int(getattr(sz, "width", None) or sz[0])
            h = int(getattr(sz, "height", None) or sz[1])
        except Exception:
            w, h = 390, 844
        cx, cy = int(w / 2), int(h / 2)
        span_x, span_y = int(w * 0.35), int(h * 0.35)
        mapping = {
            "up": (cx, cy + span_y, cx, cy - span_y),
            "down": (cx, cy - span_y, cx, cy + span_y),
            "left": (cx + span_x, cy, cx - span_x, cy),
            "right": (cx - span_x, cy, cx + span_x, cy),
        }
        x1, y1, x2, y2 = mapping.get(direction, mapping["up"])
        driver.swipe(x1, y1, x2, y2, 0.4)
        return self._ok(event, started_at, t0, f"swipe {direction}")

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
