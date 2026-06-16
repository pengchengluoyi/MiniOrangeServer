# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Copilot 执行层手势计数与延迟返回。"""
from __future__ import annotations

from script.log import SLog

TAG = "CopilotExecutor"

# 全局手势计数（用于「每 50 次点击/滑动最多按一次返回」）
_GESTURE_COUNT = 0
_BACK_PENDING = False
_BACK_FLUSH_EVERY = 50


def _gesture_tick() -> None:
    global _GESTURE_COUNT, _BACK_PENDING
    _GESTURE_COUNT += 1
    if _BACK_PENDING and _GESTURE_COUNT >= _BACK_FLUSH_EVERY:
        _flush_back()


def _schedule_back() -> None:
    global _BACK_PENDING
    _BACK_PENDING = True
    _gesture_tick()


def _flush_back() -> None:
    global _GESTURE_COUNT, _BACK_PENDING
    if not _BACK_PENDING:
        return
    try:
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
        import builtins

        sn = getattr(builtins, "TARGET_DEVICE_SN", None)
        if sn:
            engine, _ = bootstrap_mobile_engine(str(sn), "android")
            if hasattr(engine, "press_key"):
                SLog.i(TAG, "Copilot deferred back key")
                engine.press_key("back")
    except Exception as e:
        SLog.w(TAG, f"deferred back failed: {e}")
    _BACK_PENDING = False
    _GESTURE_COUNT = 0
