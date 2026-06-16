# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""记录业务 Plan 点击未命中。"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List

TAG = "CopilotExecutor"

def _record_plan_attempt_miss(
    results: List[Dict[str, Any]],
    *,
    step_index: int,
    kind: str,
    summary: str,
    click_attempt: int,
    r: Dict[str, Any],
    t0: float,
) -> None:
    """记录一次业务 Plan 点击未命中，供回放按序对齐截图与时间戳。"""
    try:
        from server.services.shared.run_context.regression_run_context import capture_trace_frame, stamp_run_timing

        attempt_out: Dict[str, Any] = {
            "index": step_index,
            "kind": kind,
            "summary": summary,
            "ok": False,
            "msg": r.get("msg") or "点击未命中",
            "method": r.get("method") or "",
            "phase": "plan_attempt",
            "click_attempt": click_attempt,
            "locate_debug": r.get("locate_debug"),
            "screen_size": r.get("screen_size"),
            "started_at": datetime.fromtimestamp(t0).isoformat(timespec="milliseconds"),
            "duration_ms": int((time.time() - t0) * 1000),
        }
        miss_shot = capture_trace_frame(
            f"plan_attempt_{step_index}_{click_attempt}",
            settle_ms=80,
        )
        if miss_shot:
            attempt_out["screenshot_before"] = miss_shot
            attempt_out["screenshot_after"] = miss_shot
        stamp_run_timing(attempt_out)
        results.append(attempt_out)
    except Exception:
        pass
