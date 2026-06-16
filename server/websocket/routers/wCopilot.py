# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""对话流 WebSocket：规划 + 执行。"""
from __future__ import annotations

import uuid

from server.services import copilot_service as cs
from server.services.executor.execute_steps import execute_steps


async def handle_copilot_chat(websocket, data: dict):
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


async def handle_copilot_execute(websocket, data: dict):
    steps = data.get("steps") or []
    sn = data.get("sn")
    platform = (data.get("platform") or "android").lower()
    if not steps:
        return {"code": 400, "msg": "steps 为空"}
    results = execute_steps(
        steps,
        sn=sn,
        platform=platform,
        run_id=str(data.get("run_id") or data.get("runId") or f"copilot-{uuid.uuid4().hex[:10]}"),
        capture_screenshots=bool(data.get("capture_screenshots", True)),
        app_id=str(data.get("app_id") or data.get("appId") or ""),
    )
    ok_all = all(r.get("ok") for r in results) if results else False
    return {
        "code": 200,
        "data": {
            "results": results,
            "ok": ok_all,
            "msg": "全部成功" if ok_all else "部分步骤失败",
        },
    }
