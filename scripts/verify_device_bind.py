#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""BE-ISO：两个 worker 线程交错执行时 SN 不得串台。"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.services.runtime.device_bind import current_device_sn, device_scope  # noqa: E402

_fails: list[str] = []
_seen: dict[str, list[str]] = {"a": [], "b": []}
_lock = threading.Lock()


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'OK' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f"  want {want!r}"))
    if not ok:
        _fails.append(name)


def _worker(name: str, sn: str, ticks: int) -> None:
    with device_scope(sn):
        for _ in range(ticks):
            got = current_device_sn()
            with _lock:
                _seen[name].append(got)
            if got != sn:
                break
            time.sleep(0.01)


def test_two_workers_do_not_cross() -> None:
    print("\n[device_scope 双线程交错]")
    t1 = threading.Thread(target=_worker, args=("a", "pixel-8", 20), daemon=True)
    t2 = threading.Thread(target=_worker, args=("b", "redmi-k70", 20), daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    with _lock:
        a = list(_seen["a"])
        b = list(_seen["b"])
    check("A 线程始终是 pixel-8", set(a), {"pixel-8"})
    check("B 线程始终是 redmi-k70", set(b), {"redmi-k70"})
    check("A 采到了足够样本", len(a) >= 10, True)
    check("B 采到了足够样本", len(b) >= 10, True)


def test_nested_scope_restores() -> None:
    print("\n[嵌套 scope 恢复]")
    with device_scope("outer"):
        check("进入 outer", current_device_sn(), "outer")
        with device_scope("inner"):
            check("进入 inner", current_device_sn(), "inner")
        check("回到 outer", current_device_sn(), "outer")
    check("离开后清空", current_device_sn(), "")


if __name__ == "__main__":
    test_two_workers_do_not_cross()
    test_nested_scope_restores()
    if _fails:
        print(f"\nFAILED {len(_fails)}: {', '.join(_fails)}")
        sys.exit(1)
    print("\nALL PASSED")
    sys.exit(0)
