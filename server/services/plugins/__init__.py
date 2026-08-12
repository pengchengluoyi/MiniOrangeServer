# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Capability 插件系统：从 plugins/ 目录加载 YAML 配置。

公开入口：
    from server.services.plugins import registry

    capabilities = registry.list_capabilities()
    executors = registry.list_executors()
    cap = registry.get_capability("tap_element")
    available = registry.filter_capabilities_by_connectivity({"adb": True, "remote": False})
"""
from __future__ import annotations

from server.services.plugins import registry  # noqa: F401
