# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""AI-led 回归测试的 Plan / Replan / Assert / Diff prompt 与服务接口。

模块布局：
    schemas.py     — Pydantic 数据契约（PlanEvent / PlanResult / ReplanResult / CaseSpec / BaselineContext）
    prompts.py     — Plan/Replan/Diff prompt 与 builders
    llm_client.py  — 纯文本 OpenAI-compatible chat 调用 + JSON 解析（不带截图）
    planner.py     — generate_overview() / replan_single_step() / 高层入口

公开入口（按使用顺序）：

    from server.services.ai.regression import (
        CaseSpec, BaselineContext,
        generate_overview, replan_single_step,
    )

    spec = CaseSpec(case_id="...", name="...", preconditions=..., steps=..., expected=...)
    result = generate_overview(spec, run_context=ctx)
    if result.mode == "decline":
        ... # 人工介入或跳过
    else:
        for ev in result.events:
            ... # 执行 / 下钻

    # 单步重规划（执行失败 / 与 baseline 偏离时调用）
    replan = replan_single_step(
        run_context=ctx,
        completed_events=[...],
        failed_event=...,
        failure_summary="...",
        baseline=None,
    )
"""
from __future__ import annotations

from server.services.ai.regression.schemas import (  # noqa: F401
    AssertResult,
    BaselineContext,
    BaselineSnippet,
    CaseSpec,
    CaseStep,
    EventResult,
    EventStatus,
    HitlComposerResult,
    LocateResult,
    PersonaExpandResult,
    PlanEvent,
    PlanResult,
    ReplanResult,
    RunReport,
)
from server.services.ai.regression.planner import (  # noqa: F401
    assert_visual,
    compose_hitl_prompt,
    expand_persona_task,
    generate_overview,
    locate_element,
    replan_single_step,
)
