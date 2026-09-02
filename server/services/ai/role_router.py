# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""分析师：根据当前步骤决定调用哪个角色的哪项能力。

执行路径默认走剧本（不额外打模型），避免每一步再套一层 LLM。
设置页对话或显式 route 时，才用分析师 prompt 做一次调度。
"""
from __future__ import annotations

from typing import Any, Optional

CONDUCTOR_SYSTEM_PROMPT = """你是 MiniOrange 的分析师。你不执行测试、不点屏幕、不定验收/发版。

【你管什么】
- 看当前场景和状态，从能力目录里选出「下一步该调用哪个角色的哪项能力」。
- 一次只排 1~3 步，说明原因。
- 人审门禁（验收通过 / 发版通过）不能自动过，只能排 BM 出草稿。

【你不管什么】
- 不代替测试工程师看截图做动作。
- 不编造目录里没有的 role_id / capability_id。

【输出 JSON（禁止 Markdown）】
{
  "scene": "qa_tick|case_exec_agent|case_exec_plan|feishu_sync|other",
  "steps": [
    {"role_id": "...", "capability_id": "...", "reason": "为什么现在调它"}
  ],
  "done": false,
  "note": "可选补充"
}"""

PLAYBOOKS = [
    {
        "id": "qa_tick",
        "label": "需求自动推进",
        "summary": "有原文后由分析师按缺口排队，不改验收门禁。",
        "steps": [
            {"phase": "有原文且未分析", "role_id": "req-analyst", "capability_id": "analyze_req", "label": "拆验收标准与测试点"},
            {"phase": "有材料且可能改骨架/用例", "role_id": "req-analyst", "capability_id": "propose_atlas", "label": "判断影响范围，出品变更任务等人确认"},
            {"phase": "已分析且无脑图", "role_id": "mindmap-writer", "capability_id": "draft_mindmap", "label": "铺测试脑图"},
            {"phase": "测试点缺用例", "role_id": "case-writer", "capability_id": "draft_cases", "label": "补用例草稿"},
            {"phase": "用例准备", "role_id": "req-qa-bm", "capability_id": "map_cases", "label": "对照用例库"},
            {"phase": "进入验收", "role_id": "req-qa-bm", "capability_id": "draft_sign", "label": "验收草稿（人点结论）"},
        ],
    },
    {
        "id": "case_exec_agent",
        "label": "执行用例 · Agent",
        "summary": "adb 真机默认路径：每条用例开跑到结束调用的能力。",
        "steps": [
            {"phase": "开跑前", "role_id": "test-engineer", "capability_id": "case-scene", "label": "理解用例场景（登录/设备/前置）"},
            {"phase": "开跑前", "role_id": "test-engineer", "capability_id": "pick_device", "label": "按场景申请设备"},
            {"phase": "开跑前", "role_id": "test-engineer", "capability_id": "pick_account", "label": "按场景租账号"},
            {"phase": "开跑", "role_id": "test-engineer", "capability_id": "goal-extract", "label": "抽取目标与检查点"},
            {"phase": "开场", "role_id": "test-engineer", "capability_id": "agent-restart", "label": "是否先重开应用"},
            {"phase": "开场", "role_id": "test-engineer", "capability_id": "inspect-session", "label": "前置登录态（业务用例退出再登录）"},
            {"phase": "每一步", "role_id": "test-engineer", "capability_id": "agent-decide", "label": "看截图决定下一个动作"},
            {"phase": "断言", "role_id": "test-engineer", "capability_id": "assert-vision", "label": "检查点是否达成"},
            {"phase": "卡住要问人", "role_id": "test-engineer", "capability_id": "hitl-composer", "label": "改写成问人话术"},
            {"phase": "清缓存/强停/装包", "role_id": "test-engineer", "capability_id": "persona-task", "label": "拟人化走设置"},
            {"phase": "用例结束", "role_id": "version-qa-bm", "capability_id": "knowledge-capture", "label": "沉淀应用知识"},
            {"phase": "用例结束", "role_id": "knowledge-reviewer", "capability_id": "knowledge-review", "label": "知识机审"},
            {"phase": "用例结束", "role_id": "test-engineer", "capability_id": "account-tag", "label": "账号打标"},
        ],
    },
    {
        "id": "case_exec_plan",
        "label": "执行用例 · Plan",
        "summary": "远程/Claw 或显式 Plan 模式：先规划事件再逐步定位。",
        "steps": [
            {"phase": "开跑前", "role_id": "test-engineer", "capability_id": "pick_device", "label": "按用例申请设备"},
            {"phase": "开跑前", "role_id": "test-engineer", "capability_id": "pick_account", "label": "按场景租账号"},
            {"phase": "开跑", "role_id": "test-engineer", "capability_id": "plan-overview", "label": "纯文本规划事件序列"},
            {"phase": "点击/输入前", "role_id": "test-engineer", "capability_id": "locate-vision", "label": "截图定位坐标"},
            {"phase": "预期", "role_id": "test-engineer", "capability_id": "assert-vision", "label": "视觉断言"},
            {"phase": "失败", "role_id": "test-engineer", "capability_id": "single-step-replan", "label": "只重规划失败步"},
            {"phase": "要问人", "role_id": "test-engineer", "capability_id": "hitl-composer", "label": "HITL 话术"},
            {"phase": "结束", "role_id": "version-qa-bm", "capability_id": "knowledge-capture", "label": "沉淀应用知识"},
            {"phase": "结束", "role_id": "knowledge-reviewer", "capability_id": "knowledge-review", "label": "知识机审"},
            {"phase": "结束", "role_id": "test-engineer", "capability_id": "account-tag", "label": "账号打标"},
        ],
    },
    {
        "id": "feishu_step",
        "label": "飞书逐步回归",
        "summary": "旧路径：一条飞书步骤规划并校验。",
        "steps": [
            {"phase": "同步表格", "role_id": "case-writer", "capability_id": "case-step-parse", "label": "解析测试步骤列"},
            {"phase": "同步表格", "role_id": "case-writer", "capability_id": "case-expected-parse", "label": "解析预期效果列"},
            {"phase": "改写指令", "role_id": "test-engineer", "capability_id": "copilot-rewrite", "label": "改写成可规划指令"},
            {"phase": "执行一步", "role_id": "test-engineer", "capability_id": "ai-case-plan", "label": "按截图出可执行 plan"},
            {"phase": "校验预期", "role_id": "test-engineer", "capability_id": "ai-case-assert", "label": "预期是否成立"},
        ],
    },
    {
        "id": "docs_reports",
        "label": "文档与报告",
        "summary": "测试/发版完成后先出报告草稿，人确认再通过文档插件写入 Wiki。源数据在流程里，外部文档是副本。",
        "steps": [
            {"phase": "需求测试完成", "role_id": "report-writer", "capability_id": "draft_test_report", "label": "写测试报告草稿"},
            {"phase": "发版评审完成", "role_id": "report-writer", "capability_id": "draft_release_report", "label": "写发版报告草稿"},
            {"phase": "人确认报告", "role_id": "doc-keeper", "capability_id": "publish_wiki", "label": "插件写入 Wiki / 文件夹"},
            {"phase": "用例跑完", "role_id": "doc-keeper", "capability_id": "mark_case_status", "label": "回写待测/通过/失败"},
        ],
    },
]


def list_playbooks() -> list[dict[str, Any]]:
    return PLAYBOOKS


def playbook(scene: str) -> Optional[dict[str, Any]]:
    sid = str(scene or "").strip()
    return next((p for p in PLAYBOOKS if p["id"] == sid), None)


def catalog_brief(roles: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for row in roles or []:
        out.append(
            {
                "id": row.get("id") or "",
                "label": row.get("label") or "",
                "owner": row.get("owner") or "",
                "job": row.get("job") or "",
            }
        )
    return out


def route_playbook(scene: str) -> dict[str, Any]:
    row = playbook(scene)
    if not row:
        return {"engine": "playbook", "scene": scene, "steps": [], "note": "未知场景，请改用分析师对话"}
    return {"engine": "playbook", "scene": row["id"], "steps": row["steps"], "label": row["label"]}


def route_with_llm(*, scene: str, state: dict | None = None) -> dict[str, Any]:
    """分析师打一次模型，从目录里选下一步。失败则回落到剧本。"""
    import json

    from server.services.ai.roles_catalog import list_roles
    from server.services.ai.regression.llm_client import call_chat_text, parse_token_usage, resolve_regression_provider

    catalog = list_roles()
    brief = {
        "scene": scene,
        "state": state or {},
        "roles": catalog_brief(catalog.get("product") or []),
        "capabilities": catalog_brief(catalog.get("runtime") or []),
        "playbooks": [{"id": p["id"], "label": p["label"]} for p in PLAYBOOKS],
    }
    provider, gate = resolve_regression_provider()
    if not provider:
        fallback = route_playbook(scene or "qa_tick")
        fallback["note"] = gate.get("reason") or "未配置模型，使用剧本"
        return fallback
    from server.services.ai import dispatch_log as dispatch
    tok = dispatch.bind(trigger="conductor_route", source="analyst_route", role="conductor", job="route")
    try:
        parsed, meta = call_chat_text(
            provider=provider,
            messages=[
                {"role": "system", "content": CONDUCTOR_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(brief, ensure_ascii=False)},
            ],
            temperature=0.1,
            max_tokens=800,
            timeout_sec=45,
        )
    finally:
        dispatch.reset(tok)
    usage = parse_token_usage(meta.get("usage"))
    if not isinstance(parsed, dict) or not parsed.get("steps"):
        fallback = route_playbook(scene or "case_exec_agent")
        fallback["usage"] = usage
        fallback["note"] = meta.get("error") or "分析师未给出步骤，使用剧本"
        return fallback
    return {
        "engine": "llm",
        "scene": parsed.get("scene") or scene,
        "steps": parsed.get("steps") or [],
        "done": bool(parsed.get("done")),
        "note": parsed.get("note") or "",
        "usage": usage,
        "elapsed_ms": meta.get("elapsed_ms") or 0,
    }
