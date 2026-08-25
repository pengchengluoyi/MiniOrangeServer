#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验 map_llm：顺序、隔离失败、contextvar 跟到子线程。

并发把质量打差的头号原因不是线程本身，是 app_profile / dispatch_log 挂在
contextvar 上、线程池默认是空上下文。这个脚本如果绿了，画像才会跟过去。
"""
from __future__ import annotations

import contextvars
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.ai.concurrency import map_llm  # noqa: E402
from server.services.ai import app_profile as ap  # noqa: E402

FLAG: contextvars.ContextVar[str] = contextvars.ContextVar("verify_map_llm", default="")


def main() -> int:
    failed = []

    def check(cond, msg):
        print(f"   {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failed.append(msg)

    print("── 顺序与单项失败隔离")
    def work(x):
        if x == 2:
            raise ValueError("boom")
        return x * 10

    out = map_llm(work, [1, 2, 3, 4], workers=3)
    check(out[0] == 10 and out[2] == 30 and out[3] == 40, f"成功项按原顺序：{out}")
    check(isinstance(out[1], ValueError) and "boom" in str(out[1]), f"失败项留下异常：{out[1]!r}")

    print("\n── contextvar 跟到子线程（画像绑定的同一条路径）")
    tok = FLAG.set("from-parent")
    seen = []

    def read_flag(_):
        seen.append(FLAG.get())
        return FLAG.get()

    got = map_llm(read_flag, range(5), workers=4)
    FLAG.reset(tok)
    check(all(v == "from-parent" for v in got), f"子线程读到父线程的值：{got}")
    check(FLAG.get() == "", "父线程 reset 后不受子线程影响")

    print("\n── 应用画像跟到子线程")
    tok = ap.bind(package="com.mathmagic.zaohaowu")
    try:
        keys = map_llm(lambda _: ap.current().key, range(4), workers=3)
    finally:
        ap.reset(tok)
    check(keys == ["zaohaowu"] * 4, f"子线程拿到造好物画像：{keys}")
    check(ap.current().key == "_default", "reset 后父线程回到默认")

    print("\n── on_partial 会被调用")
    hits = []
    map_llm(lambda x: x, [7, 8, 9], workers=2, on_partial=lambda i, v: hits.append((i, v)))
    check(sorted(hits) == [(0, 7), (1, 8), (2, 9)], f"on_partial：{hits}")

    print("\n── 单线程路径（workers=1）也走同一套返回")
    out = map_llm(lambda x: x + 1, [1, 2], workers=1)
    check(out == [2, 3], f"workers=1：{out}")

    print("\n── 并发比串行快（本机粗测，避免假绿）")
    def sleepy(_):
        time.sleep(0.15)
        return 1

    t0 = time.monotonic()
    map_llm(sleepy, range(4), workers=4)
    parallel = time.monotonic() - t0
    check(parallel < 0.45, f"4 路 0.15s 睡眠墙钟 {parallel:.2f}s（串行会 ≈0.6s）")

    print("\n" + ("=== 全部通过 ===" if not failed else f"=== {len(failed)} 条失败 ==="))
    for m in failed:
        print(f"  · {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
