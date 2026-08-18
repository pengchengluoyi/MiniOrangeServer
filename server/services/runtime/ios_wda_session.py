# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""按 UDID 复用 iOS 引擎会话（backend 由 IOS_BACKEND 决定），供截图与 ios_wda executor 共用。"""
from __future__ import annotations

from typing import Any

from script.log import SLog

TAG = "IosWdaSession"
_engines: dict[str, Any] = {}


def _session_alive(eng: Any) -> bool:
    """Appium/WDA 客户端对象还在，不代表远端 session 还活着。必须真正打一枪。"""
    driver = getattr(eng, "driver", None)
    if driver is None:
        return False
    try:
        if hasattr(driver, "get_window_size"):
            size = driver.get_window_size()
            return bool(size)
        if hasattr(driver, "window_size"):
            driver.window_size()
            return True
        return bool(getattr(driver, "session_id", None))
    except Exception as e:
        SLog.w(TAG, f"cached iOS session dead: {e}")
        return False


def get_ios_engine(udid: str):
    key = str(udid or "").strip()
    if not key:
        raise ValueError("empty ios udid")
    from driver.tentacle.engine.mobile.mIOS import create_ios_engine, resolve_ios_backend

    backend = resolve_ios_backend()
    cache_key = f"{backend}:{key}"
    eng = _engines.get(cache_key)
    if eng is not None and _session_alive(eng):
        return eng
    if eng is not None:
        SLog.w(TAG, f"stale iOS session, recreating backend={backend} udid={key[:12]}…")
        eng.reset_session()
        return eng

    eng = create_ios_engine(backend)
    eng.init_driver(test_subject=key)
    _engines[cache_key] = eng
    SLog.i(TAG, f"iOS session ready backend={backend} udid={key[:12]}…")
    return eng
