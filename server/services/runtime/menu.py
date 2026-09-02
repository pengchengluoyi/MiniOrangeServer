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


def _cap_summary(cap: Capability) -> str:
    text = (cap.description or "").strip().splitlines()[0].strip()
    return text[:120] if text else ""


def _executor_costs(cap: Capability) -> list[dict[str, Any]]:
    """同一 executor 只留 cost 最低的一条。规划模型选通道用，不需要 impl id / requires_caps。"""
    seen: dict[str, int] = {}
    for impl in sorted(cap.implementations or [], key=lambda i: getattr(i, "cost", 5)):
        ex = str(getattr(impl, "executor", "") or "").strip()
        if not ex or ex in seen:
            continue
        seen[ex] = int(getattr(impl, "cost", 5))
    return [{"executor": ex, "cost": cost} for ex, cost in seen.items()]


def available_menu_brief(
    ctx: RunContext,
    *,
    audience: str = "case",
    kind: str = "plan",
) -> list[dict[str, Any]]:
    """喂给大模型的能力菜单。YAML 里的 trigger_phrases / platforms / 实现细节不进 prompt。

    kind="plan"  文本规划 / 拟人展开：id + summary + implementations[{executor, cost}]
    kind="agent" 看图决策：只给 id + summary。点哪、走哪条通道由系统提示词和 Router 负责。

    audience 决定按 capability.visible_to 过滤：业务菜单不该出现系统层专用能力。
    """
    kind = (kind or "plan").strip().lower()
    if kind not in {"plan", "agent"}:
        kind = "plan"
    out: list[dict[str, Any]] = []
    for cap in available_capabilities(ctx):
        if not _visible_to(cap, audience):
            continue
        row: dict[str, Any] = {"id": cap.id}
        summary = _cap_summary(cap)
        if summary:
            row["summary"] = summary
        if kind == "plan":
            impls = _executor_costs(cap)
            if impls:
                row["implementations"] = impls
        out.append(row)
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
