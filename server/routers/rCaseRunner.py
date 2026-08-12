# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""CaseRunner HTTP API（原飞书回归 AI-led 端点）。

路径前缀：/case-runner

端点：
- POST /case-runner/run                            启动 AI-led 回归
- GET  /case-runner/run/{run_id}                   in-flight run 进度快照
- GET  /case-runner/runs                           列出最近的内存 runs
- GET  /case-runner/traces                         m_case_run_trace 列表
- GET  /case-runner/traces/{run_id}                单条 trace 详情
- GET  /case-runner/baseline/{case_id}             baseline overview + prompt_block
- POST /case-runner/baseline/promote               手工 promote 指定 run 为 baseline
- GET  /case-runner/devices                        当前可用设备（在线 + 通道状态）

数据源仍来自 feishu_service（飞书表格），但执行链路由 server.services.regression.case_runner 驱动。
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from server.core.database import get_db
from server.models.project import App
from server.services.regression import case_runner as cr
from script.log import SLog

router = APIRouter(prefix="/case-runner", tags=["Case Runner"])
TAG = "CaseRunnerRouter"


def _get_app(db: Session, app_id: str) -> App:
    app = db.query(App).options(joinedload(App.project)).filter(App.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    return app


class RunRequest(BaseModel):
    app_id: str
    sn: str
    platform: str = "android"
    case_ids: Optional[List[str]] = None
    start_index: int = 0
    async_exec: bool = True
    use_persisted_baseline: bool = True
    use_cache: bool = True


class PromoteBaselineRequest(BaseModel):
    run_id: str
    blessed_by: str = "manual"
    notes: str = ""


@router.post("/run")
def run_cases(body: RunRequest, db: Session = Depends(get_db)):
    """启动一次 AI-led 回归。"""
    app = _get_app(db, body.app_id)
    if not body.sn:
        raise HTTPException(status_code=400, detail="请选择执行设备")
    try:
        snapshot = cr.run_cases(
            app,
            sn=body.sn,
            platform=(body.platform or "android").lower(),
            case_ids=body.case_ids,
            start_index=body.start_index or 0,
            db=db,
            async_exec=body.async_exec,
            use_persisted_baseline=body.use_persisted_baseline,
            use_cache=body.use_cache,
        )
        return {"code": 200, "msg": "AI-led 回归任务已启动", "data": snapshot}
    except Exception as e:
        SLog.e(TAG, f"/run failed app={body.app_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/run/{run_id}")
def get_run(run_id: str):
    """读取 in-flight run 的内存快照（进度 + per-case 摘要 + 通道状态）。"""
    doc = cr.get_run(run_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return {"code": 200, "data": doc}


@router.get("/runs")
def list_runs(limit: int = 30):
    """列出最近的内存 runs（重启进程后丢失，只为 UI 列表用）。"""
    return {"code": 200, "data": {"runs": cr.list_runs(limit=limit)}}


@router.get("/traces")
def list_traces(
    case_id: Optional[str] = None,
    device_signature: Optional[str] = None,
    only_pass: bool = False,
    limit: int = 20,
):
    """列出持久化在 m_case_run_trace 的 run 摘要。"""
    rows = cr.list_recent_traces(
        case_id=case_id,
        device_signature=device_signature,
        only_pass=only_pass,
        limit=limit,
    )
    return {"code": 200, "data": {"count": len(rows), "items": rows}}


@router.get("/traces/{run_id}")
def get_trace_detail(run_id: str):
    """单条 trace 详情（含 plan_payload / event_results / run_context）。"""
    detail = cr.get_trace_detail(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"trace not found: {run_id}")
    return {"code": 200, "data": detail}


@router.get("/baseline/{case_id}")
def get_baseline(
    case_id: str,
    sn: str = "",
    device_signature: str = "",
    platform: str = "android",
):
    """查询某条 case 的当前 baseline（按 device_signature 维度）。"""
    return {
        "code": 200,
        "data": cr.get_baseline_brief(
            case_id=case_id,
            sn=sn,
            device_signature=device_signature,
            platform=platform,
        ),
    }


@router.post("/baseline/promote")
def promote_baseline(body: PromoteBaselineRequest):
    """手工把指定 trace 提升为 baseline。"""
    result = cr.promote_run(
        run_id=body.run_id,
        blessed_by=body.blessed_by,
        notes=body.notes,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("reason") or "promote failed")
    return {"code": 200, "data": result}


@router.get("/devices")
def list_devices(only_online: bool = True):
    """便利端点：返回 MDevice 列表（含 channels），供前端 sn 选择器使用。"""
    from server.core.database import SessionLocal
    from server.models.mDevice import MDevice
    from server.services.runtime.channels import channels_to_brief

    items = []
    try:
        with SessionLocal() as db:
            q = db.query(MDevice)
            if only_online:
                q = q.filter(MDevice.status == "online")
            for d in q.order_by(MDevice.sn).all():
                items.append({
                    "sn": d.sn,
                    "model": d.model or "",
                    "device_type": d.device_type or "",
                    "os_version": d.os_version or "",
                    "resolution": d.resolution or "",
                    "role": d.role or "",
                    "status": d.status or "offline",
                    "channels": channels_to_brief(d.channels or {}),
                })
    except Exception as e:
        SLog.w(TAG, f"/devices failed: {e}")
    return {"code": 200, "data": {"count": len(items), "items": items}}
