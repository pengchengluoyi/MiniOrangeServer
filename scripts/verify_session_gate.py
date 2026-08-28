#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验登录态闸门：已登录失败、微信 untestable、游客已登录失败。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.runtime.session_gate import (  # noqa: E402
    evaluate_gate,
    is_wechat_untestable,
    required_session,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> None:
    _assert(required_session("已登录，清缓存") == "logged_in", "comma still logged_in")
    _assert(required_session("已登录\n清缓存") == "logged_in", "require logged_in")
    _assert(required_session("游客浏览首页") == "guest", "require guest")
    _assert(required_session("清缓存") == "any", "no session req")

    _assert(is_wechat_untestable("使用微信登录 打开微信"), "wechat only")
    _assert(not is_wechat_untestable("手机号登录 验证码 微信登录"), "phone still available")

    fail = evaluate_gate({
        "required": "logged_in",
        "observed": "guest",
        "wechat_untestable": False,
        "reason": "当前仍在登录页",
    })
    _assert(fail["status"] == "fail" and not fail["ok"], fail)

    unt = evaluate_gate({
        "required": "logged_in",
        "observed": "guest",
        "wechat_untestable": True,
        "reason": "微信",
    })
    _assert(unt["status"] == "untestable", unt)

    guest = evaluate_gate({
        "required": "guest",
        "observed": "logged_in",
        "wechat_untestable": False,
    })
    _assert(guest["status"] == "fail" and "退出" in guest["reason"], guest)

    ok = evaluate_gate({"required": "any", "observed": "unknown"})
    _assert(ok["ok"], ok)
    print("ok: session gate fail / untestable / guest")


if __name__ == "__main__":
    main()
