# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Case Memory 高级 API：上层（Orchestrator / planner）只调这里。

职责
====
- record_run_finished(report, plan, run_context, case_id, ...): 把一次 run 写入 trace；
    如果 overall_status == "pass" 且开启 auto bless → 同时 promote 成 baseline。
- promote_run_to_baseline(...): 手工把某次 run 提升为 baseline。
- load_baseline_for_planning(case_id, device_signature): 返回 BaselineOverview（或 None）。
- build_replan_window(case_id, device_signature, current_events, current_index): 三段窗口。

只在这里碰 ORM + Pydantic 转换；下层 repo / align / windows 各管各的。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from script.log import SLog

from server.services.ai.regression.schemas import (
    BaselineContext,
    BaselineSnippet,
    PlanEvent,
    PlanResult,
    RunReport,
)
from server.services.regression.case_memory import repo
from server.services.regression.case_memory.align import compute_baseline_alignment
from server.services.regression.case_memory.windows import (
    BaselineOverview,
    baseline_snippets_from_brief,
    build_baseline_overview,
    build_baseline_window,
)
from server.services.runtime.run_context import RunContext

TAG = "CaseMemoryService"


# ---------- 内部工具 ----------


def _events_to_brief(
    final_plan_events: list[PlanEvent] | list[dict[str, Any]],
    event_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把 final_plan_events 与 event_results 合并成 events_brief（baseline 用的精简形）。"""
    # event_results 按 seq 索引（同 seq 取最后一次，因为 replan 后会重复出现）
    by_seq: dict[int, dict[str, Any]] = {}
    for r in event_results or []:
        seq = int(r.get("seq") or 0)
        if seq:
            by_seq[seq] = r

    brief: list[dict[str, Any]] = []
    for ev in final_plan_events or []:
        if isinstance(ev, PlanEvent):
            ev_d = ev.model_dump(exclude_none=True)
        else:
            ev_d = dict(ev)
        seq = int(ev_d.get("seq") or 0)
        result = by_seq.get(seq, {})
        brief.append({
            "seq": seq,
            "case_step_index": ev_d.get("case_step_index"),
            "capability_id": ev_d.get("capability_id", ""),
            "event_kind": ev_d.get("event_kind") or result.get("event_kind") or "",
            "needs_vlm": bool(ev_d.get("needs_vlm")),
            "executor_used": result.get("executor_used", ""),
            "status": str(result.get("status", "")) or "unknown",
            "summary": result.get("summary", ""),
            "params": ev_d.get("params") or {},
            "ai_reasoning": ev_d.get("ai_reasoning", ""),
            "elapsed_ms": int(result.get("elapsed_ms") or 0),
        })
    return brief


def _status_str(s: Any) -> str:
    """EventStatus / str → 字符串名。"""
    if hasattr(s, "value"):
        return str(s.value)
    return str(s or "")


def _datetime_or_now(iso_or_dt: Any) -> Optional[datetime]:
    if iso_or_dt is None or iso_or_dt == "":
        return None
    if isinstance(iso_or_dt, datetime):
        return iso_or_dt
    try:
        return datetime.fromisoformat(str(iso_or_dt))
    except Exception:
        return None


# ---------- 公开 API ----------


def record_run_finished(
    *,
    report: RunReport,
    plan: PlanResult,
    run_context: RunContext,
    case_id: str,
    auto_bless_on_pass: bool = True,
    blessed_by: str = "auto",
) -> dict[str, Any]:
    """跑完一条 case 后调用：写 trace + 视情况 promote 成 baseline。

    返回 {trace_run_id, baseline_run_id, promoted}。
    任何 DB 异常被吞掉并 log，避免影响调用方主流程。
    """
    summary = {
        "trace_run_id": report.run_id,
        "baseline_run_id": "",
        "promoted": False,
        "error": "",
    }
    try:
        report_d = report.model_dump(mode="json")
        plan_d = plan.model_dump(mode="json")
        # 序列化 EventResult / PlanEvent 时把 EventStatus 转为 str
        event_results_raw = report_d.get("events", []) or []
        for r in event_results_raw:
            r["status"] = _status_str(r.get("status"))
        final_plan_events_raw = report_d.get("final_plan_events", []) or []

        device_sig = run_context.device_signature if run_context else ""
        sn = run_context.sn if run_context else ""
        platform = run_context.platform if run_context else "android"
        provider_id = run_context.provider_id if run_context else ""
        ctx_snapshot = run_context.to_dict() if run_context else {}

        with repo.session_scope() as db:
            repo.insert_run_trace(
                db,
                run_id=report.run_id,
                case_id=case_id or report.case_id or "",
                device_signature=device_sig,
                sn=sn,
                platform=platform,
                ai_provider_id=provider_id,
                overall_status=str(report.overall_status),
                total_events=report.total_events,
                passed=report.passed,
                failed=report.failed,
                skipped=report.skipped,
                blocked=report.blocked,
                declined=report.declined,
                replan_count=report.replan_count,
                elapsed_ms=report.elapsed_ms,
                plan_payload=plan_d,
                report_payload={k: v for k, v in report_d.items() if k not in ("events", "final_plan_events")},
                final_plan_events=final_plan_events_raw,
                event_results=event_results_raw,
                run_context=ctx_snapshot,
                started_at=_datetime_or_now(report.started_at),
                finished_at=_datetime_or_now(report.finished_at),
            )

            if auto_bless_on_pass and report.overall_status == "pass":
                events_brief = _events_to_brief(final_plan_events_raw, event_results_raw)
                repo.upsert_baseline(
                    db,
                    case_id=case_id or report.case_id or "",
                    device_signature=device_sig,
                    baseline_run_id=report.run_id,
                    overall_status=str(report.overall_status),
                    events_brief=events_brief,
                    ai_reasoning_overview=str(plan.ai_reasoning or "")[:2000],
                    blessed_by=blessed_by,
                )
                summary["baseline_run_id"] = report.run_id
                summary["promoted"] = True
        SLog.i(
            TAG,
            f"recorded run={report.run_id} case={case_id} status={report.overall_status} "
            f"promoted={summary['promoted']}",
        )
    except Exception as exc:  # pragma: no cover
        SLog.e(TAG, f"record_run_finished failed: {exc}")
        summary["error"] = str(exc)
    return summary


def promote_run_to_baseline(
    *,
    run_id: str,
    blessed_by: str = "manual",
    notes: str = "",
) -> dict[str, Any]:
    """手工把某次 run trace 提升为 baseline。"""
    out = {"ok": False, "reason": ""}
    try:
        with repo.session_scope() as db:
            trace = repo.get_run_trace(db, run_id)
            if trace is None:
                out["reason"] = f"no run trace {run_id}"
                return out
            final_plan_events = trace.final_plan_events or []
            event_results = trace.event_results or []
            events_brief = _events_to_brief(final_plan_events, event_results)
            plan_payload = trace.plan_payload or {}
            ai_reasoning = str(plan_payload.get("ai_reasoning") or "")
            repo.upsert_baseline(
                db,
                case_id=trace.case_id,
                device_signature=trace.device_signature or "",
                baseline_run_id=run_id,
                overall_status=trace.overall_status or "",
                events_brief=events_brief,
                ai_reasoning_overview=ai_reasoning,
                blessed_by=blessed_by,
                notes=notes,
            )
        out["ok"] = True
    except Exception as exc:  # pragma: no cover
        SLog.e(TAG, f"promote_run_to_baseline failed: {exc}")
        out["reason"] = str(exc)
    return out


def load_baseline_for_planning(
    *,
    case_id: str,
    device_signature: str,
) -> Optional[BaselineOverview]:
    """供 PLAN_OVERVIEW 注入的总览。无 baseline 返回 None。"""
    try:
        with repo.session_scope() as db:
            row = repo.get_baseline(db, case_id=case_id, device_signature=device_signature or "")
            if row is None:
                return None
            snippets = baseline_snippets_from_brief(row.events_brief or [])
            return build_baseline_overview(
                case_id=case_id,
                device_signature=device_signature or "",
                overall_status=row.overall_status or "",
                snippets=snippets,
                last_ai_reasoning=row.ai_reasoning_overview or "",
                blessed_at=row.blessed_at.isoformat() if row.blessed_at else "",
            )
    except Exception as exc:  # pragma: no cover
        SLog.w(TAG, f"load_baseline_for_planning failed: {exc}")
        return None


def load_baseline_overview_brief(
    *,
    case_id: str,
    device_signature: str,
) -> str:
    """便利接口：直接拿到拼好的 prompt 文本块（无 baseline 返回 ""）。"""
    ov = load_baseline_for_planning(case_id=case_id, device_signature=device_signature)
    return ov.to_prompt_block() if ov else ""


def build_replan_window(
    *,
    case_id: str,
    device_signature: str,
    current_events: list[PlanEvent] | list[dict[str, Any]],
    current_index: int,
    notes: str = "",
) -> BaselineContext:
    """供 SINGLE_STEP_REPLAN 注入：基于 baseline 算 prev/curr/next 三段。

    无 baseline / 加载失败 → 返回空 BaselineContext（仍可安全传给 planner）。
    """
    try:
        with repo.session_scope() as db:
            row = repo.get_baseline(db, case_id=case_id, device_signature=device_signature or "")
            if row is None:
                return BaselineContext()
            snippets = baseline_snippets_from_brief(row.events_brief or [])
        alignment = compute_baseline_alignment(snippets, list(current_events or []))
        return build_baseline_window(
            baseline_snippets=snippets,
            alignment=alignment,
            current_index=current_index,
            overall_status=row.overall_status or "",
            notes=notes,
        )
    except Exception as exc:  # pragma: no cover
        SLog.w(TAG, f"build_replan_window failed: {exc}")
        return BaselineContext()
