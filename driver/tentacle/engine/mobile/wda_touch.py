# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""WDA 直连触控：兼容新版 WebDriverAgent（无 /wda/tap/0）"""
from __future__ import annotations

from typing import Any


def wda_tap(client: Any, x: int, y: int) -> None:
    """在坐标 (x, y) 点击。facebook-wda 旧版用 /wda/tap/0，新版为 /wda/tap。"""
    http = client._session_http
    last_err: Exception | None = None
    for path, payload in (
        ("/wda/tap", {"x": x, "y": y}),
        ("/wda/touchAndHold", {"x": x, "y": y, "duration": 0.02}),
    ):
        try:
            http.post(path, payload)
            return
        except Exception as e:
            last_err = e
    raise RuntimeError(f"WDA tap failed at ({x}, {y})") from last_err
