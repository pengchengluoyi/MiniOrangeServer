# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""CaseRunner（原 feishu_runner）：AI-led 回归在 HTTP 层的服务实现。

职责
====
- 把 case dict（目前唯一数据源是飞书表格，由 feishu_service.normalize_feishu_case
  正规化）映射成 CaseSpec
- 启动多 case 回归（同步 / 后台线程两种模式）
- 真设备闸门：build_run_context 后若 adb=remote=false，立刻把 run 标失败
- AI provider 固定从「密钥配置 → 大模型 Key」里读取「可用 + 用例」那条，不在请求里覆盖
- 提供 trace / baseline / promote 三类只读 helper

不替代 feishu_regression_service：本模块是平行、新的 CaseRunner 入口；
路由层有 /feishu/run (legacy) 与 /case-runner/run (本模块) 两条路径。
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from script.log import SLog

from server.services.ai.regression.schemas import CaseSpec, CaseStep
from server.services.feishu_service import normalize_feishu_case
from server.services.regression import case_memory
from server.services.regression.case_memory import repo as memory_repo
from server.services.regression.orchestrator import OrchestratorOptions, run_case
from server.services.regression.router import CapabilityRouter
from server.services.runtime.run_context import RunContext, build_run_context

TAG = "CaseRunner"


# ---------- 进程级 runs 状态 ----------

_RUNS: dict[str, dict[str, Any]] = {}
_LOCK = threading.RLock()


def _snapshot(run_doc: dict[str, Any]) -> dict[str, Any]:
    """copy snapshot，避免外部修改污染内存。"""
    return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
            for k, v in run_doc.items()}


# ---------- case spec 映射 ----------


def to_case_spec(
    raw_case: dict[str, Any],
    *,
    package: str = "",
    env_profile: str = "",
    app_id: str = "",
    app_name: str = "",
) -> CaseSpec:
    """把原始 case dict（normalize 后）转为 CaseSpec。

    把 (env_profile / package / app) 元信息汇入 preconditions，让 PLAN_OVERVIEW
    prompt 看到完整上下文。
    """
    case = normalize_feishu_case(raw_case) if raw_case else {}
    case_id = str(case.get("case_id") or f"row-{case.get('row_index') or '?'}")
    name = str(case.get("name") or case_id)
    precondition_raw = str(case.get("precondition") or "")

    pre_parts: list[str] = []
    meta_line_bits: list[str] = []
    if app_id:
        meta_line_bits.append(f"app_id={app_id}")
    if app_name:
        meta_line_bits.append(f"app_name={app_name}")
    if env_profile:
        meta_line_bits.append(f"env_profile={env_profile}")
    if package:
        meta_line_bits.append(f"package={package}")
    if meta_line_bits:
        pre_parts.append("[meta] " + " | ".join(meta_line_bits))
    if precondition_raw.strip():
        pre_parts.append(precondition_raw.strip())

    steps_text = list(case.get("steps") or [])
    step_nums = list(case.get("step_nums") or list(range(1, len(steps_text) + 1)))
    expected_by_step = dict(case.get("expected_by_step") or {})
    expected_lines = list(case.get("expected") or [])
    expected_raw = str(case.get("expected_raw") or "")

    steps: list[CaseStep] = []
    for idx, text in enumerate(steps_text):
        num = int(step_nums[idx]) if idx < len(step_nums) else idx + 1
        expected = ""
        if num in expected_by_step:
            expected = str(expected_by_step[num]).strip()
        elif idx < len(expected_lines):
            expected = str(expected_lines[idx]).strip()
        steps.append(
            CaseStep(
                index=num,
                instruction=str(text).strip(),
                expected=expected,
                raw={"step_num": num, "expected": expected},
            )
        )

    overall_expected = expected_raw.strip() if expected_raw else "\n".join(
        f"{n}. {e}" for n, e in expected_by_step.items() if e
    )

    tags_field = case.get("tags") or case.get("module") or ""
    if isinstance(tags_field, str):
        tags = [t for t in (s.strip() for s in tags_field.split(",")) if t]
    elif isinstance(tags_field, list):
        tags = [str(t) for t in tags_field if t]
    else:
        tags = []

    return CaseSpec(
        case_id=case_id,
        name=name,
        preconditions="\n".join(pre_parts),
        steps=steps,
        expected=overall_expected,
        tags=tags,
        priority=str(case.get("priority") or ""),
        source=case.get("source") or "feishu",
        raw_row=case,
    )


