#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""离线校验：步骤指针 + 校验禁止凑结果。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.services.ai.regression.schemas import (  # noqa: E402
    AgentAction,
    AgentDecision,
    CaseCheckpoint,
    CaseGoal,
    CaseSpec,
    CaseStep,
)
from server.services.regression.agent_executor import (  # noqa: E402
    AgentExecutor,
    build_seq_nodes,
    is_observe_only_step,
)
from server.services.runtime.run_context import RunContext  # noqa: E402

_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + ("" if ok else f"  {detail}"))
    if not ok:
        _fails.append(name)


def spec_nr() -> CaseSpec:
    return CaseSpec(
        case_id="case-nr-app-003-ok",
        name="需求上线前注册的用户，首页看不到领取悬浮球",
        steps=[
            CaseStep(index=1, instruction="点击底部导航栏「首页」。", expected="进入首页，底部「首页」为选中态。"),
            CaseStep(index=2, instruction="查看首页右下角是否出现领取悬浮球。", expected="首页右下角不出现领取悬浮球。"),
        ],
        expected="1. 进入首页，底部「首页」为选中态。\n2. 首页右下角不出现领取悬浮球。",
    )


def make_ex(nodes=None) -> AgentExecutor:
    spec = spec_nr()
    goal = CaseGoal(
        case_id=spec.case_id,
        goal=spec.name,
        checkpoints=[
            CaseCheckpoint(id="cp1", description="进入首页，底部「首页」为选中态。"),
            CaseCheckpoint(id="cp2", description="首页右下角不出现领取悬浮球。"),
        ],
        success_criteria=spec.expected,
    )
    return AgentExecutor(
        goal=goal,
        run_context=RunContext(sn="test"),
        router=None,  # type: ignore[arg-type]
        seq_nodes=nodes if nodes is not None else build_seq_nodes(spec, goal),
    )


def test_build() -> None:
    print("\n[步骤指针]")
    spec = spec_nr()
    nodes = build_seq_nodes(spec)
    check("两步", len(nodes) == 2, str(nodes))
    check("步骤1 是操作", nodes[0].observe_only is False and nodes[0].n == 1)
    check("步骤2 查看直接校验", is_observe_only_step(nodes[1].instruction) and nodes[1].observe_only)
    ex = make_ex(nodes)
    check("开场在步骤1 操作", ex._seq_i == 0 and ex._seq_phase == "do", f"{ex._seq_i} {ex._seq_phase}")
    check("步骤2 开场不会进", "不出现" not in ex._seq_decide_success())
    block = ex._seq_prompt_block()
    check("操作阶段不泄漏预期原文", "不出现" not in block and "选中态" not in block, block)
    goal = ex._seq_decide_goal()
    check("操作目标不含用例名里的「看不到」", "看不到" not in goal and "悬浮球" not in goal, goal)


def test_expect_catalog() -> None:
    from server.services.regression.expect_catalog import classify_expect_text, gap_summary

    print("\n[质检库]")
    tab = classify_expect_text("底部「首页」为选中态。")
    check("选中态无法校验", tab.gap and tab.kind == "tab_selected", str(tab))
    mixed = classify_expect_text("进入首页，底部「首页」为选中态。")
    check("混合只验进入首页", (not mixed.gap) and mixed.prompt_text == "进入首页", mixed.prompt_text)
    check("选中态被拆出", any(c.kind == "tab_selected" for c in mixed.skipped), str(mixed.skipped))
    check("混合句两条都记账", len(mixed.claims) == 2, str(mixed.claims))
    login = classify_expect_text("保持登录")
    check("首页登录态无法校验", login.gap and login.kind == "session_frame", str(login))
    absent = classify_expect_text("首页右下角不出现领取悬浮球。")
    check("不出现可验", (not absent.gap) and absent.kind == "text_absent", str(absent))
    unk = classify_expect_text("功能正常")
    check("功能正常是 UNKNOWN", unk.gap and unk.code == "EXPECT.UNKNOWN", str(unk))
    check("无法验证文案", "无法验证" in gap_summary(tab), gap_summary(tab))


def test_no_skip_ahead() -> None:
    print("\n[禁止跳步]")
    ex = make_ex()
    d = AgentDecision(
        thought="先看悬浮球在不在",
        status="continue",
        action=AgentAction(capability_id="assert_visual", params={"expectation": "首页右下角不出现领取悬浮球。"}),
        checkpoint_ids=["cp2"],
    )
    d2, cap = ex._constrain_seq_decision(d, "assert_visual")
    check("操作阶段不能提前验 cp2", cap == "" and d2.action is None)
    block = ex._seq_prompt_block()
    check("prompt 只强调当前步骤", "当前只做步骤 1" in block and "禁止" in block)


def test_check_no_mutate() -> None:
    print("\n[校验不改界面]")
    ex = make_ex()
    ex._seq_i = 1
    ex._seq_phase = "check"
    d = AgentDecision(
        thought="关掉这个悬浮球让它不出现",
        status="continue",
        action=AgentAction(capability_id="tap_element", params={"x": 900, "y": 800}),
    )
    d2, cap = ex._constrain_seq_decision(d, "tap_element")
    check("点关闭被改写成校验", cap == "assert_visual")
    check(
        "校验期望是当前预期",
        (d2.action.params or {}).get("expectation") == "首页右下角不出现领取悬浮球。",
    )
    made = ex._outcome_manufacture_reason(
        AgentDecision(thought="关掉这个悬浮球让预期成立", action=AgentAction(capability_id="tap_element", params={})),
        "tap_element",
    )
    check("凑不出现会被拦住", "禁止" in made, made)


def test_do_close_ball() -> None:
    print("\n[操作阶段也不许关球凑结果]")
    ex = make_ex()
    d = AgentDecision(
        thought="右下角有领取悬浮球，不符合成功标准，先点叉关掉它",
        status="continue",
        action=AgentAction(capability_id="tap_element", params={"x": 900, "y": 800}),
    )
    made = ex._outcome_manufacture_reason(d, "tap_element")
    check("步骤1 时关悬浮球也拦", "不出现" in made or "禁止" in made, made)


def test_instruction_no_invented_expect() -> None:
    print("\n[指令用例不把目标塞进预期]")
    spec = CaseSpec(
        case_id="chat-login",
        name="打开造好物并且登录账号",
        steps=[CaseStep(index=1, instruction="打开造好物并且登录账号", expected="")],
        expected="",
    )
    goal = CaseGoal(
        case_id=spec.case_id,
        goal=spec.name,
        checkpoints=[CaseCheckpoint(id="cp1", description="造好物移动端已成功打开且账号处于登录状态")],
        success_criteria="屏幕显示已登录的主界面",
    )
    nodes = build_seq_nodes(spec, goal)
    check("仍是一步", len(nodes) == 1)
    check("不把检查点写成预期", nodes[0].expected == "", repr(nodes[0].expected))
    check("无预期就没有 cp", nodes[0].cp_id == "")


if __name__ == "__main__":
    test_build()
    test_expect_catalog()
    test_no_skip_ahead()
    test_check_no_mutate()
    test_do_close_ball()
    test_instruction_no_invented_expect()
    if _fails:
        print(f"failed {len(_fails)}")
        sys.exit(1)
    print("ok")
