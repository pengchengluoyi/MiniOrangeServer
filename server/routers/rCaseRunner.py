# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""CaseRunner HTTP API（原飞书回归 AI-led 端点）。

路径前缀：/case-runner

端点：
- POST /case-runner/run                            启动 AI-led 回归（落库 + 设备占用 409）
- GET  /case-runner/tasks                          任务列表（DB 权威 + 内存热覆盖）
- GET  /case-runner/tasks/summary                  多 app 聚合（运行中条数 / 最近一条）
- GET  /case-runner/tasks/{task_id}                任务详情（含全量 cases）
- POST /case-runner/tasks/{task_id}/cancel         取消运行中任务（case 边界）
- POST /case-runner/tasks/{task_id}/retry-failed   重跑失败用例（新任务）
- GET  /case-runner/run/{run_id}                   in-flight run 进度快照（过渡保留）
- GET  /case-runner/runs                           列出最近的内存 runs（过渡保留）
- GET  /case-runner/traces                         m_case_run_trace 列表（可按 app_id / batch_id）
- GET  /case-runner/traces/{run_id}                单条 trace 详情
- GET  /case-runner/baseline/{case_id}             baseline overview + prompt_block
- POST /case-runner/baseline/promote               手工 promote 指定 run 为 baseline
- GET  /case-runner/devices                        当前可用设备（在线 + 通道状态 + busy_task_id）

数据源来自应用 QA 流程草稿（qa_process.draft_cases）。执行一律 Agent（看图闭环）。

响应约定
========
本仓库历史响应是 `{"code": 200, "data": ...}`，而测试平台契约
（docs/prd_testing_platform.md §12.1）写的是 `{"ok": true, "data": ...}`。
新增的 /tasks* 端点两个字段都给，前端按哪一个判断都成立，老端点不动。

任务 JSON / WS `testing_task` 事件的字段形状见 §0 与 §12.1，实现在
server/services/regression/task_store.py。设备被占用时 POST /run 返回：

    409 {"detail": {"message": "device busy", "busy_task_id": "cr-xxx", "sn": "..."}}