# ---------- 主入口 ----------


def run_cases(
    app: Any,
    *,
    sn: str,
    platform: str = "android",
    case_ids: Optional[list[str]] = None,
    start_index: int = 0,
    db: Optional[Session] = None,
    async_exec: bool = True,
    use_persisted_baseline: bool = True,
    use_cache: bool = True,
    options: Optional[OrchestratorOptions] = None,
) -> dict[str, Any]:
    """启动一次 AI-led 回归。

    返回 run snapshot dict（含 run_id 与初始状态），无论同步 / 异步。
    异步模式下 _RUNS[run_id] 持续被 worker 线程更新；调 get_run 读取。

    真设备闸门
    ----------
    worker 会先 build_run_context()，若 adb=remote=false 立刻把 run 标 failed，
    不调任何 LLM、不写 trace。
    """
    from server.services import app_automation_service as aas
    from server.services import feishu_regression_service as frs
    from server.services.ai.regression.llm_client import resolve_regression_provider

    if use_cache:
        payload = frs.list_cases_for_app(app, refresh=False)
    else:
        payload = frs.fetch_cases_for_app(app, persist=True)
    all_cases = list(payload.get("cases") or [])
    if case_ids:
        by_id = {c.get("case_id"): c for c in all_cases if c.get("case_id")}
        cases = [by_id[cid] for cid in case_ids if cid in by_id]
        missing = [cid for cid in case_ids if cid not in by_id]
        if missing:
            SLog.w(TAG, f"missing case_ids: {missing[:5]} (total {len(missing)})")
    else:
        cases = list(all_cases)
    if start_index > 0:
        cases = cases[start_index:]

    env_profile = aas.resolve_env_profile(app)
    package = aas.package_for_app(app, env_profile) or ""
    app_id = getattr(app, "id", "") or ""
    app_name = getattr(app, "name", "") or ""

    run_id = f"cr-{uuid.uuid4().hex[:12]}"
    run_doc: dict[str, Any] = {
        "run_id": run_id,
        "engine": "ai_led",
        "app_id": app_id,
        "app_name": app_name,
        "sn": sn,
        "platform": platform,
        "env_profile": env_profile,
        "package": package,
        "provider_id": "",
        "provider_name": "",
        "model_name": "",
        "total": len(cases),
        "completed": 0,
        "passed": 0,
        "failed": 0,
        "blocked": 0,
        "declined": 0,
        "status": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "cases": [],   # per-case 摘要
        "error": "",
        "connectivity": {},
    }
    with _LOCK:
        _RUNS[run_id] = run_doc

    if not cases:
        run_doc["status"] = "done"
        run_doc["finished_at"] = datetime.now().isoformat(timespec="seconds")
        SLog.w(TAG, f"run {run_id} no cases to execute (app={app_id})")
        return _snapshot(run_doc)

    provider, gate = resolve_regression_provider()
    if not provider:
        reason = gate.get("reason") or "未配置用例执行大模型（密钥配置 → 大模型 Key → 可用 + 用例）"
        run_doc["status"] = "failed"
        run_doc["error"] = reason
        run_doc["provider_id"] = gate.get("provider_id") or ""
        run_doc["finished_at"] = datetime.now().isoformat(timespec="seconds")
        SLog.e(TAG, f"run {run_id} aborted: {reason}")
        return _snapshot(run_doc)

    provider_id = (provider.get("id") or "").strip()
    model_name = (provider.get("model") or "").strip()
    run_doc["provider_id"] = provider_id
    run_doc["provider_name"] = (provider.get("name") or provider_id).strip()
    run_doc["model_name"] = model_name

    options = options or OrchestratorOptions()

    def _worker() -> None:
        try:
            _execute(
                run_doc=run_doc,
                cases=cases,
                sn=sn,
                platform=platform,
                env_profile=env_profile,
                package=package,
                app_id=app_id,
                app_name=app_name,
                use_persisted_baseline=use_persisted_baseline,
                provider_id=provider_id,
                model_name=model_name,
                options=options,
            )
        except Exception as exc:  # pragma: no cover
            SLog.e(TAG, f"run {run_id} worker crashed: {exc}")
            with _LOCK:
                run_doc["status"] = "failed"
                run_doc["error"] = str(exc)
                run_doc["finished_at"] = datetime.now().isoformat(timespec="seconds")

    if async_exec:
        threading.Thread(target=_worker, name=f"case-runner-{run_id}", daemon=True).start()
        return _snapshot(run_doc)

    _worker()
    return _snapshot(run_doc)


