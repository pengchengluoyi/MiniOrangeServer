# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Runtime context：每次 run 启动时探测设备 / adb / vlm / hitl 通道，构造 RunContext。

公开入口：
    from server.services.runtime import build_run_context, RunContext

    ctx = build_run_context(sn=sn, platform="android")
    brief = ctx.to_prompt_brief()   # 注入到 PLAN_OVERVIEW prompt
    flags = ctx.connectivity_flags  # 喂给 plugins.registry 过滤菜单
"""
from __future__ import annotations

from server.services.runtime.run_context import (  # noqa: F401
    RunContext,
    build_run_context,
)
from server.services.runtime.menu import (  # noqa: F401
    available_capabilities,
    available_menu_brief,
    capability_menu_diagnostics,
)