"""
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from server.core.database import get_db
from server.models.project import App
from server.services.regression import case_runner as cr
from server.services.regression import task_store
from server.services.runtime.qa_process_lock import blocking_reservation, reservations_by_sn
from script.log import SLog

router = APIRouter(prefix="/case-runner", tags=["Case Runner"])
TAG = "CaseRunnerRouter"


def _ok(data: Any, msg: str = "") -> dict[str, Any]:
    """新端点统一响应：同时满足 code / ok 两套约定。"""
    out: dict[str, Any] = {"code": 200, "ok": True, "data": data}
    if msg:
        out["msg"] = msg
    return out


def _get_app(db: Session, app_id: str) -> App:
    app = db.query(App).options(joinedload(App.project)).filter(App.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    return app


class RunRequest(BaseModel):
    app_id: str
    sn: str = ""
    sns: Optional[List[str]] = None
    coverage: str = ""
    platform: str = "android"
    case_ids: Optional[List[str]] = None
    start_index: int = 0
    async_exec: bool = True
    use_persisted_baseline: bool = True
    use_cache: bool = True
    # 已废弃：用例执行只走 Agent。兼容旧客户端，忽略此字段。
    execution_mode: str = "agent"
    instruction: str = ""
    # 触发源：manual | feishu | schedule | copilot（对话下发与回归同一引擎）
    run_type: str = "manual"
    slot_id: str = ""
    requirement_id: str = ""
    release_id: str = ""
    provider_id: str = ""


class PromoteBaselineRequest(BaseModel):
    run_id: str
    blessed_by: str = "manual"
    notes: str = ""


class RetryFailedRequest(BaseModel):
    # 默认沿用原任务的设备；需要换机时显式指定
    sn: str = ""
    # 已废弃：忽略。
    execution_mode: str = "agent"


def _normalize_sns(sn: str = "", sns: Optional[List[str]] = None) -> list[str]:
    raw: list[str] = []
    if sn:
        raw.append(str(sn).strip())
    for item in sns or []:
        raw.append(str(item or "").strip())
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


@router.post("/run")
def run_cases(body: RunRequest, db: Session = Depends(get_db)):
    """启动一次 AI-led 回归。

    同一台设备同时只跑一个任务：若名单里任一 sn 已有 running 任务，直接 409 并带上占用的
    task_id。若当前时刻落在其他应用/窗口的排期占用内，返回 409 device reserved；本窗口主人
    （slot_id 或同 requirement/release + run_type）可以下发。
    """
    app = _get_app(db, body.app_id)
    device_sns = _normalize_sns(body.sn, body.sns)
    if not device_sns:
        raise HTTPException(status_code=400, detail="请选择执行设备")

    cov = str(body.coverage or "").strip().lower()
    if cov not in ("", "once", "per_device"):
        raise HTTPException(status_code=400, detail="coverage 必须是 once 或 per_device")
    if not cov or len(device_sns) == 1:
        cov = "once"

    from server.models.mDevice import MDevice
    from server.services.runtime.run_context import device_platform_kind

    rows = db.query(MDevice).filter(MDevice.sn.in_(device_sns)).all()
    by_sn = {str(r.sn): r for r in rows}
    platforms_by_sn: dict[str, str] = {}
    for sn in device_sns:
        row = by_sn.get(sn)
        platforms_by_sn[sn] = device_platform_kind(
            getattr(row, "device_type", "") if row else "",
            getattr(row, "channels", None) if row else None,
            sn=sn,
        )
    platform = (
        platforms_by_sn[device_sns[0]]
        if len(set(platforms_by_sn.values())) == 1
        else "mixed"
    )

    for sn in device_sns:
        busy_task_id = task_store.busy_task_for_sn(sn)
        if busy_task_id:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "device busy",
                    "busy_task_id": busy_task_id,
                    "sn": sn,
                },
            )

    reserved = blocking_reservation(
        db,
        device_sns,
        slot_id=body.slot_id or "",
        requirement_id=body.requirement_id or "",
        release_id=body.release_id or "",
        run_type=body.run_type or "",
    )
    if reserved:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "device reserved",
                "reason": "schedule",
                "sn": reserved.get("sn") or "",
                "slot_id": reserved.get("slot_id") or "",
                "reserved_title": reserved.get("title") or reserved.get("app_name") or "",
                "reserved_until": reserved.get("reserved_until") or "",
                "app_id": reserved.get("app_id") or "",
            },
        )

    try:
        snapshot = cr.run_cases(
            app,
            sn=device_sns[0],
            sns=device_sns,
            coverage=cov,
            platform=platform,
            platforms_by_sn=platforms_by_sn,
            case_ids=body.case_ids,
            start_index=body.start_index or 0,
            db=db,
            async_exec=body.async_exec,
            use_persisted_baseline=body.use_persisted_baseline,
            use_cache=body.use_cache,
            run_type=(body.run_type or "manual").lower(),
            requirement_id=body.requirement_id or "",
            release_id=body.release_id or "",
            slot_id=body.slot_id or "",
            instruction=str(body.instruction or "").strip(),
            provider_id=str(body.provider_id or "").strip(),
        )
        return {"code": 200, "ok": True, "msg": "AI-led 回归任务已启动", "data": snapshot}
    except Exception as e:
        SLog.e(TAG, f"/run failed app={body.app_id}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# ---------- 任务中心（BE-P0-2 / BE-P1-1/2/3） ----------


@router.get("/tasks")
def list_tasks(
    app_id: str = Query("", description="必填：任务永远属于某个 App"),
    status: str = Query("", description="queued|running|done|failed|cancelled"),
    limit: int = 30,
    offset: int = 0,
):
    """任务列表（列表项省略 cases，只带计数）。任务列表的唯一数据源。"""
    if not app_id:
        raise HTTPException(status_code=400, detail="app_id is required")
    data = task_store.list_tasks(
        app_id, status=(status or "").strip(), limit=max(0, limit), offset=max(0, offset),
    )
    return _ok(data)


@router.get("/tasks/summary")
def tasks_summary(app_ids: str = Query("", description="逗号分隔的 app_id")):
    """多个 app 的运行中条数 / 最近一条任务（应用卡片角标用）。"""
    ids = [a.strip() for a in (app_ids or "").split(",") if a.strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="app_ids is required")
    return _ok({"items": task_store.summary_for_apps(ids)})


@router.get("/tasks/{task_id}")
def get_task(task_id: str):
    """任务详情：含全量 cases（len(cases) == total）。running 时内存覆盖 DB。"""
    task = task_store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"task not found: {task_id}")
    return _ok(task)


@router.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: str):
    """取消运行中任务：worker 在下一条 case 开始前退出，剩余 pending → cancelled。"""
    result = cr.request_cancel(task_id)
    if not result.get("ok"):
        code = int(result.get("code") or 400)
        raise HTTPException(
            status_code=code if code in (400, 404) else 400,
            detail=result.get("reason") or "cancel failed",
        )
    if result.get("already"):
        return _ok(result, msg="任务已结束")
    if result.get("offline"):
        return _ok(result, msg="已取消")
    return _ok(result, msg="已请求取消，当前步骤结束后停止")


@router.post("/tasks/{task_id}/retry-failed")
def retry_failed_cases(task_id: str, body: Optional[RetryFailedRequest] = None,
                       db: Session = Depends(get_db)):
    """重跑该任务里 fail / blocked / declined 的用例：创建一条新任务并返回新 task_id。"""
    body = body or RetryFailedRequest()
    sn = (body.sn or "").strip()
    if sn:
        busy_task_id = task_store.busy_task_for_sn(sn)
        if busy_task_id:
            raise HTTPException(
                status_code=409,
                detail={"message": "device busy", "busy_task_id": busy_task_id, "sn": sn},
            )
    try:
        result = cr.retry_failed(
            task_id, db=db, sn=sn,
        )
    except Exception as e:
        SLog.e(TAG, f"/tasks/{task_id}/retry-failed failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    if not result.get("ok"):
        code = int(result.get("code") or 400)
        if code == 409:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "device busy",
                    "busy_task_id": result.get("busy_task_id") or "",
                    "sn": result.get("sn") or sn,
                },
            )
        raise HTTPException(status_code=code, detail=result.get("reason") or "retry failed")
    snapshot = result.get("data") or {}
    return _ok(
        {
            "task_id": snapshot.get("run_id", ""),
            "retried_from": task_id,
            "case_ids": result.get("case_ids") or [],
            "task": snapshot,
        },
        msg=f"已重跑 {len(result.get('case_ids') or [])} 条失败用例",
    )


@router.get("/agent/runs")
def agent_runs():
    """Agent 流式执行：最近若干 run 的摘要（供 AgentRun 页面列表/历史回填）。"""
    from server.services.regression import agent_stream
    return {"code": 200, "data": {"runs": agent_stream.list_recent_runs()}}


@router.get("/agent/steps/{run_id:path}")
def agent_steps(run_id: str):
    """Agent 流式执行：某次 run 的全部步骤事件（含缩略图/思考/动作/结果），随时可看。"""
    from server.services.regression import agent_stream
    data = agent_stream.get_run_events(run_id)
    if data is None:
        raise HTTPException(status_code=404, detail=f"agent run not found: {run_id}")
    return {"code": 200, "data": data}


@router.get("/run/{run_id}")
def get_run(run_id: str):
    """读取 in-flight run 的内存快照（进度 + per-case 摘要 + 通道状态）。"""
    doc = cr.get_run(run_id)
    if doc is None:
        raise HTTPException(status_code=404, detail=f"run not found: {run_id}")
    return {"code": 200, "data": doc}


@router.get("/runs")
def list_runs(limit: int = 30, app_id: str = ""):
    """列出最近的内存 runs（重启进程后丢失）。

    过渡期端点：任务列表请用 GET /tasks（DB 权威、重启不丢）。
    """
    return {"code": 200, "ok": True,
            "data": {"runs": cr.list_runs(limit=limit, app_id=(app_id or "").strip())}}


@router.get("/traces")
def list_traces(
    case_id: Optional[str] = None,
    device_signature: Optional[str] = None,
    app_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    only_pass: bool = False,
    limit: int = 20,
):
    """列出持久化在 m_case_run_trace 的 run 摘要。

    app_id / batch_id 是 BE-P0-3 新加的归属过滤：不用再靠「当前 App 的 case_id 集合」
    去猜哪些用例属于这个应用 / 这次任务。
    """
    rows = cr.list_recent_traces(
        case_id=case_id,
        device_signature=device_signature,
        app_id=app_id,
        batch_id=batch_id,
        only_pass=only_pass,
        limit=limit,
    )
    return {"code": 200, "ok": True, "data": {"count": len(rows), "items": rows}}


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
def list_devices(only_online: bool = True, db: Session = Depends(get_db)):
    """便利端点：返回 MDevice 列表（含 channels），供前端 sn 选择器使用。

    busy_task_id 非空表示该设备正被这条任务占用；reserved_slot_id 表示当前时刻
    已被某条测试排期锁住（跨应用）。下发前应禁用/提示。
    """
    from server.models.mDevice import MDevice
    from server.services.runtime.channels import channels_to_brief

    busy = task_store.busy_map()
    reserved = reservations_by_sn(db)
    items = []
    try:
        q = db.query(MDevice)
        if only_online:
            q = q.filter(MDevice.status == "online")
        for d in q.order_by(MDevice.sn).all():
            hit = reserved.get(d.sn) or {}
            items.append({
                "sn": d.sn,
                "model": d.model or "",
                "device_type": d.device_type or "",
                "type": d.device_type or "",
                "os_version": d.os_version or "",
                "resolution": d.resolution or "",
                "role": d.role or "",
                "status": d.status or "offline",
                "channels": channels_to_brief(d.channels or {}),
                "busy_task_id": busy.get(d.sn, ""),
                "reserved_slot_id": hit.get("slot_id") or "",
                "reserved_title": hit.get("title") or "",
                "reserved_until": hit.get("reserved_until") or "",
                "reserved_kind": hit.get("kind") or "",
                "reserved_app_id": hit.get("app_id") or "",
            })
    except Exception as e:
        SLog.w(TAG, f"/devices failed: {e}")
    return {"code": 200, "ok": True, "data": {"count": len(items), "items": items}}
