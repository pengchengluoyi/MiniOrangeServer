#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验 Agent 开跑前会申请号池账号，登录不再默认问人要手机号。"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.account_issue_service import (  # noqa: E402
    EMPTY_BRIEF,
    extract_account_sms,
    format_accounts_brief,
)
from server.services.ai.regression.prompts import (  # noqa: E402
    AGENT_DECIDE_SYSTEM_PROMPT,
    AGENT_DECIDE_USER_TEMPLATE,
    build_agent_decide_messages,
)
from server.services.project_env import pick_test_accounts  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def func_source(rel: str, name: str) -> str:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment((ROOT / rel).read_text(encoding="utf-8"), node) or ""
    return ""


def main() -> int:
    failed = []

    def check(cond, msg):
        print(f"   {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failed.append(msg)

    print("── 号池备注解析 / 公开 brief")
    check(extract_account_sms("测试环境 验证码为888888") == "888888", "验证码为888888")
    check(extract_account_sms("验证码: 1234") == "1234", "验证码: 1234")
    check(extract_account_sms("手机号 17633312000") == "", "手机号不会当成验证码")
    ranked = [
        {
            "id": "a1",
            "env": "test",
            "phone": "17633312000",
            "tags": ["已登录"],
            "note": "验证码为888888",
            "score": 26,
            "reason": "环境匹配 · 标签「已登录」",
            "sms_code": "888888",
        }
    ]
    brief = format_accounts_brief(ranked, picked=ranked[0])
    check("17633312000" in brief and "888888" in brief, "brief 含首选手机号和固定验证码")
    check("test" in brief or "测试" in brief, "brief 用环境区分账号")
    check("禁止再问人" in brief, "brief 写明不要再问人要号")
    check("未命名" not in brief, "brief 不再用自定义名称")
    check(format_accounts_brief([]) == EMPTY_BRIEF, "空号池有明确空态文案")

    print("\n── 同环境按号码和标签排序，不用名称")
    rows = [
        {"id": "x", "env": "test", "phone": "13800000000", "tags": [], "note": "", "locked": False},
        {
            "id": "y",
            "env": "test",
            "phone": "17633312000",
            "tags": ["已登录"],
            "note": "验证码为888888",
            "locked": False,
        },
    ]
    picked = pick_test_accounts(rows, prompt="已登录 17633312000", env="test")
    check(picked and picked[0].get("id") == "y", "号码+标签命中的号排第一")
    named = [
        {"id": "a", "name": "未注册手机号", "env": "test", "phone": "17633330001", "tags": ["未注册手机号"], "note": "", "locked": False},
        {"id": "b", "name": "其它", "env": "test", "phone": "17633312000", "tags": ["已登录"], "note": "", "locked": False},
    ]
    by_tag = pick_test_accounts(named, prompt="已登录账号首页无悬浮球", env="test")
    check(by_tag and by_tag[0].get("id") == "b", "不再因为名称含「注册」误筛未注册号")

    print("\n── Agent decide prompt 吃号池，不再示范先问手机号")
    check("{accounts_brief}" in AGENT_DECIDE_USER_TEMPLATE, "decide user 模板有号池段")
    check("禁止 ask_human 再要手机号" in AGENT_DECIDE_SYSTEM_PROMPT, "system 禁止有号还问人")
    check("请输入11位手机号" not in AGENT_DECIDE_SYSTEM_PROMPT, "system 不再把问手机号当示例")
    msgs = build_agent_decide_messages(
        goal="进对话页",
        checkpoints_block="（无）",
        device_brief={},
        menu=[{"id": "input_text"}],
        history_block="",
        width=100,
        height=200,
        image_base64="",
        accounts_brief=brief,
    )
    blob = str(msgs)
    check("17633312000" in blob, "decide messages 含申请到的手机号")
    check("上次成功路径" not in blob, "decide 不再注入 baseline_hint")
    check("{baseline_hint" not in AGENT_DECIDE_USER_TEMPLATE, "user 模板已去掉 baseline_hint")

    from server.services.account_tag_service import apply_tag_update, exclusive_drops

    print("\n── 打标互斥：已注册/已登录清掉未注册手机号")
    merged = apply_tag_update(["未注册手机号", "已注册", "已登录"], ["已登录"], ["未注册"])
    check("未注册手机号" not in merged, f"应删未注册手机号, got {merged}")
    check("已登录" in merged and "已注册" in merged, f"已注册与已登录可共存, got {merged}")
    check("未注册手机号" in exclusive_drops(["已注册"], ["未注册手机号"]), "已注册与未注册手机号互斥")
    from server.services.account_tag_service import _normalize_tags
    check(_normalize_tags(["已领取新人礼"]) == ["已领取新人"], "新标签截到 5 字")
    check(_normalize_tags(["未注册手机号"], reuse={"未注册手机号"}) == ["未注册手机号"], "复用旧标签不截断")

    from server.services.regression.agent_executor import history_action_brief

    tap = history_action_brief("tap_element", {"x": 512, "y": 380, "memory_context": "很长" * 80})
    check(tap == "tap_element @512,380", f"tap 历史应短, got {tap!r}")
    check("memory_context" not in tap, "历史不含 memory_context")
    typed = history_action_brief("input_text", {"text": "17633312000", "x": 1, "y": 2})
    check("17633312000" in typed and "@" not in typed, f"input 历史只留文本, got {typed!r}")

    print("\n── 执行链接上了申请号")
    runner = func_source("server/services/regression/case_runner.py", "_execute_on_device")
    check("bind_account_for_case(" in runner, "case_runner 开跑前 bind_account_for_case")
    ask = func_source("server/services/regression/agent_executor.py", "_ask_human")
    check("account_pool" in ask and "_pool_value_for_field" in ask, "ask_human 有号则跳过 HITL")
    run_src = func_source("server/services/regression/agent_executor.py", "run")
    check("_note_issued_account(" in run_src, "Agent 开场记下已申请的号")
    decide = func_source("server/services/ai/regression/planner.py", "decide_next_action")
    check("accounts_brief" in decide, "planner 把号池 brief 传给 decide")
    check("max_tokens=2048" in decide, "decide 输出预算 2048")
    synthetic = func_source("server/services/regression/agent_executor.py", "_record_synthetic")
    check("_emit(" in synthetic and '"result"' in synthetic, "合成步直播推 result")

    print("\n" + ("=== 全部通过 ===" if not failed else f"=== {len(failed)} 条失败 ==="))
    for m in failed:
        print(f"  · {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
