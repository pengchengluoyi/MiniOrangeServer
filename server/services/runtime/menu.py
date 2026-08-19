# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""把 RunContext 的连通性结果喂给 plugins.registry，得到当前 Run 可用的 capability 菜单。

这是 PLAN_OVERVIEW_TEXT 之前最关键的一步：决定 AI 能"看见"哪些能力。
"""
from __future__ import annotations

from typing import Any

from server.services.plugins import registry as plugin_registry
from server.services.plugins.models import Capability
from server.services.runtime.run_context import RunContext


def available_capabilities(ctx: RunContext) -> list[Capability]:
    """返回当前 RunContext 下可用的 capability 列表（每项 implementations 已过滤）。"""
    return plugin_registry.filter_capabilities_by_connectivity(ctx.connectivity_flags)


def _visible_to(cap: Capability, audience: str) -> bool:
    """能力对该受众是否可见。

    audience="case"   业务用例决策 agent（默认，行为与改造前一致）
    audience="system" L0 系统层处置 agent
    audience="all"    不过滤（Skills 页 / 诊断用）
    老 yaml 不写 visible_to 时默认 ["case","system"]，两个受众都能看到。
    """
    if audience == "all":
        return True
    allowed = [str(x).strip().lower() for x in (getattr(cap, "visible_to", None) or [])]
    if not allowed or "both" in allowed:
        return True
    return audience in allowed


def available_menu_brief(ctx: RunContext, *, audience: str = "case") -> list[dict[str, Any]]:
    """喂给 PLAN_OVERVIEW prompt 的"菜单"精简结构。

    刻意去掉文档级字段（description / ui / examples），只保留决策时必需信息：
      - id / type / needs_vlm / implementations[ {executor, requires_caps} ]

    audience 决定按 capability.visible_to 过滤：业务菜单不该出现系统层专用能力
    （否则业务 agent 会自己去调，白烧决策预算）。
    """
    caps = [c for c in available_capabilities(ctx) if _visible_to(c, audience)]
    out: list[dict[str, Any]] = []
    for cap in caps:
        is_hitl = (cap.category or "").lower() == "hitl"
        out.append({
            "id": cap.id,
            "event_kind": cap.event_kind,
            "category": cap.category,
            "needs_vlm": cap.needs_vlm,
            "is_human_in_the_loop": is_hitl,
            "summary": (cap.description or "").strip().splitlines()[0][:160] if cap.description else "",
            "platforms": list(cap.platforms or []),
            "trigger_phrases": list(cap.trigger_phrases or []),
            "implementations": [
                {
                    "id": impl.id,
                    "executor": impl.executor,
                    "requires_caps": list(impl.requires_caps or []),
                    "needs_vlm": impl.needs_vlm,
                    # cost 越低越优先（adb 系统级实现通常 cost 更低）。双通道在线时两渠道
                    # implementations 都在此列出，按 cost 升序引导大模型优先选 adb。
                    "cost": getattr(impl, "cost", 5),
                    "notes": (impl.description or "")[:160],
                }
                for impl in sorted(cap.implementations, key=lambda i: getattr(i, "cost", 5))
            ],
        })
    return out


def capability_menu_diagnostics(ctx: RunContext) -> dict[str, Any]:
    """诊断模式：包含被过滤掉的 capability 及原因，便于 UI 显示"为什么这个事件不可用"。"""
    from server.services.plugins.loader import get_loader

    loader = get_loader()
    flags = ctx.connectivity_flags

    # 计算所有可用 executor 的能力并集（和 registry.filter_capabilities_by_connectivity 同语义）
    executor_available: dict[str, bool] = {}
    executor_caps: dict[str, set[str]] = {}
    for exec_id, executor in loader.executors.items():
        from server.services.plugins.registry import _executor_available

        is_avail = _executor_available(executor, flags)
        executor_available[exec_id] = is_avail
        caps: set[str] = set()
        if is_avail:
            caps = set(executor.provides)
            for cond in executor.conditional_provides or []:
                cap = cond.get("cap")
                if cap and flags.get(cap, False):
                    caps.add(cap)
        executor_caps[exec_id] = caps
    executor_available.setdefault("internal", True)
    executor_caps.setdefault("internal", set())
    globally_available_caps: set[str] = set()
    for exec_id, is_avail in executor_available.items():
        if is_avail:
            globally_available_caps.update(executor_caps.get(exec_id, set()))

    available_ids = {c.id for c in available_capabilities(ctx)}
    dropped: list[dict[str, Any]] = []
    for cap_id, cap in loader.capabilities.items():
        if cap_id in available_ids:
            continue
        reasons: list[str] = []
        for impl in cap.implementations:
            if impl.executor not in executor_available or not executor_available[impl.executor]:
                reasons.append(f"executor `{impl.executor}` not connected")
                continue
            missing = [c for c in impl.requires_caps if c not in globally_available_caps]
            if missing:
                reasons.append(
                    f"impl `{impl.id}`: requires_caps {missing} not satisfied by any executor"
                )
        dropped.append({
            "id": cap_id,
            "needs_vlm": cap.needs_vlm,
            "category": cap.category,
            "reasons": list({r for r in reasons}) or ["unknown"],
        })
    return {
        "flags": flags,
        "executor_available": executor_available,
        "globally_available_caps": sorted(globally_available_caps),
        "available_count": len(available_ids),
        "dropped": dropped,
    }
