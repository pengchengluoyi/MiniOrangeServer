#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验设备预置：Android 白名单 grant 解析、保留询问、iOS 不静默授权。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.runtime.device_provision import (  # noqa: E402
    GRANT_WHITELIST,
    parse_requested_runtime_permissions,
    wants_keep_permission_prompt,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> None:
    dump = """
        requested permissions:
          android.permission.CAMERA: granted=false
          android.permission.BIND_ACCESSIBILITY_SERVICE: granted=false
          android.permission.ACCESS_FINE_LOCATION
    """
    perms = parse_requested_runtime_permissions(dump)
    _assert("android.permission.CAMERA" in perms, perms)
    _assert("android.permission.ACCESS_FINE_LOCATION" in perms, perms)
    _assert("android.permission.CAMERA" in GRANT_WHITELIST, "camera should be grantable")
    _assert("android.permission.BIND_ACCESSIBILITY_SERVICE" not in GRANT_WHITELIST, "a11y must not grant")

    _assert(wants_keep_permission_prompt("保留权限询问，验证拒绝相机后的降级"), "keep prompt")
    _assert(wants_keep_permission_prompt("keep_permission_prompt"), "keep en")
    _assert(not wants_keep_permission_prompt("弹出权限后点击允许"), "mentioning dialog is not opt-out")
    _assert(not wants_keep_permission_prompt("已登录，清缓存"), "normal case")
    print("ok: device provision parse + keep-permission flag")


if __name__ == "__main__":
    main()
