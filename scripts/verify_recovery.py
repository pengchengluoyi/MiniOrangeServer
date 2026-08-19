#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""S1-recovery 验收脚本：YAML 声明的系统层恢复。

验证「新增一种系统状况的处置不需要改 Python」这条通路：
  取证（probe_device_state，纯 YAML low_level）
    → 匹配（plugins/recovery/*.yaml）
    → 处置（wake_screen / dismiss_keyguard，也是纯 YAML 能力）
    → verify 复查

用法：
    .venv/bin/python scripts/verify_recovery.py            # 仅离线
    .venv/bin/python scripts/verify_recovery.py 5fda2f6d   # 追加真机：熄屏 → 自动恢复

⚠️ 真机部分会把屏幕熄掉再唤醒解锁（无密码设备）。
退出码 0 = 全通过，1 = 有失败项。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.services.plugins import registry  # noqa: E402
from server.services.plugins.models import (  # noqa: E402
    RecoveryAction,
    RecoveryForbid,
    RecoveryMatch,
    RecoveryRule,
)
from server.services.regression import recovery as R  # noqa: E402

_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: {got!r}" + ("" if ok else f"  期望 {want!r}"))
    if not ok:
        _fails.append(name)


def ev(**kw) -> R.Evidence:
    return R.Evidence(**kw)


def test_shipped_rules() -> None:
    print("\n[内置规则加载]")
    rules = registry.list_recovery_rules()
    ids = {r.id for r in rules}
    check("屏幕息屏/锁屏规则已加载", "screen_asleep_or_locked" in ids, True)
    check("系统权限框规则已加载", "system_permission_dialog" in ids, True)
    check("按 priority 降序", [r.priority for r in rules] == sorted((r.priority for r in rules), reverse=True), True)
    check("每条都有 owner（谁负责）", all(r.owner for r in rules), True)
    bad = [e for e in registry.list_load_errors() if e.kind == "recovery"]
    check("无 recovery 加载错误", bad, [])

    lock = registry.get_recovery_rule("screen_asleep_or_locked")
    check("确定性规则声明了动作", len(lock.actions) if lock else 0, 4)
    caps = [a.capability for a in lock.actions] if lock else []
    check("动作只引用已有能力", all(registry.get_capability(c) is not None for c in caps), True)


def test_matching() -> None:
    print("\n[规则匹配]")
    lock = registry.get_recovery_rule("screen_asleep_or_locked")

    hits = R.match_rules(ev(awake="no", locked="yes", screen_blocked="yes"), [])
    check("息屏锁屏 → 命中", [h.rule_id for h in hits], ["screen_asleep_or_locked"])

    hits = R.match_rules(ev(awake="yes", locked="no", screen_blocked="no"), [])
    check("正常状态 → 不命中", [h.rule_id for h in hits], [])

    hits = R.match_rules(ev(awake="unknown", locked="unknown"), [])
    check("事实未知 → 不命中（不瞎猜）", [h.rule_id for h in hits], [])

    # 顶层窗口 + 屏上文案（权限框那条）
    e = ev(awake="yes", locked="no", screen_blocked="no",
           top_window_pkg="com.android.permissioncontroller")
    hits = R.match_rules(e, ["允许", "使用此应用时"])
    check("权限框（包名+文案都中）→ 命中", [h.rule_id for h in hits], ["system_permission_dialog"])
    hits = R.match_rules(e, ["完全无关的文案"])
    check("包名中但文案不中 → 不命中（AND 语义）", [h.rule_id for h in hits], [])
    e2 = ev(awake="yes", locked="no", screen_blocked="no", top_window_pkg="com.mathmagic.magicam")
    check("文案中但包名不中 → 不命中", [h.rule_id for h in R.match_rules(e2, ["允许"])], [])

    empty = RecoveryRule(id="empty", owner="@x", mode="advise", match=RecoveryMatch())
    check("空条件规则不许命中一切",
          R.match_rules(ev(awake="yes"), ["x"], rules=[empty]), [])

    # 多条命中时按 priority 排序（registry 已排好，这里验证顺序被保留）
    hi = RecoveryRule(id="hi", owner="@x", priority=99, mode="advise",
                      match=RecoveryMatch(evidence={"awake": "no"}))
    lo = RecoveryRule(id="lo", owner="@x", priority=1, mode="advise",
                      match=RecoveryMatch(evidence={"awake": "no"}))
    check("多条命中保持传入顺序（priority 在 registry 层排序）",
          [h.rule_id for h in R.match_rules(ev(awake="no"), [], rules=[hi, lo])], ["hi", "lo"])


def test_advise_mode() -> None:
    print("\n[advise 模式：只出建议，不动设备]")
    rule = registry.get_recovery_rule("system_permission_dialog")
    out = R.apply_rule(R.RuleMatch(rule=rule), ctx=None, router=None)
    check("不执行任何动作", out.applied, False)
    check("产出建议文本", len(out.advice) > 20, True)
    check("建议里含安全约束", "禁止" in out.advice, True)
    check("forbid 名单非空", bool(rule.forbid.text_any), True)


def test_deterministic_permission_dialog() -> None:
    print("\n[deterministic 模式：权限弹窗（YAML actions + verify）]")
    rule = registry.get_recovery_rule("system_permission_dialog_while_using_deterministic")
    assert rule is not None

    calls: list[tuple[str, dict]] = []

    class FakeRouter:
        def dispatch(self, event, **kw):
            from server.services.ai.regression.schemas import EventResult, EventStatus

            calls.append((event.capability_id, dict(event.params or {})))
            if event.capability_id == "probe_device_state":
                # 让 verify.evidence.app_foreground == "yes"
                pkg = str((event.params or {}).get("package") or PKG)
                low_level = {
                    "power": {"mWakefulness": "Awake"},
                    "keyguard": {"isKeyguardShowing": "false"},
                    "foreground": [f"topResumedActivity=foo bar {pkg}/.MainActivity"],
                    "target_pid": "123",
                    "anr_window": "0",
                    "ime": {"mInputShown": "false"},
                }
                return EventResult(
                    seq=1,
                    capability_id=event.capability_id,
                    event_kind=event.capability_id,
                    status=EventStatus.PASS,
                    executor_used="fake",
                    summary="ok",
                    ai_reasoning="",
                    raw_response={"low_level": low_level},
                )

            return EventResult(
                seq=1,
                capability_id=event.capability_id,
                event_kind=event.capability_id,
                status=EventStatus.PASS,
                executor_used="fake",
                summary="ok",
                ai_reasoning="",
            )

    PKG = "com.mathmagic.magicam"
    out = R.apply_rule(
        R.RuleMatch(rule=rule), ctx=None, router=FakeRouter(), target_package=PKG
    )
    check("真的应用了动作（applied=true）", out.applied, True)
    check("verify 通过（recovered=true）", out.recovered, True)

    tap_targets = [
        (params.get("target") or {}).get("text")
        for cap, params in calls
        if cap == "tap_element"
    ]
    check("tap 明确覆盖 zh 文案",
          "仅在使用该应用时允许" in tap_targets, True)
    check("tap 明确覆盖 en 文案（While using the app）",
          "While using the app" in tap_targets, True)
    check("tap 明确覆盖 en 变体（Allow while using the app）",
          "Allow while using the app" in tap_targets, True)


def test_forbid_guard() -> None:
    print("\n[安全护栏：forbid 命中即拒绝执行]")
    calls: list[str] = []

    class FakeRouter:
        def dispatch(self, event, **kw):
            calls.append(event.capability_id)
            from server.services.ai.regression.schemas import EventResult, EventStatus
            return EventResult(seq=1, capability_id=event.capability_id,
                               event_kind=event.capability_id, status=EventStatus.PASS,
                               executor_used="fake", summary="ok", ai_reasoning="")

    rule = RecoveryRule(
        id="danger", owner="@x", mode="deterministic",
        match=RecoveryMatch(evidence={"awake": "no"}),
        actions=[
            RecoveryAction(capability="tap_element", target={"text": "清除数据"}),
            RecoveryAction(capability="wake_screen"),
        ],
        forbid=RecoveryForbid(text_any=["清除数据"]),
        verify=RecoveryMatch(),
    )
    out = R.apply_rule(R.RuleMatch(rule=rule), ctx=None, router=FakeRouter())
    check("危险动作被拦下", calls, ["wake_screen"])
    check("拦下有记录", any("forbid" in str(a.get("skipped", "")) for a in out.actions), True)


def test_evidence_derivation() -> None:
    print("\n[派生事实]")
    e = R.Evidence(awake="no", locked="unknown")
    check("息屏即 screen_blocked（构造时不自动派生，由 collect 计算）",
          e.as_match_dict()["screen_blocked"], "unknown")
    facts = R.Evidence(awake="yes", locked="no", target_alive="yes").as_match_dict()
    check("as_match_dict 覆盖全部可匹配键",
          sorted(facts.keys()),
          ["anr", "app_foreground", "awake", "ime_shown", "locked", "screen_blocked", "target_alive"])


def test_device(sn: str) -> None:
    print(f"\n[真机端到端 sn={sn}]")
    import subprocess
    import time

    from server.services.regression.router import CapabilityRouter
    from server.services.runtime.run_context import build_run_context

    PKG = "com.mathmagic.magicam"
    ctx = build_run_context(sn, platform="android", run_id="verify-recovery", target_package=PKG,
                            probe_remote_channel=False, probe_vlm_channel=False,
                            probe_hitl_channel=False)
    if ctx.adb.get("state") != "connected":
        print(f"  ⚠️ adb 未连通（{ctx.adb.get('state')}），跳过真机部分")
        return
    router = CapabilityRouter(ctx, capture_prefer=("adb",))

    e0 = R.collect_evidence(ctx, router, target_package=PKG)
    check("取证拿到电源状态", e0.awake in ("yes", "no"), True)
    check("取证拿到锁屏状态", e0.locked in ("yes", "no"), True)

    # 尽量在熄屏前先处理权限弹窗（如果当前屏幕确实出现）
    try:
        from server.services.regression import hierarchy as H

        dump = H.dump_ui_nodes(sn, force_fresh=True)
        screen_texts = R.screen_texts_from_hierarchy(dump)
        hits = R.match_rules(e0, screen_texts)
        perm = next(
            (h for h in hits if h.rule_id == "system_permission_dialog_while_using_deterministic"),
            None,
        )
        if perm:
            out_perm = R.apply_rule(perm, ctx, router, target_package=PKG)
            check("权限弹窗 deterministic 能通过 verify", bool(out_perm and out_perm.recovered), True)
            if out_perm:
                print(f"  ℹ️  {out_perm.summary()}")
        else:
            print("  ℹ️  当前屏幕未检测到权限弹窗（跳过权限弹窗回收验证）")
    except Exception as exc:  # pragma: no cover
        print(f"  ⚠️ 权限弹窗检测/恢复跳过：{exc}")

    print("  … 人为熄屏（模拟过夜息屏）")
    subprocess.run(["adb", "-s", sn, "shell", "input", "keyevent", "26"], capture_output=True)
    time.sleep(2.5)

    e1 = R.collect_evidence(ctx, router, target_package=PKG)
    check("熄屏被取证发现", e1.screen_blocked, "yes")
    hits = R.match_rules(e1, [])
    check("命中息屏规则", [h.rule_id for h in hits], ["screen_asleep_or_locked"])

    out = R.apply_rule(hits[0], ctx, router, target_package=PKG) if hits else None
    check("规则执行并通过 verify", bool(out and out.recovered), True)
    if out:
        print(f"  ℹ️  {out.summary()}")
        for a in out.actions:
            print(f"     - {a['capability']}: {a.get('status') or a.get('skipped')}")

    e2 = R.collect_evidence(ctx, router, target_package=PKG)
    check("恢复后屏幕可用", e2.screen_blocked, "no")
    check("恢复后不再命中", [h.rule_id for h in R.match_rules(e2, [])], [])

    # 一键入口
    subprocess.run(["adb", "-s", sn, "shell", "input", "keyevent", "26"], capture_output=True)
    time.sleep(2.5)
    out2 = R.recover_if_needed(ctx, router, target_package=PKG)
    check("recover_if_needed 单一入口可用", bool(out2 and out2.recovered), True)
    e3 = R.collect_evidence(ctx, router, target_package=PKG)
    check("入口调用后屏幕可用", e3.screen_blocked, "no")


def main() -> int:
    print("=== S1-recovery 验收：YAML 声明的系统层恢复 ===")
    test_shipped_rules()
    test_matching()
    test_advise_mode()
    test_deterministic_permission_dialog()
    test_forbid_guard()
    test_evidence_derivation()
    if len(sys.argv) > 1:
        test_device(sys.argv[1])
    else:
        print("\n（未指定 sn，跳过真机部分；用法：verify_recovery.py <sn>）")

    print("\n" + "=" * 46)
    if _fails:
        print(f"❌ {len(_fails)} 项失败：{_fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
