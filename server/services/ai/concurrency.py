# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""LLM 调用的有界并发。

全仓以前是串行的：脑图三个端、用例十几批，墙钟直接相加。批次之间彼此独立，
本来就能并行；缺的是一个**会把 contextvar 带过去**的小工具。

必须 copy_context：`app_profile.bind` / `dispatch_log.bind` 都挂在 contextvar 上，
线程池默认是空上下文。漏了这一步，并发生成的 prompt 会丢掉应用术语表，
看起来像「并发把质量打差了」，其实是画像没跟过去。

每个任务单独 copy 一份。同一个 Context 对象不能被两个线程同时 enter
（CPython 会抛 RuntimeError: cannot enter context）。
"""
from __future__ import annotations

import contextvars
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable, Optional

DEFAULT_WORKERS = 6


def llm_workers(requested: int | None = None) -> int:
    if requested is not None:
        return max(1, int(requested))
    raw = str(os.environ.get("MINIORANGE_LLM_WORKERS") or "").strip()
    if raw.isdigit():
        return max(1, min(16, int(raw)))
    return DEFAULT_WORKERS


def map_llm(
    fn: Callable[[Any], Any],
    items: Iterable[Any],
    *,
    workers: int | None = None,
    on_partial: Optional[Callable[[int, Any], None]] = None,
) -> list:
    """按原顺序返回每个 item 的结果。单项抛错不会拖垮其余，对应位置放该异常。

    on_partial(index, result) 在每一项完成时回调（完成顺序，不是输入顺序），
    给流式落库用。
    """
    rows = list(items)
    if not rows:
        return []
    n = len(rows)
    n_workers = min(llm_workers(workers), n)
    out: list[Any] = [None] * n

    def capture(i: int, item: Any) -> tuple[int, Any]:
        try:
            return i, fn(item)
        except Exception as exc:  # noqa: BLE001 — 调用方要的就是「这项失败、别的继续」
            return i, exc

    def store(i: int, value: Any) -> None:
        out[i] = value
        if on_partial is not None:
            on_partial(i, value)

    if n_workers == 1:
        for i, item in enumerate(rows):
            _, value = capture(i, item)
            store(i, value)
        return out

    # 在父线程里为每个任务拍一份上下文快照，再交给线程池。
    snapshots = [contextvars.copy_context() for _ in rows]
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = [pool.submit(snapshots[i].run, capture, i, rows[i]) for i in range(n)]
        for fut in as_completed(futs):
            i, value = fut.result()
            store(i, value)
    return out


__all__ = ["DEFAULT_WORKERS", "llm_workers", "map_llm"]
