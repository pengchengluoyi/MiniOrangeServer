# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Regression orchestration（执行循环 + Router + 5 个 Executor）。

模块布局：
    executors/       — 每个执行通道一个 Executor 实现（adb / remote / vlm / hitl / ai_persona）
    screen.py        — 抓截图统一入口
    router.py        — CapabilityRouter（按 expected/fallback 选 executor + 抓图分发）
    orchestrator.py  — 顶层循环：plan → loop events → fail → replan → 汇总

公开入口：
    from server.services.regression import Orchestrator, run_case
"""
from __future__ import annotations

from server.services.regression.orchestrator import (  # noqa: F401
    Orchestrator,
    OrchestratorOptions,
    run_case,
)
from server.services.regression.router import CapabilityRouter  # noqa: F401
from server.services.regression.screen import capture_screen, CapturedScreen  # noqa: F401
from server.services.regression.screen import (  # noqa: F401
    classify_clawnode_screenshot_hint,
    parse_clawnode_screenshot_response,
    screenshot_failure_meta,
)
