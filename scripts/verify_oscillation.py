#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""P0 验收脚本：震荡检测（落点容差 + 感知哈希）。

原实现要求连续 3 步「capability + str(params) + 整图 sha1」三者全等，实测两个条件都过严：
  - VLM 每次给的坐标差几像素（VIEW-007 实测同一按钮七种坐标）→ params 永不相等；
  - 状态栏时钟 / 加载动画每帧变 → 整图 sha1 永不相等。
结果它只能抓纯黑屏。本脚本验证改造后的判定，并给出真机标定数据。

用法：
    .venv/bin/python scripts/verify_oscillation.py            # 仅离线
    .venv/bin/python scripts/verify_oscillation.py 5fda2f6d   # 追加真机标定

退出码 0 = 全通过，1 = 有失败项。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.services.ai.regression.schemas import CaseGoal  # noqa: E402
from server.services.regression.agent_executor import (  # noqa: E402
    AgentExecutor,
    AgentOptions,
    _phash_distance,
    _screen_phash,
    _Step,
)
from server.services.runtime.run_context import RunContext  # noqa: E402

_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: {got!r}" + ("" if ok else f"  期望 {want!r}"))
    if not ok:
        _fails.append(name)


def make_executor(**opt_kw) -> AgentExecutor:
    """构造一个只用于判定逻辑的 executor（不跑循环、不碰设备）。"""
    return AgentExecutor(
        goal=CaseGoal(goal="verify"),
        run_context=RunContext(sn="test"),
        router=None,  # type: ignore[arg-type]
        options=AgentOptions(**opt_kw),
    )


def steps_from(coords: list[tuple[int, int]], phashes: list[str],
               cap: str = "tap_element") -> list[_Step]:
    out = []
    for i, ((x, y), ph) in enumerate(zip(coords, phashes), 1):
        out.append(_Step(idx=i, capability_id=cap, params={"x": x, "y": y}, phash=ph))
    return out


# VIEW-007 真实坐标序列（cr-898b203890ac，同一个缩略图连点 8 次）
_VIEW007 = [(455, 2094), (450, 2081), (462, 2094), (456, 2086),
            (460, 2089), (461, 2092), (462, 2092), (462, 2094)]
_SAME = "8c88cccc8c43c3a2"     # 真机实测：同一屏的 phash
_OTHER = "1e3f77a09b5c2d41"    # 另一屏（构造，与上者距离远）


def test_action_sig() -> None:
    print("\n[动作比较：非坐标参数全等 + 落点容差]")
    ex = make_executor()
    st = steps_from(_VIEW007, [_SAME] * len(_VIEW007))
    check("VIEW-007 八次落点两两都在容差内", all(ex._coords_close(st[0], s) for s in st), True)
    check("非坐标部分签名一致", len({ex._action_key(s) for s in st}), 1)

    far = steps_from([(455, 2094), (900, 500)], [_SAME, _SAME])
    check("相距很远的两点不算同一目标", ex._coords_close(far[0], far[1]), False)

    near_miss = steps_from([(400, 2094), (455, 2094)], [_SAME, _SAME])
    check("相邻缩略图（差 55px > 容差 48）不算同一目标",
          ex._coords_close(near_miss[0], near_miss[1]), False)

    a = _Step(idx=1, capability_id="tap_element", params={"target": {"text": "社区"}, "x": 233, "y": 2494})
    b = _Step(idx=2, capability_id="tap_element", params={"target": {"text": "社区"}, "x": 236, "y": 2490})
    check("带锚点时 key 相同且落点相近",
          ex._action_key(a) == ex._action_key(b) and ex._coords_close(a, b), True)

    c = _Step(idx=3, capability_id="swipe_direction", params={"direction": "up"})
    check("不同能力 key 不同", ex._action_key(a) != ex._action_key(c), True)
    d = _Step(idx=4, capability_id="swipe_direction", params={"direction": "down"})
    check("同能力不同参数 key 不同", ex._action_key(c) != ex._action_key(d), True)


def test_phash_math() -> None:
    print("\n[感知哈希距离]")
    check("自身距离 0", _phash_distance(_SAME, _SAME), 0)
    check("不同屏距离大", _phash_distance(_SAME, _OTHER) > 6, True)
    check("空值 → -1（未知）", _phash_distance("", _SAME), -1)
    check("非法十六进制 → -1", _phash_distance("zzz", _SAME), -1)
    check("空图 → 空哈希", _screen_phash(""), "")
    check("坏 base64 → 空哈希（不抛异常）", _screen_phash("!!!not-base64!!!"), "")