def _execute(
    *,
    run_doc: dict[str, Any],
    cases: list[dict[str, Any]],
    sn: str,
    platform: str,
    env_profile: str,
    package: str,
    app_id: str,
    app_name: str,
    use_persisted_baseline: bool,
    provider_id: str = "",
    model_name: str = "",
    options: OrchestratorOptions,
) -> None:
    """同步逐 case 跑；中途更新 _RUNS。"""
    run_id = run_doc["run_id"]

    # 1) 真设备探测 + 闸门
    try:
        ctx = build_run_context(
            sn=sn,
            platform=platform,
            run_id=run_id,
            provider_id=provider_id,
            model_name=model_name,
            target_package=package,
        )
    except Exception as exc:
        SLog.e(TAG, f"build_run_context failed sn={sn}: {exc}")
        with _LOCK:
            run_doc["status"] = "failed"
            run_doc["error"] = f"build_run_context: {exc}"
            run_doc["finished_at"] = datetime.now().isoformat(timespec="seconds")
        return

    flags = ctx.connectivity_flags
    with _LOCK:
        run_doc["connectivity"] = {
            "adb": flags.get("adb", False),
            "remote": flags.get("remote", False),
            "vlm": flags.get("vlm", False),
            "hitl": flags.get("hitl", False),
            "device_signature": ctx.device_signature,
            "channels": {
                "adb": ctx.adb,
                "remote": ctx.remote,
                "vlm": ctx.vlm,
                "hitl": ctx.hitl,
            },
        }

    if not flags.get("adb") and not flags.get("remote"):
        msg = (
            f"device offline: sn={sn} 既未连上 adb 也未连上 remote(ClawNode)；"
            f"adb={ctx.adb.get('state')} remote={ctx.remote.get('state')}"
        )
        SLog.e(TAG, f"[{run_id}] {msg}")
        with _LOCK:
            run_doc["status"] = "failed"
            run_doc["error"] = msg
            run_doc["finished_at"] = datetime.now().isoformat(timespec="seconds")
        return

    router = CapabilityRouter(
        ctx,
        capture_prefer=("remote", "adb") if str(sn).startswith("claw-") else ("adb", "remote"),
    )

    # 2) 逐 case 跑
    for raw_case in cases:
        case_started_ts = time.time()
        try:
            spec = to_case_spec(
                raw_case,
                package=package,
                env_profile=env_profile,
                app_id=app_id,
                app_name=app_name,
            )
        except Exception as exc:
            SLog.e(TAG, f"to_case_spec failed: {exc}")
            with _LOCK:
                run_doc["cases"].append({
                    "case_id": raw_case.get("case_id") or "(unknown)",
                    "name": raw_case.get("name") or "",
                    "status": "fail",
                    "report_run_id": "",
                    "summary": f"to_case_spec error: {exc}",
                    "elapsed_ms": int((time.time() - case_started_ts) * 1000),
                })
                run_doc["completed"] += 1
                run_doc["failed"] += 1
            continue

        SLog.i(TAG, f"[{run_id}] >>> running case={spec.case_id} ({spec.name}) sn={sn}")

        raw_pre = str(raw_case.get("precondition") or "").strip()
        app_cache_cleared = False
        if raw_pre:
            from server.services.case_precondition_service import (
                has_precondition_phase,
                precondition_cleared_app_cache,
                run_preconditions,
            )

            if has_precondition_phase(raw_pre, "before_launch"):
                before_res = run_preconditions(
                    raw_pre,
                    sn=sn,
                    platform=platform,
                    package=package,
                    phase="before_launch",
                )
                if not before_res.get("ok"):
                    with _LOCK:
                        run_doc["cases"].append({
                            "case_id": spec.case_id,
                            "name": spec.name,
                            "status": "fail",
                            "report_run_id": "",
                            "summary": before_res.get("msg") or "前置条件不满足",
                            "elapsed_ms": int((time.time() - case_started_ts) * 1000),
                        })
                        run_doc["completed"] += 1
                        run_doc["failed"] += 1
                    SLog.w(
                        TAG,
                        f"[{run_id}] case={spec.case_id} precondition failed: "
                        f"{before_res.get('msg')}",
                    )
                    continue
                app_cache_cleared = precondition_cleared_app_cache(
                    list(before_res.get("items") or [])
                )

        try:
            report = run_case(
                spec,
                run_context=ctx,
                options=options,
                provider_id=provider_id or None,
                router=router,
                run_id=f"{run_id}::{spec.case_id}",
                use_persisted_baseline=use_persisted_baseline,
                app_cache_cleared=app_cache_cleared,
            )
        except Exception as exc:  # pragma: no cover
            SLog.e(TAG, f"run_case crashed case={spec.case_id}: {exc}")
            with _LOCK:
                run_doc["cases"].append({
                    "case_id": spec.case_id,
                    "name": spec.name,
                    "status": "fail",
                    "report_run_id": "",
                    "summary": f"run_case crashed: {exc}",
                    "elapsed_ms": int((time.time() - case_started_ts) * 1000),
                })
                run_doc["completed"] += 1
                run_doc["failed"] += 1
            continue

        with _LOCK:
            entry = {
                "case_id": spec.case_id,
                "name": spec.name,
                "status": str(report.overall_status),
                "report_run_id": report.run_id,
                "summary": (
                    report.blocked_reason or report.decline_reason
                    or f"events={report.total_events} pass={report.passed} fail={report.failed}"
                ),
                "passed": report.passed,
                "failed": report.failed,
                "blocked": report.blocked,
                "skipped": report.skipped,
                "declined": report.declined,
                "replan_count": report.replan_count,
                "elapsed_ms": report.elapsed_ms,
            }
            run_doc["cases"].append(entry)
            run_doc["completed"] += 1
            ostatus = report.overall_status
            if ostatus == "pass":
                run_doc["passed"] += 1
            elif ostatus == "blocked":
                run_doc["blocked"] += 1
            elif ostatus == "declined":
                run_doc["declined"] += 1
            else:
                run_doc["failed"] += 1

        SLog.i(
            TAG,
            f"[{run_id}] <<< case={spec.case_id} status={report.overall_status} "
            f"({report.passed}P/{report.failed}F/{report.blocked}B in {report.elapsed_ms}ms)",
        )

    with _LOCK:
        run_doc["status"] = "done"
        run_doc["finished_at"] = datetime.now().isoformat(timespec="seconds")


