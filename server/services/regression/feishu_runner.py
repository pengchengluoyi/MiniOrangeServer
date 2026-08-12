# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Deprecated shim — 旧的 feishu_runner 名字已经迁移到 case_runner。

保留是为了任何外部代码 / 测试脚本 `from ... import feishu_runner` 不立刻爆。
新代码请直接 import server.services.regression.case_runner。
"""
from __future__ import annotations

from server.services.regression.case_runner import (  # noqa: F401
    TAG,
    _LOCK,
    _RUNS,
    CapabilityRouter,
    RunContext,
    build_run_context,
    case_memory,
    get_baseline_brief,
    get_run as get_ai_led_run,  # backward-compat 别名
    get_trace_detail,
    list_runs as list_ai_led_runs,
    list_recent_traces,
    memory_repo,
    normalize_feishu_case,
    promote_run,
    run_cases as run_ai_led_cases,
    to_case_spec,
)
