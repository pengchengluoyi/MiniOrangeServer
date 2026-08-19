#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""S0b 验收脚本：UI 层级采集与语义锚点解析。

验证"模型给语义锚点、执行侧解析成精确坐标"这条通路
（见 docs/plan-skill-packs-and-console.md §3.2），核心指标是**同一锚点落点稳定**。

用法：
    .venv/bin/python scripts/verify_anchors.py            # 仅离线（含真实层级片段回归）
    .venv/bin/python scripts/verify_anchors.py 5fda2f6d   # 追加真机端到端

退出码 0 = 全通过，1 = 有失败项。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.services.regression.hierarchy import (  # noqa: E402
    dump_ui_nodes,
    has_target,
    resolve_target,
    to_prompt_text,
    _parse_xml,
)

_fails: list[str] = []


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {name}: {got!r}" + ("" if ok else f"  期望 {want!r}"))
    if not ok:
        _fails.append(name)


# 造物相机实测层级片段（2026-08-19，5fda2f6d / Android 16）。
# 关键点：desc="社区" 同时出现在**不可点击的顶部标题**与**可点击的底栏 Tab** 上，
# 且标题面积(5586)远小于底栏(45804)。曾按"面积最小"挑选 → 点中标题 → 点击无效果。
_FIXTURE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
  <node index="0" class="android.widget.FrameLayout" package="com.mathmagic.magicam"
        bounds="[0,0][1200,2608]" clickable="false" enabled="true" text="" content-desc="" resource-id="">
    <node index="1" class="android.view.View" bounds="[76,196][197,242]" clickable="false"
          enabled="true" text="" content-desc="社区" resource-id=""/>
    <node index="2" class="android.view.View" bounds="[133,2456][334,2534]" clickable="true"
          enabled="true" text="" content-desc="社区" resource-id=""/>
    <node index="3" class="android.view.View" bounds="[866,2456][1067,2534]" clickable="true"
          enabled="true" text="" content-desc="我的" resource-id=""/>
    <node index="4" class="android.widget.FrameLayout" bounds="[400,2400][800,2560]" clickable="true"
          enabled="true" text="" content-desc="" resource-id="com.mathmagic.magicam:id/btn_create">
      <node index="5" class="android.widget.TextView" bounds="[520,2470][680,2510]" clickable="false"
            enabled="true" text="开始造物" content-desc="" resource-id=""/>
    </node>
    <node index="6" class="android.widget.TextView" bounds="[100,1000][300,1040]" clickable="false"
          enabled="true" text="禁用按钮" content-desc="" resource-id="" />
  </node>
