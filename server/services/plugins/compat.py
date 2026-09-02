# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Backward-compat：把新插件系统的数据，重建为旧 list_skills_catalog() 返回形状。

目的：现有 /settings/skills API 与前端 Skills 页不破。
同时在返回里新增 `capabilities` / `executors` / `abstract_caps` / `health` 字段，
为新 UI（U1）提供完整数据。
"""
from __future__ import annotations

from typing import Any

from server.services.plugins import registry
from server.services.plugins.models import Capability, Implementation

# 类目 → 旧 component 元信息（名称 / 分类 / 默认 node_code）
_CATEGORY_META: dict[str, dict[str, str]] = {
    "app_lifecycle": {
        "name": "应用生命周期",
        "category": "应用生命周期",
        "node_code": "capability/app_lifecycle",
    },
    "ui_interaction": {
        "name": "UI 交互",
        "category": "手势 / 输入",
        "node_code": "capability/ui_interaction",
    },
    "system": {
        "name": "系统辅助",
        "category": "系统",
        "node_code": "capability/system",
    },
    "control": {
        "name": "执行控制",
        "category": "控制",
        "node_code": "capability/control",
    },
    "assertion": {
        "name": "断言 / 校验",
        "category": "断言",
        "node_code": "capability/assertion",
    },
    "hitl": {
        "name": "人工介入 (HITL)",
        "category": "人工介入",
        "node_code": "capability/hitl",
    },
    "future": {
        "name": "未来扩展",
        "category": "扩展",
        "node_code": "capability/future",
    },
    "uncategorized": {
        "name": "未分类",
        "category": "其它",
        "node_code": "capability/misc",
    },
}


_EXECUTOR_BADGE: dict[str, str] = {
    "adb": "[ADB]",
    "remote": "[Remote]",
    "ai_persona": "[拟人]",
    "vlm": "[VLM]",
    "hitl": "[HITL]",
    "internal": "[内置]",
}


def _impl_to_invoke_line(impl: Implementation) -> str:
    badge = _EXECUTOR_BADGE.get(impl.executor, f"[{impl.executor}]")
    needs_vlm = " · 需 VLM" if impl.needs_vlm else ""
    cost = f" · cost={impl.cost}"
    name = impl.display_name or impl.id
    return f"{badge} {name}{needs_vlm}{cost}"


def _capability_to_operation(cap: Capability) -> dict[str, Any]:
    """把一个 Capability 序列化为旧 `operations[]` 中的一项。"""
    invoke_lines = [_impl_to_invoke_line(impl) for impl in cap.implementations]
    risk_notes: list[str] = []
    for impl in cap.implementations:
        if impl.executor == "ai_persona":
            risk_notes.append(f"拟人路径成本较高（cost={impl.cost}），UI 失效时可能多步重试")
        if impl.expands_to_events:
            risk_notes.append(f"{impl.id} 会展开为多个子事件")
    return {
        "id": cap.id,
        "name": cap.display_name,
        "description": cap.description,
        "params": [],  # 新系统的 params 由 implementation.low_level 模板决定，运行时填充
        "invoke": "\n".join(invoke_lines) if invoke_lines else "（无可用实现）",
        "triggers": list(cap.trigger_phrases),
        "examples": list(cap.ui.examples),
        "platforms": list(cap.platforms),
        "risk": "；".join(risk_notes),
        # ↓ 新增字段，新 UI 用
        "event_kind": cap.event_kind,
        "needs_vlm": cap.needs_vlm,
        "category": cap.category,
        "implementations": [_impl_to_dict(impl) for impl in cap.implementations],
        "shown_in_settings": cap.ui.shown_in_settings,
    }


def _impl_to_dict(impl: Implementation) -> dict[str, Any]:
    return {
        "id": impl.id,
        "display_name": impl.display_name,
        "executor": impl.executor,
        "requires_caps": list(impl.requires_caps),
        "needs_vlm": impl.needs_vlm,
        "locate_prompt": impl.locate_prompt,
        "prompt_template": impl.prompt_template,
        "low_level": dict(impl.low_level),
        "cost": impl.cost,
        "expands_to_events": impl.expands_to_events,
        "description": impl.description,
    }


def build_executor_components() -> list[dict[str, Any]]:
    """按 category 分组，每组一个旧 ComponentItem。"""
    caps = registry.list_capabilities()

    grouped: dict[str, list[Capability]] = {}
    for cap in caps:
        if not cap.ui.shown_in_settings:
            continue
        grouped.setdefault(cap.category or "uncategorized", []).append(cap)

    # 类目展示顺序
    order = [
        "app_lifecycle",
        "ui_interaction",
        "assertion",
        "system",
        "hitl",
        "control",
        "future",
        "uncategorized",
    ]
    components: list[dict[str, Any]] = []
    for category in order:
        items = grouped.get(category)
        if not items:
            continue
        meta = _CATEGORY_META.get(category, _CATEGORY_META["uncategorized"])
        # platforms = 该 category 下所有 cap 的 platforms 并集
        plats: set[str] = set()
        for cap in items:
            plats.update(cap.platforms)
        components.append(
            {
                "node_code": meta["node_code"],
                "name": meta["name"],
                "category": meta["category"],
                "description": _category_description(category),
                "invoke_type": "plugin",
                "platforms": sorted(plats),
                "risk": "",
                "operations": [_capability_to_operation(cap) for cap in items],
            }
        )
    # 兜底：处理 order 之外的 category（未来新增的）
    for category, items in grouped.items():
        if category in order:
            continue
        meta = _CATEGORY_META.get(category, _CATEGORY_META["uncategorized"])
        plats: set[str] = set()
        for cap in items:
            plats.update(cap.platforms)
        components.append(
            {
                "node_code": meta["node_code"],
                "name": meta["name"],
                "category": meta["category"],
                "description": _category_description(category),
                "invoke_type": "plugin",
                "platforms": sorted(plats),
                "risk": "",
                "operations": [_capability_to_operation(cap) for cap in items],
            }
        )
    return components


def _category_description(category: str) -> str:
    return {
        "app_lifecycle": "应用启停 / 缓存 / 安装等生命周期能力。",
        "ui_interaction": "拟人化 UI 交互：点击、长按、滑动、文本输入、拖拽。",
        "assertion": "视觉断言：把预期文本与当前截图交给 VLM 判断。",
        "system": "系统级辅助：按键、剪贴板、读底层数据等。",
        "hitl": "人工介入：阻塞等待用户输入，AI 自动场景化文案。",
        "control": "执行控制：等待、就绪判断等流程控制能力。",
        "future": "未启用，留作未来扩展。",
    }.get(category, "")


def build_server_groups() -> list[dict[str, Any]]:
    """Server 编排层：描述 Plan / Run / HITL / Plugin 等 meta 能力。"""
    groups = [
        {
            "key": "planning",
            "title": "AI 规划",
            "description": "AI 大脑负责把用例文案拆解为事件序列，并按需调用 VLM 单步决策。",
            "items": [
                {
                    "id": "plan_overview",
                    "name": "整体规划 (PLAN_OVERVIEW_TEXT)",
                    "description": "case 开始时纯文本规划，输出事件序列，不带截图。",
                    "invoke": "ai/plan/prompt · PLAN_OVERVIEW_TEXT（Step 3 实装）",
                },
                {
                    "id": "replan_from_current",
                    "name": "异常单步规划 (REPLAN_FROM_CURRENT)",
                    "description": "执行异常时基于当前截图重新规划剩余事件。",
                    "invoke": "ai/plan/prompt · REPLAN_FROM_CURRENT（Step 4 实装）",
                },
                {
                    "id": "locate_vision",
                    "name": "VLM 元素定位 (LOCATE_VISION)",
                    "description": "单事件执行时由 VLM 给出坐标。",
                    "invoke": "ai/plan/prompt · LOCATE_VISION（Step 4 实装）",
                },
                {
                    "id": "readiness_check",
                    "name": "VLM 就绪判断 (READINESS_CHECK_VISION)",
                    "description": "等待页面进入可交互稳定状态。",
                    "invoke": "ai/plan/prompt · READINESS_CHECK_VISION（Step 4 实装）",
                },
            ],
        },
        {
            "key": "assertion",
            "title": "断言与对账",
            "description": "VLM 视觉断言 + 与上次 baseline 的 diff 报告。",
            "items": [
                {
                    "id": "assertor_vision",
                    "name": "VLM 视觉断言 (ASSERTOR_VISION)",
                    "description": "把预期文本 + 当前截图 + 历史成功截图交给 VLM。",
                    "invoke": "ai/plan/prompt · ASSERTOR_VISION（Step 4 实装）",
                },
                {
                    "id": "diff_summarizer",
                    "name": "差异总结 (DIFF_SUMMARIZER)",
                    "description": "run 结束后对比 baseline，输出回归判定。",
                    "invoke": "ai/plan/prompt · DIFF_SUMMARIZER（Step 7 实装）",
                },
            ],
        },
        {
            "key": "hitl",
            "title": "人工介入",
            "description": "AI 在 plan 阶段预判或执行阶段临时插入 HITL 事件，前端阻塞弹窗。",
            "items": [
                {
                    "id": "hitl_composer",
                    "name": "HITL 文案生成 (HITL_PROMPT_COMPOSER)",
                    "description": "AI 根据上下文写出场景化的问句（含手机号、当前意图等）。",
                    "invoke": "ai/plan/prompt · HITL_PROMPT_COMPOSER（Step 5 实装）",
                },
            ],
        },
        {
            "key": "persona",
            "title": "拟人化任务",
            "description": "系统能力在 adb/remote 都不可用时，AI 把它拆成多步 UI 操作。",
            "items": [
                {
                    "id": "persona_task",
                    "name": "拟人路径规划 (PERSONA_TASK)",
                    "description": "如清缓存：长按图标 → 应用信息 → 存储 → 清除。",
                    "invoke": "ai/plan/prompt · PERSONA_TASK（Step 8 实装）",
                },
            ],
        },
        {
            "key": "test_resources",
            "title": "测试资源",
            "description": "租账号、取口令、备会话。开跑前/填框时由系统调用，不进业务看图菜单。",
            "items": [],
        },
        {
            "key": "plugin_system",
            "title": "插件体系",
            "description": "Capability 与 Executor 都是 YAML 插件，运行时热加载。",
            "items": [
                {
                    "id": "plugin_loader",
                    "name": "插件加载器",
                    "description": "扫描 plugins/ 目录，校验交叉引用。",
                    "invoke": "server.services.plugins.loader",
                },
                {
                    "id": "plugin_reload",
                    "name": "插件热更新",
                    "description": "改 yaml 后无需重启服务。",
                    "invoke": "POST /settings/skills/reload（待挂）",
                },
            ],
        },
    ]
    try:
        from server.services.resources.catalog import list_resource_skills

        items = list_resource_skills()
        for g in groups:
            if g.get("key") == "test_resources":
                g["items"] = items
                break
    except Exception:
        pass
    return groups
    """完整 /settings/skills 返回形状（旧字段兼容 + 新字段扩展）。"""
    components = build_executor_components()
    server_groups = build_server_groups()
    health = registry.health_summary()

    op_count = sum(len(c.get("operations") or []) for c in components)
    server_count = sum(len(g.get("items") or []) for g in server_groups)

    new_capabilities = [
        {
            "id": cap.id,
            "display_name": cap.display_name,
            "event_kind": cap.event_kind,
            "category": cap.category,
            "description": cap.description,
            "platforms": list(cap.platforms),
            "trigger_phrases": list(cap.trigger_phrases),
            "needs_vlm": cap.needs_vlm,
            "implementations": [
                _impl_to_dict(impl) for impl in cap.implementations
            ],
            "ui": cap.ui.model_dump(),
            "source_path": cap.source_path,
        }
        for cap in registry.list_capabilities()
    ]
    new_executors = [
        {
            "id": ex.id,
            "display_name": ex.display_name,
            "description": ex.description,
            "available_when": ex.available_when,
            "provides": list(ex.provides),
            "conditional_provides": list(ex.conditional_provides),
            "platforms": list(ex.platforms),
            "probe": dict(ex.probe) if ex.probe else None,
            "source_path": ex.source_path,
        }
        for ex in registry.list_executors()
    ]
    new_abstract_caps = [
        {"id": ac.id, "description": ac.description, "note": ac.note}
        for ac in registry.list_abstract_caps()
    ]

    return {
        # ↓↓↓ 旧字段（保持兼容）
        "server": {
            "intro": "Server 编排层：AI 规划 / HITL / Plugin 加载等元能力。",
            "groups": server_groups,
        },
        "executor": {
            "intro": "执行器能力按 category 聚合，每个 Capability 列出其 implementations（可用 adb/remote/拟人/VLM）。",
            "components": components,
        },
        "summary": {
            "server_count": server_count,
            "executor_count": op_count,
            "executor_component_count": len(components),
        },
        "platforms": [
            {"id": "android", "label": "Android", "notes": "主路径，adb + Remote 双通道。"},
            {"id": "ios", "label": "iOS", "notes": "依赖 WDA 执行器（占位中）。"},
            {"id": "mac", "label": "macOS", "notes": "依赖 AppleScript 执行器（占位中）。"},
            {"id": "windows", "label": "Windows", "notes": "依赖 WinAPI 执行器（占位中）。"},
            {"id": "web", "label": "Web", "notes": "依赖 CDP 执行器（占位中）。"},
        ],
        # ↓↓↓ 新字段（U1 新 UI 用）
        "capabilities": new_capabilities,
        "executors": new_executors,
        "abstract_caps": new_abstract_caps,
        "health": health,
    }
