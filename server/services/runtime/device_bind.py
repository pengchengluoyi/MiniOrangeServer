# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""线程内设备绑定：多 worker 禁止共用 builtins.TARGET_DEVICE_SN。

回归执行链路上仍有旧代码读写全局 SN。多机任务里每个 worker 进入时
`bind_device_sn(sn)`，本线程后续 `current_device_sn()` 只看到自己的设备。
不要往 builtins 上写：那会串台。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator, Optional

_tls = threading.local()


def bind_device_sn(sn: str) -> None:
    _tls.sn = str(sn or "").strip()


def current_device_sn() -> str:
    return str(getattr(_tls, "sn", "") or "").strip()


def clear_device_sn() -> None:
    if hasattr(_tls, "sn"):
        delattr(_tls, "sn")


@contextmanager
def device_scope(sn: str) -> Iterator[str]:
    prev: Optional[str] = getattr(_tls, "sn", None)
    bind_device_sn(sn)
    try:
        yield current_device_sn()
    finally:
        if prev is None:
            clear_device_sn()
        else:
            _tls.sn = prev
