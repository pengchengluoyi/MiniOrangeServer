#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验 call_chat_text 对「可解析 JSON 失败」再打一轮。

场景对齐 2026-08-25 填点事故：第一次返回键名碎掉的伪 JSON，同预算再问一次常能自愈。
截断（finish_reason=length）不得在此重试。
"""
from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.ai.regression import llm_client as lc  # noqa: E402


def _resp(content: str, *, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }


PROVIDER = {"id": "mock", "base_url": "https://example.invalid", "api_key": "k", "model": "m"}
MESSAGES = [{"role": "user", "content": "x"}]

GARBLED = '{\n  "\n  : [{"\n    ,"text":"新用户点击推荐tab"\n'
GOOD = '{"points":[{"text":"新用户点击推荐tab","kind":"正向","detail":""}]}'
# 截断且没有任何完整元素 → salvage 也救不回；仍不得再打一轮
TRUNC = '{"points":[{"text":"a","kind":"正'


def _http_meta() -> dict[str, Any]:
    return {
        "provider_id": "mock",
        "model": "m",
        "http_status": 200,
        "elapsed_ms": 10,
        "error": "",
        "attempts": 1,
        "retry_reasons": [],
        "json_mode_downgraded": False,
    }


def main() -> int:
    fails = 0

    # 1) 第一次乱码 → 第二次修好
    calls = {"n": 0}

    def post_ok_then_good(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(GARBLED), _http_meta()
        return _resp(GOOD), _http_meta()

    with patch.object(lc, "_post_chat_completions", side_effect=post_ok_then_good):
        with patch.object(lc.time, "sleep", return_value=None):
            parsed, meta = lc.call_chat_text(provider=PROVIDER, messages=MESSAGES, parse_retries=1)
    if not (parsed and parsed.get("points") and "parse" in (meta.get("retry_reasons") or [])):
        print("FAIL 乱码后重试应成功且 retry_reasons 含 parse", parsed, meta.get("retry_reasons"))
        fails += 1
    else:
        print("OK   乱码 → 重试成功")

    # 2) 两次都坏 → 仍失败，打了 2 轮 HTTP
    calls["n"] = 0

    def post_always_bad(**kwargs):
        calls["n"] += 1
        return _resp(GARBLED), _http_meta()

    with patch.object(lc, "_post_chat_completions", side_effect=post_always_bad):
        with patch.object(lc.time, "sleep", return_value=None):
            parsed, meta = lc.call_chat_text(provider=PROVIDER, messages=MESSAGES, parse_retries=1)
    if parsed is not None or meta.get("fail_kind") != "parse" or calls["n"] != 2:
        print("FAIL 两次乱码应 fail_kind=parse 且 HTTP×2", parsed, meta.get("fail_kind"), calls["n"])
        fails += 1
    else:
        print("OK   两次乱码 → parse 失败（HTTP×2）")

    # 3) 截断不重试
    calls["n"] = 0

    def post_trunc(**kwargs):
        calls["n"] += 1
        return _resp(TRUNC, finish_reason="length"), _http_meta()

    with patch.object(lc, "_post_chat_completions", side_effect=post_trunc):
        with patch.object(lc.time, "sleep", return_value=None):
            parsed, meta = lc.call_chat_text(provider=PROVIDER, messages=MESSAGES, parse_retries=1)
    if calls["n"] != 1 or meta.get("fail_kind") != "truncated" or "parse" in (meta.get("retry_reasons") or []):
        print("FAIL 截断不应 parse-retry", calls["n"], meta.get("fail_kind"), meta.get("retry_reasons"))
        fails += 1
    else:
        print("OK   截断不重试")

    # 4) parse_retries=0 不重试
    calls["n"] = 0
    with patch.object(lc, "_post_chat_completions", side_effect=post_always_bad):
        with patch.object(lc.time, "sleep", return_value=None):
            parsed, meta = lc.call_chat_text(provider=PROVIDER, messages=MESSAGES, parse_retries=0)
    if calls["n"] != 1 or parsed is not None:
        print("FAIL parse_retries=0 应只打 1 次", calls["n"], parsed)
        fails += 1
    else:
        print("OK   parse_retries=0")

    if fails:
        print(f"\n{fails} failed")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
