#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""空白熔断：流式早停，避免等满 max_tokens。"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.ai.regression.llm_client import (  # noqa: E402
    _consume_sse_chat,
    has_json_object_key,
    looks_like_output_melt,
    repair_utf8_mojibake,
)


class _FakeResp:
    def __init__(self, lines):
        self.lines = list(lines)
        self.closed = False

    def iter_lines(self, decode_unicode=True):
        yield from self.lines

    def close(self):
        self.closed = True


def _sse(content: str, finish: str | None = None) -> str:
    chunk = {"choices": [{"delta": {"content": content}, "finish_reason": finish}]}
    return "data: " + json.dumps(chunk, ensure_ascii=False)


def main() -> int:
    fails = 0

    def check(cond, msg):
        nonlocal fails
        print(f"   {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            fails += 1

    melt = "{\n" + ("    \n" * 80)
    check(looks_like_output_melt(melt), "空白 { 后判定熔断")
    check(not looks_like_output_melt("{"), "短输出不误判")
    good = '{"thought": "当前是首页", "status": "continue"}'
    check(has_json_object_key(good), "正常 JSON 识别到键")
    check(not looks_like_output_melt(good + " " * 400), "有键的长 JSON 不熔断")

    meta: dict = {"provider_id": "mock"}
    lines = [_sse("{")] + [_sse(" \n    " * 20) for _ in range(8)] + ["data: [DONE]"]
    resp = _FakeResp(lines)
    out, meta = _consume_sse_chat(resp, meta, started=time.time(), timeout_sec=30)
    content = (((out or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    check(meta.get("fail_kind") == "melt", f"SSE 空白应 melt, got {meta.get('fail_kind')}")
    check(meta.get("aborted") is True, "熔断应 aborted")
    check(resp.closed, "熔断后关闭连接")
    check(looks_like_output_melt(content) or len(content) >= 200, f"已积累空白 len={len(content)}")

    meta2: dict = {"provider_id": "mock"}
    resp2 = _FakeResp([
        _sse('{"thought":'),
        _sse(' "ok", "status": "continue"}'),
        _sse("", finish="stop"),
        "data: [DONE]",
    ])
    out2, meta2 = _consume_sse_chat(resp2, meta2, started=time.time(), timeout_sec=30)
    content2 = (((out2 or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    check(not meta2.get("aborted"), "正常 JSON 不中止")
    check("status" in content2, f"正常 JSON 拼完整, got {content2!r}")

    chinese = '{"thought":"当前是应用首页"}'
    check(repair_utf8_mojibake(chinese) == chinese, "已是中文不误修")
    mojibake = chinese.encode("utf-8").decode("latin-1")
    check(repair_utf8_mojibake(mojibake) == chinese, f"latin-1 乱码应还原, got {repair_utf8_mojibake(mojibake)!r}")

    def _content(out):
        return (((out or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""

    meta3: dict = {"provider_id": "mock"}
    utf8_line = _sse(chinese).encode("utf-8")
    resp3 = _FakeResp([utf8_line, b"data: [DONE]"])
    out3, meta3 = _consume_sse_chat(resp3, meta3, started=time.time(), timeout_sec=30)
    check("当前是应用首页" in _content(out3), f"UTF-8 字节 SSE 应是中文, got {_content(out3)!r}")
    check(not meta3.get("aborted"), "UTF-8 中文 JSON 不中止")

    meta4: dict = {"provider_id": "mock"}
    latin1_line = _sse(chinese).encode("utf-8").decode("latin-1")
    resp4 = _FakeResp([latin1_line, "data: [DONE]"])
    out4, meta4 = _consume_sse_chat(resp4, meta4, started=time.time(), timeout_sec=30)
    check("当前是应用首页" in _content(out4), f"latin-1 误解码应还原, got {_content(out4)!r}")

    print("\n" + ("=== 全部通过 ===" if not fails else f"=== {fails} 条失败 ==="))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
