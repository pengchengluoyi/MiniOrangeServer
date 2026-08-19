#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""S0 验收脚本：通用 low_level 执行契约。

验证"只加 capability yaml 就能新增能力"这条通路（见
docs/plan-skill-packs-and-console.md §3.1）以及它的安全边界。

用法：
    .venv/bin/python scripts/verify_low_level.py            # 仅离线检查
    .venv/bin/python scripts/verify_low_level.py 5fda2f6d   # 追加真机取证

离线部分不碰设备，可在 CI 跑；真机部分需要 adb 已连该 sn。
退出码 0 = 全通过，1 = 有失败项。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.services.regression.executors.low_level import (  # noqa: E402
    LowLevelError,
    assert_command_allowed,
    parse_output,
    render_template,
    run_low_level,
)

_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: {got!r}" + ("" if ok else f"  期望 {want!r}"))
    if not ok:
        _fails.append(name)


# 用显式标记决定 rc，避免桩函数分支顺序造成歧义
_RC = {"BAD": (3, "", "boom"), "MISSING": (1, "", "")}


def fake_shell(cmd: str) -> tuple[int, str, str]:
    for marker, ret in _RC.items():
        if marker in cmd:
            return ret
    return 0, "k1=v1 k2=v2\n", ""


def test_template() -> None:
    print("\n[模板渲染与参数校验]")
    check("正常渲染", render_template("pidof {package}", {"package": "com.a.b"}), "pidof com.a.b")
    for name, params in (("缺参数", {}), ("注入字符", {"package": "a; rm -rf /"})):
        try:
            render_template("pidof {package}", params)
            check(f"{name}应被拒", "放行了", "抛 LowLevelError")
        except LowLevelError:
            check(f"{name}被拒", True, True)


def test_command_gate() -> None:
    print("\n[命令安全闸门]")
    blocked = [
        ("重定向", "dumpsys power > /sdcard/x"),
        ("命令分隔", "input tap 1 1; rm -rf /sdcard"),
        ("管道到 sh", "echo x | sh"),
        ("非白名单首词", "curl http://evil.test"),
        ("rm", "rm -rf /data"),
        ("变量展开", "echo $SECRET"),
        ("逻辑与", "input tap 1 1 && reboot"),
    ]
    for name, cmd in blocked:
        try:
            assert_command_allowed(cmd)
            check(f"{name}应被拒", "放行了", "抛 LowLevelError")
        except LowLevelError:
            check(f"{name}被拒", True, True)
    # grep 管道是取证必需，必须放行
    try:
        assert_command_allowed("dumpsys power | grep -E 'mWakefulness='")
        check("grep 管道放行", True, True)
    except LowLevelError as exc:
        check("grep 管道放行", f"被拒: {exc}", True)


def test_parsers() -> None:
    print("\n[输出解析器]")
    # 一行多对：dumpsys 常见形态，必须全取（曾漏取只拿第一个）
    kv = parse_output("mShowingDream=false mDreamingLockscreen=false\nisKeyguardShowing=true\n",
                      "keyvalue_lines")
    check("一行多对全取", (kv.get("mShowingDream"), kv.get("mDreamingLockscreen")), ("false", "false"))
    check("多行合并", kv.get("isKeyguardShowing"), "true")
    check("lines", parse_output(" a \n\n b \n", "lines"), ["a", "b"])
    check("first_token", parse_output("12345 678\n", "first_token"), "12345")
    try:
        parse_output("x", "no_such_parser")
        check("未知解析器应被拒", "放行了", "抛 LowLevelError")
    except LowLevelError:
        check("未知解析器被拒", True, True)


