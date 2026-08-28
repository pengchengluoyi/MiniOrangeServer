# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""对话流 WebSocket：与 CaseRunner 同一套 Agent 下发。"""
from __future__ import annotations

from server.core.database import SessionLocal
from server.models.project import App
from server.services import copilot_service as cs
from server.services.regression import case_runner as cr
from server.services.regression import task_store
from sqlalchemy.orm import joinedload

_DEVICE_STEP_KINDS = {"click", "swipe", "open_app", "close_app", "system_key", "input"}


def handle_copilot_chat(websocket, data: dict):
    text = (data.get("text") or data.get("message") or "").strip()
    sn = str(data.get("sn") or "").strip()
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    app_id = str(
        data.get("app_id") or data.get("appId") or context.get("app_id") or context.get("appId") or ""
    ).strip()
    provider_id = str(
        data.get("provider_id") or data.get("providerId") or context.get("provider_id") or ""
    ).strip()

    local = cs.plan_message(
        text,
        sn=sn,
        context=context,
        channel="copilot",
        planning_mode="local",
    )
    steps = list(local.get("steps") or [])
    deviceish = any(str(s.get("kind") or "") in _DEVICE_STEP_KINDS for s in steps if isinstance(s, dict))
    if local.get("navigate") and not deviceish:
        local["engine"] = "navigate"
        local["auto_run"] = True
        return {"code": 200, "data": local}

    if not text:
        return {"code": 400, "msg": "请输入要执行的指令"}
    if not sn:
        return {"code": 400, "msg": "请选择在线设备"}
    if not app_id:
        return {"code": 400, "msg": "请选择应用后再下发 Agent 任务"}

    busy = task_store.busy_task_for_sn(sn)
    if busy:
        return {
            "code": 409,
            "msg": "设备占用中",
            "data": {"busy_task_id": busy, "sn": sn},
        }

    with SessionLocal() as db:
        app = db.query(App).options(joinedload(App.project)).filter(App.id == app_id).first()
        if app is None:
            return {"code": 404, "msg": "App not found"}
        snapshot = cr.run_cases(
            app,
            sn=sn,
            sns=[sn],
            coverage="once",
            platform=str(context.get("platform") or "android"),
            db=db,
            async_exec=True,
            run_type="copilot",
            instruction=text,
            provider_id=provider_id,
        )
    run_id = str(snapshot.get("run_id") or "")
    return {
        "code": 200,
        "data": {
            "reply": f"已下发 Agent 任务 {run_id}",
            "display_reply": f"已下发 Agent 任务 {run_id}，正在看图执行。",
            "engine": "agent",
            "auto_run": True,
            "run_id": run_id,
            "task_id": run_id,
            "sn": sn,
            "app_id": app_id,
            "provider_id": provider_id,
            "steps": [],
            "plan_complete": False,
        },
    }


def handle_copilot_execute(websocket, data: dict):
    """旧逐步执行入口：转发为同一套 Agent 下发。"""
    instruction = (
        data.get("instruction") or data.get("text") or data.get("command") or ""
    ).strip()
    if not instruction:
        steps = data.get("steps") or []
        bits = []
        for s in steps:
            if isinstance(s, dict):
                bits.append(str(s.get("summary") or s.get("kind") or "").strip())
        instruction = "；".join(b for b in bits if b)
    if not instruction:
        return {"code": 400, "msg": "请通过对话下发 Agent 任务（需要自然语言指令）"}
    payload = dict(data or {})
    payload["text"] = instruction
    return handle_copilot_chat(websocket, payload)
