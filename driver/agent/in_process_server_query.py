# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
在「与 main 同机、同 Python 环境」的子进程里（如 actuator.process_runner_wrapper）
提供与 WebSocket SERVER_QUERY 等价的同步查询，避免 ServerBridge 报错。

仅实现 Orchestrator / Memory 实际会用到的 action。
"""
from __future__ import annotations

import base64
import builtins
import json
import os
from typing import Any, Optional

from script.log import SLog

TAG = "InProcessQuery"


def _truncate(s: str, n: int = 400) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def _handle_get_device_password(params: dict) -> Optional[dict]:
    sn = params.get("sn")
    if not sn:
        return None
    from server.services.device_service import DeviceService

    password = DeviceService.get_password(sn)
    if password is None and not DeviceService.get_by_sn(sn):
        return None
    return {"code": 200, "data": {"password": password}}


def _handle_get_workflow_detail(params: dict) -> Optional[dict]:
    flow_id = params.get("flow_id")
    if not flow_id:
        return None
    inline = getattr(builtins, "WORKFLOW_INLINE_NODES", None)
    if isinstance(inline, dict) and inline:
        return {"id": None, "name": "inline", "nodes": inline, "updated_at": None}

    from server.core.database import SessionLocal
    from server.models.workflow import Workflow

    session = SessionLocal()
    try:
        wf = session.query(Workflow).filter(Workflow.id == int(flow_id)).first()
        if not wf:
            return None
        try:
            nodes_json = json.loads(wf.nodes) if wf.nodes else {}
        except json.JSONDecodeError:
            nodes_json = {}
        return {
            "id": wf.id,
            "name": wf.name,
            "nodes": nodes_json,
            "updated_at": str(wf.updated_at) if wf.updated_at else None,
        }
    finally:
        session.close()


def _handle_sync_timeline(params: dict) -> dict:
    run_id = params.get("run_id")
    timeline = params.get("timeline", {})
    if not run_id or not timeline:
        return {"code": 400, "msg": "Missing run_id or timeline"}

    from server.core.database import SessionLocal
    from server.models.timeline import TaskTimeline

    session = SessionLocal()
    try:
        for ts, item in timeline.items():
            session.add(
                TaskTimeline(
                    run_id=str(run_id),
                    timestamp=int(ts),
                    event_type=item.get("type"),
                    event_data=str(item.get("data")),
                )
            )
        session.commit()
        return {"code": 200, "msg": "Timeline synced"}
    except Exception as e:
        session.rollback()
        SLog.e(TAG, f"sync_timeline: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


def _handle_upload(params: dict) -> dict:
    from server.websocket.wsFile import UPLOAD_DIR

    file_name = params.get("name")
    content_b64 = params.get("content")
    if not file_name or not content_b64:
        return {"code": 400, "msg": "Missing name or content"}
    file_name = os.path.basename(file_name)
    file_path = os.path.join(UPLOAD_DIR, file_name)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    if "," in content_b64:
        content_b64 = content_b64.split(",", 1)[1]
    with open(file_path, "wb") as f:
        f.write(base64.b64decode(content_b64))
    return {
        "code": 200,
        "msg": "Upload success",
        "filename": file_name,
        "path": file_path,
        "url": f"/static/{file_name}",
    }


def _handle_get_world_model(_params: dict) -> dict:
    return {"data": {}}


def _handle_get_app_graph(_params: dict) -> dict:
    return {"nodes": [], "edges": []}


def in_process_server_query(action: str, params: dict | None = None, timeout: int = 10) -> Any:
    params = params or {}
    try:
        if action == "get_device_password":
            return _handle_get_device_password(params)
        if action == "get_workflow_detail":
            return _handle_get_workflow_detail(params)
        if action == "sync_timeline":
            return _handle_sync_timeline(params)
        if action == "upload":
            return _handle_upload(params)
        if action == "get_world_model":
            return _handle_get_world_model(params)
        if action == "get_app_graph":
            return _handle_get_app_graph(params)
    except Exception as e:
        SLog.e(TAG, f"{action} failed: {e}")
        return None
    SLog.w(TAG, f"Unknown in-process action: {action}")
    return None


def install_in_process_server_query() -> None:
    """注入 builtins.SERVER_QUERY，供 ServerBridge 使用。"""
    builtins.SERVER_QUERY = in_process_server_query
    SLog.i(TAG, "SERVER_QUERY=in_process (actuator / 同机子进程)")