def test_oscillation() -> None:
    print("\n[震荡判定]")
    ex = make_executor()

    ex.steps = steps_from(_VIEW007[:3], [_SAME] * 3)
    check("同动作+同屏 3 步 → 判卡死", ex._is_oscillating(), True)

    ex.steps = steps_from(_VIEW007[:2], [_SAME] * 2)
    check("只有 2 步 → 不判", ex._is_oscillating(), False)

    ex.steps = steps_from(_VIEW007[:3], [_SAME, _SAME, _OTHER])
    check("屏幕变了 → 不判（正在推进）", ex._is_oscillating(), False)

    ex.steps = steps_from([(455, 2094), (900, 500), (455, 2094)], [_SAME] * 3)
    check("动作不同 → 不判", ex._is_oscillating(), False)

    ex.steps = steps_from(_VIEW007[:3], [_SAME, "", _SAME])
    check("phash 未知 → 不判（宁漏不误杀）", ex._is_oscillating(), False)

    # 阈值边界：距离刚好等于/超过 max_distance
    near = f"{int(_SAME, 16) ^ 0b111111:016x}"      # 距离 6
    far = f"{int(_SAME, 16) ^ 0b1111111:016x}"      # 距离 7
    ex.steps = steps_from(_VIEW007[:3], [_SAME, near, _SAME])
    check("距离=6（阈值内）→ 判卡死", ex._is_oscillating(), True)
    ex.steps = steps_from(_VIEW007[:3], [_SAME, far, _SAME])
    check("距离=7（超阈值）→ 不判", ex._is_oscillating(), False)

    ex2 = make_executor(oscillation_window=4)
    ex2.steps = steps_from(_VIEW007[:3], [_SAME] * 3)
    check("窗口=4 时 3 步不足 → 不判", ex2._is_oscillating(), False)

    ex.steps = [_Step(idx=1, capability_id="", phash=_SAME) for _ in range(3)]
    check("无 capability_id → 不判", ex._is_oscillating(), False)

    waits = [
        _Step(idx=i, capability_id="wait_ms", params={"ms": 3000}, phash=_SAME)
        for i in range(1, 4)
    ]
    ex.steps = waits
    check("连续 3 次 wait 同屏 → 不判（轮播/加载）", ex._is_oscillating(), False)

    asserts = [
        _Step(idx=i, capability_id="assert_visual", params={"expectation": "x"}, phash=_SAME)
        for i in range(1, 4)
    ]
    ex.steps = asserts
    check("连续 3 次 assert 同屏 → 不判", ex._is_oscillating(), False)

    mixed = [
        _Step(idx=1, capability_id="tap_element", params={"x": 455, "y": 2094}, phash=_SAME),
        _Step(idx=2, capability_id="wait_ms", params={"ms": 3000}, phash=_SAME),
        _Step(idx=3, capability_id="tap_element", params={"x": 450, "y": 2081}, phash=_SAME),
        _Step(idx=4, capability_id="wait_ms", params={"ms": 3000}, phash=_SAME),
        _Step(idx=5, capability_id="tap_element", params={"x": 462, "y": 2094}, phash=_SAME),
    ]
    ex.steps = mixed
    check("wait 夹心但 3 次同目标点击同屏 → 仍判卡死", ex._is_oscillating(), True)

    one_tap = [
        _Step(idx=1, capability_id="tap_element", params={"x": 455, "y": 2094}, phash=_SAME),
        *waits,
    ]
    ex.steps = one_tap
    check("1 次点击 + 多次 wait → 不判", ex._is_oscillating(), False)


def test_device(sn: str) -> None:
    print(f"\n[真机标定 sn={sn}]")
    import subprocess
    import time

    from server.services.regression.screen import capture_screen
    from server.services.runtime.run_context import build_run_context

    ctx = build_run_context(sn, platform="android", run_id="verify-osc",
                            probe_remote_channel=False, probe_vlm_channel=False,
                            probe_hitl_channel=False)
    if ctx.adb.get("state") != "connected":
        print(f"  ⚠️ adb 未连通（{ctx.adb.get('state')}），跳过真机部分")
        return

    def shot() -> str:
        return _screen_phash(capture_screen(ctx, prefer=("adb",), force_fresh=True).image_base64)

    def statusbar(action: str) -> None:
        subprocess.run(["adb", "-s", sn, "shell", "cmd", "statusbar", action], capture_output=True)

    # 用通知栏开合造出「屏幕确实变了」，与被测应用无关、不依赖列表滚动位置
    a = shot()
    time.sleep(1.0)
    b = shot()
    d_same = _phash_distance(a, b)
    check("同一静态屏距离 ≤6", d_same <= 6, True)

    statusbar("expand-notifications")
    time.sleep(1.5)
    c = shot()
    d_change = _phash_distance(b, c)
    check("屏幕真变了 → 距离 >6", d_change > 6, True)

    statusbar("collapse")
    time.sleep(1.5)
    d = shot()
    d_back = _phash_distance(b, d)
    check("恢复原屏 → 距离 ≤6（认得出是同一屏）", d_back <= 6, True)

    print(f"  ℹ️  标定：同屏={d_same} 变化={d_change} 复原={d_back}"
          f"（阈值 6 落在两者之间的间隙里）")
    print("  ℹ️  历史实测参考：切换 Tab=25，滚动一屏=33，列表到底后再滑=0（真·空操作）")


def main() -> int:
    print("=== P0 验收：震荡检测 ===")
    test_action_sig()
    test_phash_math()
    test_oscillation()
    if len(sys.argv) > 1:
        test_device(sys.argv[1])
    else:
        print("\n（未指定 sn，跳过真机部分；用法：verify_oscillation.py <sn>）")

    print("\n" + "=" * 46)
    if _fails:
        print(f"❌ {len(_fails)} 项失败：{_fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