# ---------- 读取 / 查询 ----------


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    """从内存 _RUNS 拿 snapshot；未找到返回 None。"""
    with _LOCK:
        doc = _RUNS.get(run_id)
        return _snapshot(doc) if doc else None


def list_runs(limit: int = 30) -> list[dict[str, Any]]:
    with _LOCK:
        items = list(_RUNS.values())
    items.sort(key=lambda d: d.get("started_at") or "", reverse=True)
    return [_snapshot(d) for d in items[:limit]]


def list_recent_traces(
    *,
    case_id: Optional[str] = None,
    device_signature: Optional[str] = None,
    only_pass: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """从 m_case_run_trace 读列表（含 baseline 标记）。"""
    out: list[dict[str, Any]] = []
    try:
        with memory_repo.session_scope() as db:
            rows = memory_repo.list_run_traces(
                db,
                case_id=case_id,
                device_signature=device_signature,
                only_pass=only_pass,
                limit=limit,
            )
            for r in rows:
                out.append({
                    "run_id": r.run_id,
                    "case_id": r.case_id,
                    "device_signature": r.device_signature,
                    "sn": r.sn,
                    "platform": r.platform,
                    "ai_provider_id": r.ai_provider_id,
                    "overall_status": r.overall_status,
                    "total_events": r.total_events,
                    "passed": r.passed,
                    "failed": r.failed,
                    "skipped": r.skipped,
                    "blocked": r.blocked,
                    "declined": r.declined,
                    "replan_count": r.replan_count,
                    "elapsed_ms": r.elapsed_ms,
                    "is_baseline": bool(r.is_baseline),
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                })
    except Exception as exc:
        SLog.w(TAG, f"list_recent_traces failed: {exc}")
    return out


def get_trace_detail(run_id: str) -> Optional[dict[str, Any]]:
    """读 m_case_run_trace 单条详情（含 plan_payload / event_results）。"""
    try:
        with memory_repo.session_scope() as db:
            r = memory_repo.get_run_trace(db, run_id)
            if r is None:
                return None
            return {
                "run_id": r.run_id,
                "case_id": r.case_id,
                "device_signature": r.device_signature,
                "sn": r.sn,
                "platform": r.platform,
                "overall_status": r.overall_status,
                "total_events": r.total_events,
                "passed": r.passed,
                "failed": r.failed,
                "skipped": r.skipped,
                "blocked": r.blocked,
                "declined": r.declined,
                "replan_count": r.replan_count,
                "elapsed_ms": r.elapsed_ms,
                "is_baseline": bool(r.is_baseline),
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "plan_payload": r.plan_payload or {},
                "report_payload": r.report_payload or {},
                "final_plan_events": r.final_plan_events or [],
                "event_results": r.event_results or [],
                "run_context": r.run_context or {},
            }
    except Exception as exc:
        SLog.w(TAG, f"get_trace_detail failed: {exc}")
        return None


def get_baseline_brief(
    *,
    case_id: str,
    sn: str = "",
    device_signature: str = "",
    platform: str = "android",
) -> dict[str, Any]:
    """便利包装：返回 baseline overview + prompt_block 预览。

    优先用 device_signature；若空则尝试从 sn 反推（只读 MDevice）。
    """
    sig = (device_signature or "").strip()
    if not sig and sn:
        try:
            from server.core.database import SessionLocal
            from server.models.mDevice import MDevice

            with SessionLocal() as db:
                dev = db.query(MDevice).filter(MDevice.sn == sn).first()
                if dev:
                    parts = [x for x in (dev.model, dev.os_version, dev.resolution) if x]
                    sig = " / ".join(parts) if parts else (sn or "")
        except Exception as exc:  # pragma: no cover
            SLog.w(TAG, f"resolve device_signature for sn={sn} failed: {exc}")

    overview = case_memory.load_baseline_for_planning(case_id=case_id, device_signature=sig)
    if overview is None:
        return {
            "case_id": case_id,
            "device_signature": sig,
            "exists": False,
            "overview": None,
        }
    return {
        "case_id": case_id,
        "device_signature": sig,
        "exists": True,
        "overview": {
            "event_count": overview.event_count,
            "overall_status": overview.overall_status,
            "blessed_at": overview.blessed_at,
            "last_ai_reasoning": overview.last_ai_reasoning,
            "events_brief_text": overview.events_brief_text,
            "prompt_block": overview.to_prompt_block(),
        },
    }


def promote_run(
    *,
    run_id: str,
    blessed_by: str = "manual",
    notes: str = "",
) -> dict[str, Any]:
    """手工把某条 m_case_run_trace 提升为 baseline。"""
    return case_memory.promote_run_to_baseline(
        run_id=run_id,
        blessed_by=blessed_by,
        notes=notes,
    )
