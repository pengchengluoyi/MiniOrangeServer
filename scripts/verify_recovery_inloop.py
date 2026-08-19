#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""验收：L0 系统层恢复接入 agent 主循环。

关键点是**成本**：预筛只用已经算出来的画面统计与停滞计数，正常屏每步 0 额外设备调用；
只有可疑时才花一次取证去查 YAML 规则。

用法：
    .venv/bin/python scripts/verify_recovery_inloop.py            # 仅离线
    .venv/bin/python scripts/verify_recovery_inloop.py 5fda2f6d   # 追加真机：熄屏 → 主循环自愈

⚠️ 真机部分会熄屏再唤醒解锁（无密码设备）。不调用 LLM。
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
    _screen_signal,
    _ScreenSignal,
)
from server.services.runtime.run_context import RunContext  # noqa: E402

_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: {got!r}" + ("" if ok else f"  期望 {want!r}"))
    if not ok:
        _fails.append(name)


def make_ex(**opt_kw) -> AgentExecutor:
    return AgentExecutor(
        goal=CaseGoal(goal="verify"),
        run_context=RunContext(sn="test"),
        router=None,  # type: ignore[arg-type]
        options=AgentOptions(**opt_kw),
    )


def test_screen_signal() -> None:
    print("\n[画面信号：一次解码同时给 phash 与全黑/全白]")
    import base64
    import io

    import numpy as np
    from PIL import Image

    def b64(arr) -> str:
        buf = io.BytesIO()
        Image.fromarray(arr.astype("uint8")).save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode()

    black = _screen_signal(b64(np.zeros((200, 100))))
    white = _screen_signal(b64(np.full((200, 100), 255)))
    noise = _screen_signal(b64(np.random.RandomState(0).rand(200, 100) * 255))
    check("全黑识别", black.blank, "black")
    check("全白识别", white.blank, "white")
    check("正常画面", noise.blank, "no")
    check("同时给出 phash", len(noise.phash), 16)
    check("空图 → unknown", _screen_signal("").blank, "unknown")
    check("坏图不抛异常", _screen_signal("!!!bad!!!").blank, "unknown")


def test_prefilter() -> None:
    print("\n[廉价预筛：什么情况下才值得花一次取证]")
    ok = _ScreenSignal(phash="a" * 16, blank="no")

    ex = make_ex()
    check("开场必查一次", ex._recovery_suspicion(ok), "case_start")
    ex._checked_at_start = True
    check("正常屏不查", ex._recovery_suspicion(ok), "")
    check("全黑要查", ex._recovery_suspicion(_ScreenSignal(phash="b" * 16, blank="black")), "blank_black")
    check("全白要查", ex._recovery_suspicion(_ScreenSignal(phash="b" * 16, blank="white")), "blank_white")

    ex.opts.recovery_stall_steps = 3
    ex._stall_steps = 2
    check("停滞 2 步还不查", ex._recovery_suspicion(ok), "")
    ex._stall_steps = 3
    check("停滞 3 步要查", ex._recovery_suspicion(ok), "stalled_3")

    ex._stall_steps = 0
    ex._recovery_rounds = 3
    check("轮数用尽后不再查（代码止损）",
          ex._recovery_suspicion(_ScreenSignal(phash="c" * 16, blank="black")), "")

    off = make_ex(recovery_enabled=False)
    check("开关关掉后完全不查", off._recovery_suspicion(_ScreenSignal(blank="black")), "")


def test_stall_tracking() -> None:
    print("\n[停滞计数：复用已算好的 phash]")
    ex = make_ex()
    same = "8c88cccc8c43c3a2"
    other = "1e3f77a09b5c2d41"
    ex._last_phash = same
    ex._track_stall(same)
    check("屏幕没变 → 计数 1", ex._stall_steps, 1)
    ex._last_phash = same
    ex._track_stall(same)
    check("再没变 → 计数 2", ex._stall_steps, 2)
    ex._last_phash = same
    ex._track_stall(other)
    check("屏幕变了 → 归零", ex._stall_steps, 0)
    ex._last_phash = ""
    ex._track_stall(same)
    check("上一帧未知 → 不计数", ex._stall_steps, 0)