</hierarchy>
"""


def test_parse() -> None:
    print("\n[层级解析]")
    d = _parse_xml(_FIXTURE)
    check("解析成功", d.ok, True)
    check("节点数", len(d), 7)
    tab = [n for n in d.nodes if n.content_desc == "社区" and n.clickable]
    check("底栏 Tab 中心点", tab[0].center if tab else None, (233, 2495))
    check("面积计算", tab[0].area if tab else 0, 201 * 78)
    rid = [n for n in d.nodes if n.rid_short == "btn_create"]
    check("resource-id 去包名前缀", bool(rid), True)


def test_clickable_first() -> None:
    print("\n[候选挑选：可点击优先，不是面积最小]")
    d = _parse_xml(_FIXTURE)
    m = resolve_target(d.nodes, {"content_desc": "社区"})
    check("命中方式", m.matched_by if m else None, "content_desc")
    check("候选总数", m.candidates if m else 0, 2)
    check("其中可点击", m.clickable_candidates if m else -1, 1)
    check("选中可点击的底栏（不是面积更小的标题）", m.node.center if m else None, (233, 2495))
    check("选中节点确实可点击", m.node.clickable if m else None, True)


def test_priority_and_ancestor() -> None:
    print("\n[优先级与可点击祖先]")
    d = _parse_xml(_FIXTURE)
    m = resolve_target(d.nodes, {"resource_id": "btn_create"})
    check("resource_id 短名命中", m.matched_by if m else None, "resource_id_short")

    # 文本在不可点击的 TextView 上，应上溯到可点击父容器
    m = resolve_target(d.nodes, {"text": "开始造物"})
    check("文本命中", m.matched_by if m else None, "text")
    check("上溯到可点击父容器", (m.node.rid_short, m.node.clickable) if m else None,
          ("btn_create", True))

    # 同时给多个键时，resource_id 优先于 text
    m = resolve_target(d.nodes, {"resource_id": "btn_create", "text": "开始造物"})
    check("resource_id 优先于 text", m.matched_by if m else None, "resource_id_short")

    m = resolve_target(d.nodes, {"content_desc": "社"})
    check("子串兜底", m.matched_by if m else None, "content_desc_contains")

    check("完全不存在 → None", resolve_target(d.nodes, {"text": "根本没有XYZ"}), None)
    check("空 target → None", resolve_target(d.nodes, {}), None)


def test_has_target() -> None:
    print("\n[has_target 判定]")
    check("有 text", has_target({"target": {"text": "社区"}}), True)
    check("有 content-desc 连字符写法", has_target({"target": {"content-desc": "社区"}}), True)
    check("空 target", has_target({"target": {}}), False)
    check("target 值为空串", has_target({"target": {"text": "  "}}), False)
    check("无 target", has_target({"x": 1, "y": 2}), False)
    check("target 不是 dict", has_target({"target": "社区"}), False)


def test_prompt_text() -> None:
    print("\n[紧凑视图]")
    d = _parse_xml(_FIXTURE)
    text = to_prompt_text(d, limit=10)
    check("含底栏文案", "社区" in text, True)
    check("标注可点击", "[点]" in text, True)
    check("带坐标", "@(" in text, True)


def test_device(sn: str) -> None:
    print(f"\n[真机端到端 sn={sn}]")
    from server.services.ai.regression.schemas import PlanEvent
    from server.services.regression.router import CapabilityRouter
    from server.services.runtime.run_context import build_run_context

    PKG = "com.mathmagic.magicam"
    ctx = build_run_context(sn, platform="android", run_id="verify-anchors", target_package=PKG,
                            probe_remote_channel=False, probe_vlm_channel=False,
                            probe_hitl_channel=False)
    if ctx.adb.get("state") != "connected":
        print(f"  ⚠️ adb 未连通（{ctx.adb.get('state')}），跳过真机部分")
        return

    dump = dump_ui_nodes(sn, force_fresh=True)
    check("层级采集成功", dump.ok, True)
    print(f"  ℹ️  {len(dump)} 节点 / {dump.elapsed_ms}ms（dump 成本，故不在每步注入）")

    router = CapabilityRouter(ctx, capture_prefer=("adb",))

    def go(cap, params, seq=1):
        ev = PlanEvent(seq=seq, capability_id=cap, event_kind=cap, params=params,
                       needs_vlm=False, ai_reasoning="verify-anchors", label="")
        return router.dispatch(ev, run_id="verify", case_id="S0b", case_brief="", shared={})

    go("close_app", {"package": PKG})
    time.sleep(0.5)
    go("launch_app", {"package": PKG})
    time.sleep(5)

    # 落点稳定性：同一锚点连续 3 次，每次真实重新 dump
    pts: list[tuple] = []
    for i in range(3):
        time.sleep(1.8)
        res = go("tap_element", {"target": {"content_desc": "我的"}}, seq=10 + i)
        a = (res.raw_response or {}).get("anchor") or {}
        pts.append(tuple(a.get("center") or ()))
    check("锚点三次落点完全一致", len(set(pts)) == 1 and pts[0] != (), True)
    print(f"  ℹ️  落点：{pts}（对比历史 VIEW-007 同一按钮七种坐标）")

    res = go("tap_element", {"target": {"content_desc": "社区"}}, seq=20)
    a = (res.raw_response or {}).get("anchor") or {}
    check("锚点点击成功", str(res.status.value), "pass")
    check("选中的是可点击元素", a.get("clickable"), True)

    res = go("tap_element", {"target": {"text": "不存在XYZ"}, "x": 233, "y": 2494}, seq=30)
    check("未命中时回落坐标", str(res.status.value), "pass")
    check("回落有审计记录", (res.raw_response or {}).get("anchor", {}).get("ok"), False)

    res = go("tap_element", {"target": {"text": "不存在XYZ"}}, seq=31)
    check("既无锚点又无坐标 → 明确失败", str(res.status.value), "fail")

    res = go("tap_element", {"x": 233, "y": 2494}, seq=32)
    check("纯坐标老形态不受影响", str(res.status.value), "pass")


def main() -> int:
    print("=== S0b 验收：UI 层级与语义锚点 ===")
    test_parse()
    test_clickable_first()
    test_priority_and_ancestor()
    test_has_target()
    test_prompt_text()
    if len(sys.argv) > 1:
        test_device(sys.argv[1])
    else:
        print("\n（未指定 sn，跳过真机部分；用法：verify_anchors.py <sn>）")

    print("\n" + "=" * 46)
    if _fails:
        print(f"❌ {len(_fails)} 项失败：{_fails}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
