#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""多设备任务：覆盖方式的单元主键 / 领取队列（不碰真机）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.services.regression.case_runner import (  # noqa: E402
    _LOCK,
    _device_platform_of,
    _next_unit,
    _normalize_sns,
    _package_of,
    _report_run_id,
    _row_matches,
    _seed_unit,
    _task_platform_of,
)
from server.services.project_env import target_id_from_snapshot  # noqa: E402
from server.services.runtime.run_context import _is_ios_target, device_platform_kind  # noqa: E402

_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'OK' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f"  want {want!r}"))
    if not ok:
        _fails.append(name)


def test_normalize_and_keys() -> None:
    print("\n[sns / report_run_id]")
    check("去重保序", _normalize_sns("a", ["b", "a", "c"]), ["a", "b", "c"])
    check("once 主键", _report_run_id("cr-1", "login", sn="pixel", coverage="once"), "cr-1::login")
    check(
        "per_device 主键",
        _report_run_id("cr-1", "login", sn="pixel", coverage="per_device"),
        "cr-1::login::pixel",
    )


def test_seed_counts() -> None:
    print("\n[seed 执行单元数]")
    cases = [{"case_id": f"c{i}", "name": f"N{i}"} for i in range(6)]
    once = [_seed_unit("cr-x", c, i, sn="", coverage="once") for i, c in enumerate(cases)]
    matrix = [
        _seed_unit("cr-x", c, i, sn=sn, coverage="per_device")
        for sn in ("p8", "k70", "op12")
        for i, c in enumerate(cases)
    ]
    check("once 6 行", len(once), 6)
    check("once 尚未绑 sn", all(not r["sn"] for r in once), True)
    check("per_device 18 行", len(matrix), 18)
    login_rows = [r for r in matrix if r["case_id"] == "c0"]
    check("同一用例三台各一行", len(login_rows), 3)
    check("三份 report_run_id 不同", len({r["report_run_id"] for r in login_rows}), 3)


def test_claim_once_unique() -> None:
    print("\n[加速拆分领取：每条用例只领一次]")
    cases = [{"case_id": f"c{i}", "name": f"N{i}"} for i in range(6)]
    run_doc = {
        "coverage": "once",
        "cases": [_seed_unit("cr-x", c, i, sn="", coverage="once") for i, c in enumerate(cases)],
    }
    claimed: list[tuple[str, str]] = []
    with _LOCK:
        while True:
            # 轮流模拟三台机抢
            progressed = False
            for sn in ("p8", "k70", "op12"):
                row = _next_unit(run_doc, sn, "once")
                if row is None:
                    continue
                progressed = True
                claimed.append((row["case_id"], sn))
            if not progressed:
                break
    check("恰好 6 次领取", len(claimed), 6)
    check("case_id 不重复", len({c for c, _ in claimed}), 6)
    check("三台都领到过", len({sn for _, sn in claimed}) >= 2, True)
    leftover = [r for r in run_doc["cases"] if r["status"] == "pending" and not r.get("sn")]
    check("队列清空", leftover, [])


def test_row_match_per_device() -> None:
    print("\n[全机覆盖 upsert 按 report_run_id]")
    a = {"case_id": "login", "sn": "p8", "report_run_id": "cr-x::login::p8"}
    b = {"case_id": "login", "sn": "k70", "report_run_id": "cr-x::login::k70"}
    check("同用例不同机不匹配", _row_matches(a, b), False)
    check("同格子匹配", _row_matches(a, {"report_run_id": "cr-x::login::p8", "status": "pass"}), True)


def test_mixed_platform_helpers() -> None:
    print("\n[混平台：包名 / 探测 / 任务字段]")
    snap = {
        "android": {"package": "com.example.android"},
        "ios": {"bundle": "com.example.ios"},
    }
    check("android 包名", target_id_from_snapshot(snap, "android"), "com.example.android")
    check("ios bundle", target_id_from_snapshot(snap, "ios"), "com.example.ios")
    check("缺 ios 不回落到安卓包", target_id_from_snapshot({"android": {"package": "com.a"}}, "ios"), "")

    check("任务全安卓", _task_platform_of(["android", "android"]), "android")
    check("任务混选", _task_platform_of(["android", "ios"]), "mixed")
    check("任务全 iOS", _task_platform_of(["ios", "ios"]), "ios")

    check("device_type=android 不被 platform=ios 带偏", _is_ios_target("PIXEL8", "ios", "android"), False)
    check("真机 UDID 仍是 iOS", _is_ios_target("00008140-001879181139801C", "android", ""), True)
    check("kind: iphone", device_platform_kind("iphone"), "ios")
    check("kind: pixel", device_platform_kind("pixel 8"), "android")

    run_doc = {
        "platform": "mixed",
        "platforms_by_sn": {"p8": "android", "iphone": "ios"},
        "packages_by_platform": {"android": "com.a", "ios": "com.a.ios"},
        "package": "com.a",
        "coverage": "once",
        "cases": [_seed_unit("cr-x", {"case_id": "c0", "name": "N0"}, 0, sn="", coverage="once")],
    }
    check("p8 是 android", _device_platform_of(run_doc, "p8"), "android")
    check("iphone 是 ios", _device_platform_of(run_doc, "iphone"), "ios")
    check("ios 取 bundle", _package_of(run_doc, "ios"), "com.a.ios")
    with _LOCK:
        row = _next_unit(run_doc, "iphone", "once")
    check("领取时打下 device_platform", row.get("device_platform"), "ios")
    check("领取绑的 sn", row.get("sn"), "iphone")


if __name__ == "__main__":
    test_normalize_and_keys()
    test_seed_counts()
    test_claim_once_unique()
    test_row_match_per_device()
    test_mixed_platform_helpers()
    if _fails:
        print(f"\nFAILED {len(_fails)}: {', '.join(_fails)}")
        sys.exit(1)
    print("\nALL PASSED")
    sys.exit(0)
