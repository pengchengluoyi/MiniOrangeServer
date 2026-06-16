# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""步骤执行后页面上下文识别。"""
from __future__ import annotations

from typing import Any, Dict

from script.log import SLog

TAG = "CopilotExecutor"

def _attach_step_page_context(
    out: Dict[str, Any],
    *,
    sn: str,
    platform: str,
    app_id: str,
    wait_ms: int = 600,
    run_id: str = "",
) -> None:
    """执行后识别当前页，写入步骤结果供回放/断言参考。"""
    if not sn or not app_id:
        return
    if out.get("kind") not in ("click", "open_app", "close_app", "swipe"):
        return
    try:
        from server.services.shared.run_context.regression_run_context import get_ctx

        if get_ctx() and get_ctx().get("run_id"):
            return
    except Exception:
        pass
    try:
        import builtins
        from script.sleep import mSleep
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
        from server.services.shared.page_context.page_context_service import (
            get_engine_screen_snapshot,
            identify_for_app,
        )

        if wait_ms > 0:
            mSleep(min(wait_ms, 250) / 1000.0)

        builtins.TARGET_DEVICE_SN = str(sn)
        engine, (w, h) = bootstrap_mobile_engine(str(sn), platform)
        blob = get_engine_screen_snapshot(engine).get("blob") or ""
        from server.services.shared.page_context.page_context_service import enrich_page_context_screenshot

        pc = identify_for_app(
            str(app_id), engine, frame_count=1, screen_text=blob
        )
        pc = enrich_page_context_screenshot(
            pc,
            sn=str(sn),
            platform=platform,
            run_id=str(run_id or ""),
            tag=f"step_page_{out.get('index', 0)}",
        )
        out["current_page"] = pc.get("label")
        out["current_page_score"] = pc.get("score")
        out["current_page_matched"] = pc.get("matched")
        out["current_page_id"] = pc.get("node_id")
        out["page_context"] = pc
    except Exception as e:
        SLog.w(TAG, f"step page context failed: {e}")
