# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""对话流 WebSocket：规划 + 执行。"""
from __future__ import annotations

import uuid

from server.services import copilot_service as cs
from server.services.executor.plan_execute_service import execute_planned_steps_with_drift_replan


def handle_copilot_chat(websocket, data: dict):
    text = (data.get("text") or data.get("message") or "").strip()
    sn = data.get("sn")
    plan = cs.plan_message(
        text,
        sn=sn,
        context=data.get("context"),
        channel="copilot",
        provider_id=data.get("provider_id") or data.get("providerId"),
        planning_mode=data.get("planning_mode") or data.get("planningMode") or "local",
    )
    return {"code": 200, "data": plan}


def handle_copilot_execute(websocket, data: dict):
    steps = data.get("steps") or []
    sn = data.get("sn")
    platform = (data.get("platform") or "android").lower()
    if not steps:
        return {"code": 400, "msg": "steps 为空"}
    planning_mode = str(
        data.get("planning_mode") or data.get("planningMode") or "local"
    ).lower()
    instruction = (
        data.get("instruction")
        or data.get("text")
        or data.get("command")
        or ""
    ).strip()
    run_id = str(data.get("run_id") or data.get("runId") or f"copilot-{uuid.uuid4().hex[:10]}")
    payload = execute_planned_steps_with_drift_replan(
        steps,
        instruction=instruction,
        sn=sn,
        platform=platform,
        context=data.get("context") or {},
        run_id=run_id,
        app_id=str(data.get("app_id") or data.get("appId") or ""),
        target_package=str(
            data.get("package")
            or data.get("target_package")
            or (data.get("context") or {}).get("package")
            or ""
        ),
        planning_mode=planning_mode,
        channel="copilot",
        provider_id=data.get("provider_id") or data.get("providerId"),
    )
    return {"code": 200, "data": payload}