def test_kinds() -> None:
    print("\n[三种 kind]")
    o = run_low_level({"kind": "shell", "shell": "getprop {p}", "parse": "first_token"},
                      {"p": "ro.product.model"}, fake_shell)
    check("shell 成功", (o.ok, o.data.get("result")), (True, "k1=v1"))

    o = run_low_level({"kind": "shell_seq", "summary": "预置完成", "steps": [
        {"shell": "settings put global window_animation_scale 0"},
        {"shell": "getprop ro.build.version.sdk", "name": "sdk", "parse": "first_token"},
    ]}, {}, fake_shell)
    check("shell_seq 成功", (o.ok, o.summary, len(o.commands)), (True, "预置完成", 2))

    o = run_low_level({"kind": "shell_seq", "steps": [
        {"shell": "settings put global a 0"},
        {"shell": "settings put global BAD 0"},
        {"shell": "settings put global c 0"},
    ]}, {}, fake_shell)
    check("shell_seq 中途失败即停", (o.ok, len(o.commands)), (False, 2))

    o = run_low_level({"kind": "shell_batch", "commands": [
        {"name": "ok1", "shell": "dumpsys power", "parse": "keyvalue_lines"},
        {"name": "missing", "shell": "pidof MISSING", "parse": "first_token", "allow_rc": [0, 1]},
        {"name": "bad", "shell": "dumpsys BAD"},
    ]}, {}, fake_shell)
    check("batch 部分成功仍可用", (o.ok, o.summary), (True, "采集 2/3 项"))
    check("batch 失败项单独标错", "error" in (o.data.get("bad") or {}), True)

    print("\n[allow_rc：非 0 也可能是事实]")
    o = run_low_level({"kind": "shell", "shell": "pidof MISSING", "allow_rc": [0, 1]}, {}, fake_shell)
    check("allow_rc 命中 rc=1", o.ok, True)
    o = run_low_level({"kind": "shell", "shell": "pidof MISSING"}, {}, fake_shell)
    check("默认只允许 rc=0", o.ok, False)

    print("\n[声明错误]")
    check("未知 kind", run_low_level({"kind": "wat"}, {}, fake_shell).ok, False)
    check("batch 缺 commands", run_low_level({"kind": "shell_batch"}, {}, fake_shell).ok, False)


def test_yaml_capability() -> None:
    print("\n[纯 yaml 能力已被识别]")
    from server.services.plugins import registry
    from server.services.regression.executors.adb_executor import AdbExecutor

    cap = registry.get_capability("probe_device_state")
    check("capability 已加载", cap is not None, True)
    if cap is None:
        return
    check("visible_to 仅 system", list(getattr(cap, "visible_to", [])), ["system"])
    impls = [i for i in cap.implementations if i.executor == "adb" and (i.low_level or {})]
    check("声明了 adb low_level", len(impls) == 1, True)
    check("executor.supports 认它", AdbExecutor().supports("probe_device_state"), True)
    check("不认不存在的能力", AdbExecutor().supports("no_such_capability_xyz"), False)


def test_device(sn: str) -> None:
    print(f"\n[真机取证 sn={sn}]")
    from server.services.ai.regression.schemas import PlanEvent
    from server.services.regression.router import CapabilityRouter
    from server.services.runtime.menu import available_menu_brief
    from server.services.runtime.run_context import build_run_context

    ctx = build_run_context(sn, platform="android", run_id="verify-low-level",
                            target_package="com.mathmagic.magicam",
                            probe_remote_channel=False, probe_vlm_channel=False,
                            probe_hitl_channel=False)
    if ctx.adb.get("state") != "connected":
        print(f"  ⚠️ adb 未连通（{ctx.adb.get('state')}），跳过真机部分")
        return

    case_ids = {c["id"] for c in available_menu_brief(ctx, audience="case")}
    sys_ids = {c["id"] for c in available_menu_brief(ctx, audience="system")}
    check("业务菜单不含系统专用能力", "probe_device_state" in case_ids, False)
    check("系统菜单含它", "probe_device_state" in sys_ids, True)
    check("普通能力两边都在", "tap_element" in case_ids and "tap_element" in sys_ids, True)

    router = CapabilityRouter(ctx, capture_prefer=("adb",))
    ev = PlanEvent(seq=1, capability_id="probe_device_state", event_kind="probe_device_state",
                   params={"package": "com.mathmagic.magicam"}, needs_vlm=False,
                   ai_reasoning="S0 verify", label="取证")
    res = router.dispatch(ev, run_id="verify", case_id="S0", case_brief="", shared={})
    low = (res.raw_response or {}).get("low_level") or {}
    check("dispatch 成功", str(res.status.value), "pass")
    check("采集到电源状态", bool((low.get("power") or {}).get("mWakefulness")), True)
    check("采集到锁屏状态", "isKeyguardShowing" in (low.get("keyguard") or {}), True)
    check("采集到前台 Activity", bool(low.get("foreground")), True)
    print(f"  ℹ️  证据快照：wakefulness={(low.get('power') or {}).get('mWakefulness')} "
          f"keyguard={(low.get('keyguard') or {}).get('isKeyguardShowing')} "
          f"target_pid={low.get('target_pid')!r} anr={low.get('anr_window')!r}")


def main() -> int:
    print("=== S0 验收：通用 low_level 执行契约 ===")
    test_template()
    test_command_gate()
    test_parsers()
    test_kinds()
    test_yaml_capability()
    if len(sys.argv) > 1:
        test_device(sys.argv[1])
    else:
        print("\n（未指定 sn，跳过真机部分；用法：verify_low_level.py <sn>）")

    print("\n" + "=" * 46)
    if _fails:
        print(f"❌ {len(_fails)} 项失败：{_fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
