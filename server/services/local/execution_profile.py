# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Local 用例执行 profile：页面恢复、弹窗守卫、本地断言。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from script.log import SLog

TAG = "LocalExecutionProfile"


class LocalExecutionProfile:
    mode = "local"

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
        if not app_id:
            return None
        from server.services.local.navigation.page_navigation_service import (
            ensure_page_ready_before_action,
        )

        return ensure_page_ready_before_action(
            sn=sn,
            platform=platform,
            app_id=app_id,
            session=session,
            step_text=step_text,
            icon_targets=icon_targets,
            run_id=run_id,
            target_package=target_package,
        )

    def before_verify(
        self,
        engine,
        screen_w: int,
        screen_h: int,
        *,
        sn: str = "",
        platform: str = "android",
    ) -> List[Dict[str, Any]]:
        from script.sleep import mSleep
        from server.services.local.overlay.overlay_guard_service import (
            run_overlay_guard_until_clear,
        )

        gestures: List[Dict[str, Any]] = []
        try:
            guard_out = run_overlay_guard_until_clear(
                engine, screen_w, screen_h, max_rounds=5
            )
            for it in guard_out.get("iterations") or []:
                g = (it.get("action") or {}).get("gesture")
                if g and g not in gestures:
                    gestures.append(g)
            mSleep(0.4)
        except Exception as e:
            SLog.w(TAG, f"prepare screen for verify failed: {e}")
        return gestures

    def should_attempt_page_recovery(self) -> bool:
        return True

    def should_attempt_post_action_recovery(
        self,
        *,
        pre_action_recovery: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if pre_action_recovery and (
            pre_action_recovery.get("attempted")
            or pre_action_recovery.get("overlay_guard_delegated")
        ):
            return False
        return True