def test_maybe_recover_paths() -> None:
    print("\n[恢复分支：recovered / advise / 用尽轮数]")
    from server.services.regression import recovery as R

    original = R.recover_if_needed
    signal = _ScreenSignal(phash="d" * 16, blank="black")

    def stub(outcome):
        def _f(ctx, router, **kw):
            return outcome
        return _f

    try:
        # ① 恢复成功 → 主循环应重新观察
        ex = make_ex()
        R.recover_if_needed = stub(R.RecoveryOutcome(
            rule_id="screen_asleep_or_locked", mode="deterministic",
            applied=True, recovered=True, attempts=1))
        got = ex._maybe_recover(signal, 1, "")
        check("恢复成功 → recovered=True", got.get("recovered"), True)
        check("记进报告命中列表", [p["id"] for p in ex._recovery_hits], ["screen_asleep_or_locked"])
        check("写了一条 trace 事件", len(ex.results), 1)
        check("事件不占决策预算", ex._decision_used, 0)
        check("给业务 agent 留了提示", any("系统层恢复过" in m[1] for m in ex._memory), True)

        # ② advise 规则：只记录，不改流程（按约定未接注入）
        ex = make_ex()
        R.recover_if_needed = stub(R.RecoveryOutcome(
            rule_id="system_permission_dialog", mode="advise",
            advice="这是系统权限框，优先点允许"))
        got = ex._maybe_recover(signal, 1, "")
        check("advise 不触发重新观察", got.get("recovered"), False)
        check("advise 也记录命中", len(ex._recovery_hits), 1)

        # ③ 没有规则命中 → 交给业务决策，不报错
        ex = make_ex()
        R.recover_if_needed = stub(None)
        check("无命中 → 返回 None", ex._maybe_recover(signal, 1, ""), None)
        check("无命中不写 trace", len(ex.results), 0)

        # ④ 反复恢复不成功 → 用尽轮数后判 device_unhealthy
        ex = make_ex(max_recovery_rounds=2)
        ex._checked_at_start = True
        R.recover_if_needed = stub(R.RecoveryOutcome(
            rule_id="screen_asleep_or_locked", mode="deterministic",
            applied=True, recovered=False, attempts=2, error="仍然黑屏"))
        first = ex._maybe_recover(signal, 1, "")
        check("第 1 轮未恢复但不致命", first.get("fatal"), None)
        second = ex._maybe_recover(signal, 2, "")
        check("第 2 轮用尽 → device_unhealthy", second.get("fatal"), "device_unhealthy")
        check("轮数不会再涨", ex._recovery_suspicion(signal), "")

        # ⑤ 恢复流程本身抛异常不该拖垮用例
        ex = make_ex()
        def boom(ctx, router, **kw):
            raise RuntimeError("probe exploded")
        R.recover_if_needed = boom
        check("恢复异常被吞掉", ex._maybe_recover(signal, 1, ""), None)
    finally:
        R.recover_if_needed = original


def test_device(sn: str) -> None:
    print(f"\n[真机：熄屏 → 主循环自愈（不调 LLM）sn={sn}]")
    import subprocess
    import time

    from server.services.regression import recovery as R
    from server.services.regression.router import CapabilityRouter
    from server.services.regression.screen import capture_screen
    from server.services.runtime.run_context import build_run_context

    PKG = "com.mathmagic.magicam"
    ctx = build_run_context(sn, platform="android", run_id="verify-inloop", target_package=PKG,
                            probe_remote_channel=False, probe_vlm_channel=False,
                            probe_hitl_channel=False)
    if ctx.adb.get("state") != "connected":
        print(f"  ⚠️ adb 未连通（{ctx.adb.get('state')}），跳过真机部分")
        return
    router = CapabilityRouter(ctx, capture_prefer=("adb",))
    ex = AgentExecutor(goal=CaseGoal(goal="verify"), run_context=ctx, router=router,
                       run_id="verify-inloop", case_id="INLOOP")

    print("  … 人为熄屏")
    subprocess.run(["adb", "-s", sn, "shell", "input", "keyevent", "26"], capture_output=True)
    time.sleep(2.5)

    screen = capture_screen(ctx, prefer=("adb",), force_fresh=True)
    signal = _screen_signal(screen.image_base64)
    print(f"  ℹ️  熄屏后画面：blank={signal.blank} mean={signal.mean:.1f}")

    got = ex._maybe_recover(signal, 1, "")
    check("触发并恢复成功", bool(got and got.get("recovered")), True)
    check("命中的是息屏规则", [p["id"] for p in ex._recovery_hits], ["screen_asleep_or_locked"])
    check("落了一条 trace 事件", len(ex.results), 1)
    check("事件名标出规则", ex.results[0].capability_id, "recovery_screen_asleep_or_locked")
    check("不占业务决策预算", ex._decision_used, 0)

    ev = R.collect_evidence(ctx, router, target_package=PKG)
    check("设备确实恢复可用", ev.screen_blocked, "no")
    print(f"  ℹ️  恢复后证据：{ev.brief()}")
    print(f"  ℹ️  干预轮数：{ex._recovery_rounds}/{ex.opts.max_recovery_rounds}")


def main() -> int:
    print("=== 验收：L0 恢复接入主循环 ===")
    test_screen_signal()
    test_prefilter()
    test_stall_tracking()
    test_maybe_recover_paths()
    if len(sys.argv) > 1:
        test_device(sys.argv[1])
    else:
        print("\n（未指定 sn，跳过真机部分；用法：verify_recovery_inloop.py <sn>）")

    print("\n" + "=" * 46)
    if _fails:
        print(f"❌ {len(_fails)} 项失败：{_fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
