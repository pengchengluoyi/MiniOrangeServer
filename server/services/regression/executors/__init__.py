# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""执行通道：adb / remote / ios_wda / playwright / vlm / hitl / ai_persona。

每个 Executor 实现统一接口 execute(event, ctx) → EventResult。
Router 根据 capability + connectivity 把事件分发到正确的 Executor。
"""
from __future__ import annotations

from server.services.regression.executors.adb_executor import AdbExecutor  # noqa: F401
from server.services.regression.executors.ai_persona_executor import AiPersonaExecutor  # noqa: F401
from server.services.regression.executors.base import (  # noqa: F401
    Executor,
    ExecutorContext,
)
from server.services.regression.executors.hitl_executor import HitlExecutor  # noqa: F401
from server.services.regression.executors.internal_executor import InternalExecutor  # noqa: F401
from server.services.regression.executors.ios_wda_executor import IosWdaExecutor  # noqa: F401
from server.services.regression.executors.playwright_executor import PlaywrightExecutor  # noqa: F401
from server.services.regression.executors.remote_executor import RemoteExecutor  # noqa: F401
from server.services.regression.executors.vlm_executor import VlmExecutor  # noqa: F401


def build_default_executors() -> dict[str, Executor]:
    """Router 默认注册的执行器集合。"""
    return {
        "internal": InternalExecutor(),
        "adb": AdbExecutor(),
        "remote": RemoteExecutor(),
        "vlm": VlmExecutor(),
        "hitl": HitlExecutor(),
        "ai_persona": AiPersonaExecutor(),
        "ios_wda": IosWdaExecutor(),
        "playwright": PlaywrightExecutor(),
    }
