#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验：知识检索带用例意图；路径类知识通用优先；另辟路径时纠正（无业务特判）。"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.regression.agent_executor import (  # noqa: E402
    AgentExecutor,
    build_case_intent_for_knowledge,
    build_knowledge_hint_text,
    build_knowledge_index_text,
    build_knowledge_query,
    build_path_knowledge_nudge,
    case_steps_text,
    is_path_knowledge_item,
    rank_knowledge_for_case_intent,
)
from server.services.ai.regression.schemas import CaseGoal, CaseSpec, CaseStep  # noqa: E402
from server.services.system_settings_service import match_testing_knowledge  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> None:
    steps_raw = (
        "1. 打开App，进入Agent对话历史，找到该条新人礼领取对话并进入\n"
        "2. 按原领取流程继续操作，直至完成或走到当前可完成的最后一步\n"
        "3. 返回首页/个人页，确认是否仍存在新的领取卡片入口"
    )
    spec = CaseSpec(
        case_id="case-nr-app-003-ok",
        name="本需求上线前已生成的新人礼领取对话保留，允许走完领取流程",
        steps=[CaseStep(index=1, instruction="打开App，进入Agent对话历史")],
        raw_row={"steps_raw": steps_raw},
    )
    extracted = case_steps_text(spec)
    intent = build_case_intent_for_knowledge(
        case_name=spec.name,
        goal="验证上线前生成的新人礼领取对话仍可进入并走完领取流程",
        steps_text=extracted,
        open_checkpoints=["已进入Agent对话历史页面"],
    )
    home_ocr = "造好物 推荐 热门互动 想要成真 消息 我的 开始造物 feed 作品 首页"
    query = build_knowledge_query(case_intent=intent, screen=home_ocr)
    _assert("Agent对话历史" in query, "query 必须含用例步骤意图")
    hits = match_testing_knowledge(query, limit=8)
    ranked = rank_knowledge_for_case_intent(hits, case_intent=intent, limit=3)
    titles = [str(r.get("title") or "") for r in ranked]
    _assert(
        any(is_path_knowledge_item(r) for r in ranked),
        f"重排后 top3 应含路径类知识，got={titles}",
    )

    path_row = {
        "used": True,
        "id": "k_path",
        "title": "如何进入目标功能页",
        "category": "UI导航",
        "tags": ["入口"],
        "when": "入口",
        "prompt": "「如何进入目标功能页」: 点击底部「开始造物」再点右上角秒表图标",
        "content": "点击底部「开始造物」再点右上角秒表图标",
    }
    noise_row = {
        "used": True,
        "id": "k_noise",
        "title": "点击「在首页feed中站到任意的作品点击进入详情页」操作说明",
        "tags": ["feed"],
        "prompt": "「feed」: 空",
        "content": "【本应用正确操作方式】\n1. \n2.",
    }
    _assert(is_path_knowledge_item(path_row), "标题含「如何进入」应判为路径知识")
    index = build_knowledge_index_text([path_row, noise_row])
    _assert("k_path" in index and "如何进入目标功能页" in index, f"索引应含 id+标题: {index!r}")
    _assert("开始造物" not in index, f"索引不得展开正文: {index!r}")
    hint = build_knowledge_hint_text([path_row, noise_row], case_intent=intent)
    _assert("优先执行·操作路径" in hint, f"展开正文仍强调路径优先: {hint!r}")

    bad = SimpleNamespace(
        thought="当前在首页，先进入消息页面查看历史记录",
        expected_after="消息列表",
        knowledge_ids=[],
    )
    nudge = build_path_knowledge_nudge(bad, [path_row], case_intent=intent)
    _assert(bool(nudge), "未点名路径知识时应纠正")
    _assert("knowledge_ids" in nudge or "点名" in nudge, f"纠正应要求点名: {nudge!r}")
    _assert("替代入口" in nudge, f"纠正文案应通用: {nudge!r}")
    _assert("禁止用底部" not in nudge and "Agent 对话" not in nudge, f"纠正不应含业务特判: {nudge!r}")

    good = SimpleNamespace(
        thought="按知识点击底部开始造物，再找右上角秒表图标",
        expected_after="目标功能页",
        knowledge_ids=["k_path"],
    )
    _assert(
        not build_path_knowledge_nudge(good, [path_row], case_intent=intent),
        "已点名路径知识时不应纠正",
    )

    # Executor 包装仍可用
    ex = AgentExecutor.__new__(AgentExecutor)
    ex.goal = CaseGoal(case_id="x", goal="验证对话保留", checkpoints=[], success_criteria="")
    ex.case_brief = ""
    ex.case_name = spec.name
    ex.case_steps_text = extracted
    ex.case_preconditions = ""
    _assert(bool(ex._path_knowledge_nudge(bad, [path_row])), "executor nudge 包装可用")

    print("ranked top3:", titles)
    print("ok: generic path-knowledge priority + divergence nudge")


if __name__ == "__main__":
    main()
