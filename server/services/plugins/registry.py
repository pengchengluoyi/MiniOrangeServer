# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Plugin 注册表的公开查询 API。

所有上层（PLAN_OVERVIEW prompt 构造、Capability Router、Skills 页 API）
都从这里访问 capability/executor 数据。
"""
from __future__ import annotations

from typing import Any, Optional

from server.services.plugins.loader import get_loader, force_reload
from server.services.plugins.models import (
    AbstractCap,
    Capability,
    Executor,
    Implementation,
    LoadError,
)

# ---------- 列表查询 ----------


def list_abstract_caps() -> list[AbstractCap]:
    return list(get_loader().abstract_caps.values())


def list_executors(*, include_disabled: bool = False) -> list[Executor]:
    # include_disabled 暂未启用（.disabled 文件已被加载器跳过），保留参数预留未来扩展
    return list(get_loader().executors.values())


def list_capabilities() -> list[Capability]:
    return list(get_loader().capabilities.values())


def list_recovery_rules(*, enabled_only: bool = True) -> list:
    """L0 恢复规则，按 priority 降序。enabled_only 过滤掉 draft/deprecated。"""
    rules = list(get_loader().recovery_rules.values())
    if enabled_only:
        rules = [r for r in rules if r.enabled and r.lifecycle == "active"]
    return sorted(rules, key=lambda r: -int(getattr(r, "priority", 0)))


def get_recovery_rule(rule_id: str):
    return get_loader().recovery_rules.get(rule_id)


def list_load_errors() -> list[LoadError]:
    return list(get_loader().errors)


# ---------- 单项查询 ----------


def get_capability(capability_id: str) -> Optional[Capability]:
    return get_loader().capabilities.get(capability_id)


def get_executor(executor_id: str) -> Optional[Executor]:
    return get_loader().executors.get(executor_id)


def get_abstract_cap(cap_id: str) -> Optional[AbstractCap]:
    return get_loader().abstract_caps.get(cap_id)


# ---------- Connectivity 过滤 ----------


ConnectivityFlags = dict[str, bool]
"""执行器层面的连通性标记。常见 key：
    adb, remote, vlm, hitl, web, playwright, pc, mac, ios_wda
"""


def _executor_available(executor: Executor, connectivity: ConnectivityFlags) -> bool:
    """根据 executor.available_when 判断当前是否可用。

    available_when 支持的形式：
      - 空字符串 / 缺省 → 永远可用
      - "connectivity.adb" → connectivity["adb"] is True
      - "connectivity.remote" → connectivity["remote"]
      - "provider.vlm.available" → connectivity.get("vlm", False)
      - "connectivity.frontend" → connectivity.get("hitl", False) or connectivity.get("frontend", False)
    """
    expr = (executor.available_when or "").strip()
    if not expr:
        return True
    # 标准化键名：把 dotted-path 末段做映射
    last = expr.rsplit(".", 1)[-1].lower()
    alias = {
        "adb": "adb",
        "remote": "remote",
        "frontend": "hitl",
        "available": "vlm" if "vlm" in expr.lower() else last,
        "web": "web",
        "pc": "pc",
        "mac": "mac",
        "ios_wda": "ios_wda",
        "playwright": "playwright",
        "hitl": "hitl",
    }
    key = alias.get(last, last)
    return bool(connectivity.get(key, False))


def filter_capabilities_by_connectivity(
    connectivity: ConnectivityFlags,
    *,
    drop_when_no_impl: bool = True,
) -> list[Capability]:
    """按当前连通性过滤 capabilities。

    语义：
      1. 每个 implementation 必须满足两个独立条件：
         a) `impl.executor` 自身在当前 connectivity 下可用
         b) `impl.requires_caps` 中每一项，**在所有可用 executor 的并集中**都能找到
         （之所以是"并集"——比如 VLM 的 assert 实现需要 `ui_screenshot`，
           但截图是 orchestrator 用 adb/remote 提前抓的，不是 VLM 自己抓的）
      2. 整 capability 没有任何 impl 存活时，按 drop_when_no_impl 决定是否丢弃
      3. 返回浅拷贝，原 loader 实例不变

    返回的 capability 列表用于塞进 PLAN_OVERVIEW prompt 的"菜单"。
    """
    loader = get_loader()
    executors = loader.executors

    # 计算每个 executor 是否可用 + 实际能提供的 caps
    executor_available: dict[str, bool] = {}
    executor_caps: dict[str, set[str]] = {}
    for exec_id, executor in executors.items():
        is_avail = _executor_available(executor, connectivity)
        executor_available[exec_id] = is_avail
        if not is_avail:
            executor_caps[exec_id] = set()
            continue
        caps = set(executor.provides)
        for cond in executor.conditional_provides or []:
            cap = cond.get("cap")
            if cap and connectivity.get(cap, False):
                caps.add(cap)
        executor_caps[exec_id] = caps
    # internal 执行器（如 wait_ms 的 noop sleep）永远可用、永远满足
    executor_available.setdefault("internal", True)
    executor_caps.setdefault("internal", set())

    # 并集：所有可用 executor 的 provides，用于 requires_caps 满足判断
    globally_available_caps: set[str] = set()
    for exec_id, is_avail in executor_available.items():
        if is_avail:
            globally_available_caps.update(executor_caps.get(exec_id, set()))

    out: list[Capability] = []
    for cap in loader.capabilities.values():
        kept: list[Implementation] = []
        for impl in cap.implementations:
            if impl.executor not in executor_available:
                # 引用了未知 executor，跳过
                continue
            if not executor_available[impl.executor]:
                continue
            # requires_caps 在全局并集中查
            if all(c in globally_available_caps for c in impl.requires_caps):
                kept.append(impl)
        if kept or not drop_when_no_impl:
            new_cap = cap.model_copy(update={"implementations": kept})
            out.append(new_cap)
    return out


# ---------- 调试 / 管理 ----------


def reload() -> dict[str, Any]:
    """强制热更新插件。Settings 页可暴露按钮调用。"""
    loader = force_reload()
    return {
        "abstract_caps": len(loader.abstract_caps),
        "executors": len(loader.executors),
        "capabilities": len(loader.capabilities),
        "errors": [e.model_dump() for e in loader.errors],
    }


def health_summary() -> dict[str, Any]:
    loader = get_loader()
    return {
        "root": str(loader.root),
        "abstract_caps_count": len(loader.abstract_caps),
        "executors_count": len(loader.executors),
        "capabilities_count": len(loader.capabilities),
        "errors": [e.model_dump() for e in loader.errors],
    }
