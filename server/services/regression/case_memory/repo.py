# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Case memory 的存储层：SQLAlchemy 操作 m_case_baseline + m_case_run_trace。

设计原则
========
- 本层只做"读写表"，不掺业务规则。是否 bless / 怎么算 brief 全在 service.py。
- session 由调用方负责创建并 commit；这里只接受 Session 形参，便于测试用 in-memory db。
- 保留少量"便利方法"使用 SessionLocal 自动开关，给一站式 API 调。
"""
from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any, Iterator, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from script.log import SLog
from server.core.database import SessionLocal
from server.models.case_baseline import MCaseBaseline, MCaseRunTrace

TAG = "CaseMemoryRepo"


# ---------- session helper ----------


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    """便利上下文：自动 commit / rollback / close。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ---------- run trace ----------


def split_batch_id(run_id: str) -> str:
    """从 trace run_id（"{batch_id}::{case_id}"）反解任务 id；无 '::' 则返回空串。"""
    rid = str(run_id or "")
    return rid.split("::", 1)[0] if "::" in rid else ""


def insert_run_trace(
    db: Session,
    *,
    run_id: str,
    case_id: str,
    device_signature: str,
    sn: str,
    platform: str,
    ai_provider_id: str,
    overall_status: str,
    total_events: int,
    passed: int,
    failed: int,
    skipped: int,
    blocked: int,
    declined: int,
    replan_count: int,
    elapsed_ms: int,
    plan_payload: dict[str, Any],
    report_payload: dict[str, Any],
    final_plan_events: list[dict[str, Any]],
    event_results: list[dict[str, Any]],
    run_context: dict[str, Any],
    app_id: str = "",
    batch_id: str = "",
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
) -> MCaseRunTrace:
    """插入一条 run trace；如果 run_id 已存在则覆盖。

    app_id / batch_id 缺省时从 run_context 与 run_id 兜底（BE-P0-3），这样
    orchestrator 不传也能归属到应用与任务。
    """
    ctx = run_context or {}
    app_id = app_id or str(ctx.get("app_id") or "")
    batch_id = batch_id or str(ctx.get("batch_id") or "") or split_batch_id(run_id)

    existing = db.get(MCaseRunTrace, run_id)
    if existing is not None:
        # 开跑时先 upsert 过一条 running 占位行（mark_run_trace_running），结束覆盖属正常流程
        started = started_at or existing.started_at
        if str(existing.overall_status or "") in ("pending", "running"):
            SLog.d(TAG, f"run trace {run_id} placeholder → finalize")
        else:
            SLog.w(TAG, f"run trace {run_id} already exists → overwrite")
        db.delete(existing)
        db.flush()
        started_at = started

    row = MCaseRunTrace(
        run_id=run_id,
        case_id=case_id,
        app_id=app_id,
        batch_id=batch_id,
        device_signature=device_signature or "",
        sn=sn or "",
        platform=platform or "android",
        ai_provider_id=ai_provider_id or "",
        overall_status=overall_status or "unknown",
        total_events=int(total_events or 0),
        passed=int(passed or 0),
        failed=int(failed or 0),
        skipped=int(skipped or 0),
        blocked=int(blocked or 0),
        declined=int(declined or 0),
        replan_count=int(replan_count or 0),
        elapsed_ms=int(elapsed_ms or 0),
        plan_payload=plan_payload or {},
        report_payload=report_payload or {},
        final_plan_events=final_plan_events or [],
        event_results=event_results or [],
        run_context=run_context or {},
        is_baseline=False,
        started_at=started_at or datetime.now(),
        finished_at=finished_at,
    )
    db.add(row)
    db.flush()
    return row


def mark_run_trace_running(
    db: Session,
    *,
    run_id: str,
    case_id: str,
    app_id: str = "",
    batch_id: str = "",
    sn: str = "",
    platform: str = "android",
    device_signature: str = "",
    ai_provider_id: str = "",
    status: str = "running",
) -> Optional[MCaseRunTrace]:
    """用例开跑就落一条占位 trace（BE-P0-3）。

    这样「任务下有哪些用例、当前跑到哪条」在 trace 表里也查得到，不必等用例结束；
    结束时 insert_run_trace 会覆盖这一行补齐完整 payload。已存在终态行则不动。
    """
    if not run_id:
        return None
    batch_id = batch_id or split_batch_id(run_id)
    row = db.get(MCaseRunTrace, run_id)
    if row is not None:
        if str(row.overall_status or "") not in ("pending", "running"):
            return row  # 已是终态（例如重跑同 run_id），不要退回 running
        row.overall_status = status
    else:
        row = MCaseRunTrace(
            run_id=run_id,
            case_id=case_id or "",
            overall_status=status,
            started_at=datetime.now(),
        )
        db.add(row)
    row.app_id = app_id or row.app_id or ""
    row.batch_id = batch_id or row.batch_id or ""
    row.sn = sn or row.sn or ""
    row.platform = platform or row.platform or "android"
    row.device_signature = device_signature or row.device_signature or ""
    row.ai_provider_id = ai_provider_id or row.ai_provider_id or ""
    db.flush()
    return row


