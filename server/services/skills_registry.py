# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Server / 执行器能力目录 —— 兼容入口。

本模块从 v2 开始变为薄壳，真实数据来自 plugins/ 下的 YAML 声明，
由 server.services.plugins.{loader, registry, compat} 提供。

旧的函数签名 (list_executor_components / list_server_groups / list_skills_catalog)
保持不变，前端 /settings/skills 不需要修改。

如果要新增能力，请编辑 plugins/capabilities/*.yaml 而不是这里。
如果要扩展抽象能力或新执行器，请编辑 plugins/abstract_caps.yaml 或 plugins/executors/*.yaml。
"""
from __future__ import annotations

from typing import Any, Dict, List

from server.services.plugins import compat as _compat
from server.services.plugins import registry as _registry

# ---------- 旧 API (保持兼容) ----------


def list_executor_components() -> List[Dict[str, Any]]:
    """执行器能力按 category 聚合（每个 Capability 作为一个 operation）。"""
    return _compat.build_executor_components()


def list_server_groups() -> List[Dict[str, Any]]:
    """Server 编排层 meta 能力（AI 规划 / HITL / Plugin 等）。"""
    return _compat.build_server_groups()


def list_skills_catalog() -> Dict[str, Any]:
    """完整 Skills 目录（旧字段兼容 + 新字段扩展）。"""
    return _compat.build_skills_catalog()


# ---------- 新 API (运维 / 新 UI 用) ----------


def reload_plugins() -> Dict[str, Any]:
    """强制热更新插件（运营改 YAML 后调用）。"""
    return _registry.reload()


def plugin_health() -> Dict[str, Any]:
    """插件加载健康状态（出错文件清单等）。"""
    return _registry.health_summary()
