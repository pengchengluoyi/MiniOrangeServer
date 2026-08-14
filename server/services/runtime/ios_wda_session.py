# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""按 UDID 复用 IOSEngine / WDA 会话，供截图与 ios_wda executor 共用。"""
from __future__ import annotations

from typing import Any

from script.log import SLog

TAG = "IosWdaSession"
_engines: dict[str, Any] = {}


def get_ios_engine(udid: str):
    key = str(udid or "").strip()
    if not key:
        raise ValueError("empty ios udid")
    eng = _engines.get(key)
    if eng is not None and getattr(eng, "driver", None) is not None:
        return eng
    from driver.tentacle.engine.mobile.mIOS import IOSEngine

    eng = IOSEngine()
    eng.init_driver(test_subject=key)
    _engines[key] = eng
    SLog.i(TAG, f"WDA session ready udid={key[:12]}…")
    return eng