def get_run_trace(db: Session, run_id: str) -> Optional[MCaseRunTrace]:
    return db.get(MCaseRunTrace, run_id)


def list_run_traces(
    db: Session,
    *,
    case_id: Optional[str] = None,
    device_signature: Optional[str] = None,
    app_id: Optional[str] = None,
    batch_id: Optional[str] = None,
    limit: int = 20,
    only_pass: bool = False,
) -> list[MCaseRunTrace]:
    q = db.query(MCaseRunTrace)
    if case_id:
        q = q.filter(MCaseRunTrace.case_id == case_id)
    if device_signature:
        q = q.filter(MCaseRunTrace.device_signature == device_signature)
    if app_id:
        q = q.filter(MCaseRunTrace.app_id == app_id)
    if batch_id:
        # 老数据 batch_id 由迁移回填；再用 run_id 前缀兜底，避免漏掉未回填的行
        q = q.filter(
            or_(
                MCaseRunTrace.batch_id == batch_id,
                MCaseRunTrace.run_id.like(f"{batch_id}::%"),
            )
        )
    if only_pass:
        q = q.filter(MCaseRunTrace.overall_status == "pass")
    return q.order_by(MCaseRunTrace.started_at.desc()).limit(limit).all()


# ---------- baseline ----------


def get_baseline(
    db: Session,
    *,
    case_id: str,
    device_signature: str,
) -> Optional[MCaseBaseline]:
    return (
        db.query(MCaseBaseline)
        .filter(
            MCaseBaseline.case_id == case_id,
            MCaseBaseline.device_signature == (device_signature or ""),
        )
        .first()
    )


def upsert_baseline(
    db: Session,
    *,
    case_id: str,
    device_signature: str,
    baseline_run_id: str,
    overall_status: str,
    events_brief: list[dict[str, Any]],
    ai_reasoning_overview: str = "",
    blessed_by: str = "auto",
    notes: str = "",
) -> MCaseBaseline:
    """写入或覆盖 baseline。同时把对应 run_trace.is_baseline=True，其它 run_trace.is_baseline=False。"""
    row = get_baseline(db, case_id=case_id, device_signature=device_signature)
    now = datetime.now()
    if row is None:
        row = MCaseBaseline(
            case_id=case_id,
            device_signature=device_signature or "",
            baseline_run_id=baseline_run_id,
            overall_status=overall_status or "pass",
            events_brief=events_brief or [],
            ai_reasoning_overview=(ai_reasoning_overview or "")[:2000],
            blessed_at=now,
            blessed_by=blessed_by or "auto",
            notes=notes or "",
        )
        db.add(row)
    else:
        row.baseline_run_id = baseline_run_id
        row.overall_status = overall_status or "pass"
        row.events_brief = events_brief or []
        row.ai_reasoning_overview = (ai_reasoning_overview or "")[:2000]
        row.blessed_at = now
        row.blessed_by = blessed_by or "auto"
        row.notes = notes or ""

    # 维护 is_baseline 标记
    db.query(MCaseRunTrace).filter(
        MCaseRunTrace.case_id == case_id,
        MCaseRunTrace.device_signature == (device_signature or ""),
        MCaseRunTrace.is_baseline.is_(True),
    ).update({MCaseRunTrace.is_baseline: False}, synchronize_session=False)
    trace = db.get(MCaseRunTrace, baseline_run_id)
    if trace is not None:
        trace.is_baseline = True
    db.flush()
    return row


def clear_baseline(
    db: Session,
    *,
    case_id: str,
    device_signature: str,
) -> bool:
    row = get_baseline(db, case_id=case_id, device_signature=device_signature)
    if row is None:
        return False
    db.delete(row)
    db.query(MCaseRunTrace).filter(
        MCaseRunTrace.case_id == case_id,
        MCaseRunTrace.device_signature == (device_signature or ""),
        MCaseRunTrace.is_baseline.is_(True),
    ).update({MCaseRunTrace.is_baseline: False}, synchronize_session=False)
    return True
