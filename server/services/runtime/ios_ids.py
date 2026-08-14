# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""iOS 标识：区分真机 UDID、模拟器 UUID、Bonjour 垃圾名。"""
from __future__ import annotations

import re

# 模拟器 / CoreDevice _remotepairing 实例名：标准 UUID
_RFC4122 = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)
# 真机：旧 40 hex，或 A12+ 的 8-16（如 00008140-001879181139801C）
_APPLE_UDID = re.compile(
    r"^([0-9A-Fa-f]{40}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16})$"
)


def is_rfc4122_uuid(value: str) -> bool:
    return bool(_RFC4122.fullmatch(str(value or "").strip()))


def is_physical_ios_udid(value: str) -> bool:
    s = str(value or "").strip()
    if not s or is_rfc4122_uuid(s):
        return False
    return bool(_APPLE_UDID.fullmatch(s))


def is_executable_ios_sn(value: str) -> bool:
    """能作为新建执行目标的 iOS SN：真机 UDID，或 apple-mobdev 的 MAC 句柄。"""
    s = str(value or "").strip()
    if is_physical_ios_udid(s):
        return True
    if s.lower().startswith("ios-wifi-"):
        rest = s[9:]
        if is_rfc4122_uuid(rest) or is_rfc4122_uuid(s):
            return False
        return len(rest) >= 12
    return False
