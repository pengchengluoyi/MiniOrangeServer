# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""AI 用例执行 profile：弹窗/页面就绪/Overlay Guard 均交给大模型 Plan，本地不写死前置。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class AiExecutionProfile:
    mode = "ai"

    def __init__(
        self,
        *,
        channel: str = "case_execution",
        provider_id: Optional[str] = None,
    ) -> None:
        self.channel = channel
        self.provider_id = provider_id

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
        return None

    def before_verify(
        self,
        engine,
        screen_w: int,
        screen_h: int,
        *,
        sn: str = "",
        platform: str = "android",
    ) -> List[Dict[str, Any]]:
        return []

    def should_attempt_page_recovery(self) -> bool:
        return False

    def should_attempt_post_action_recovery(
        self,
        *,
        pre_action_recovery: Optional[Dict[str, Any]] = None,
    ) -> bool:
        return False
