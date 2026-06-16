# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""用例/回归执行 profile：区分 AI 自主与本地写死前置逻辑。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class ExecutionProfile(Protocol):
    """编排层依赖的执行前置/校验策略。"""

    mode: str  # "ai" | "local"

    def before_action(
        self,
        *,
        sn: str,
        platform: str,
        app_id: str,
        session,
        step_text: str,
        icon_targets: Optional[List[Dict[str, Any]]] = None,
        run_id: str = "",
        target_package: str = "",
    ) -> Optional[Dict[str, Any]]:
        """操作前页面恢复 / 本地前置。"""

    def before_verify(
        self,
        engine,
        screen_w: int,
        screen_h: int,
        *,
        sn: str = "",
        platform: str = "android",
    ) -> List[Dict[str, Any]]:
        """校验前清弹窗 / 屏幕准备。"""

    def should_attempt_page_recovery(self) -> bool:
        """校验失败后是否尝试本地页面恢复。"""

    def should_attempt_post_action_recovery(
        self,
        *,
        pre_action_recovery: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """操作失败后是否尝试本地 post-recovery。"""


def resolve_execution_profile(
    channel: str = "case_execution",
    provider_id: Optional[str] = None,
) -> ExecutionProfile:
    """按通道配置解析 AI / Local profile。"""
    try:
        from server.services import system_settings_service as ss

        if ss.should_use_ai_planning(channel, provider_id=provider_id).get("enabled"):
            from server.services.ai.execution_profile import AiExecutionProfile

            return AiExecutionProfile(channel=channel, provider_id=provider_id)
    except Exception:
        pass
    from server.services.local.execution_profile import LocalExecutionProfile

    return LocalExecutionProfile(channel=channel, provider_id=provider_id)
