# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""CaseRunner（原 feishu_runner）：AI-led 回归在 HTTP 层的服务实现。

职责
====
- 把 case dict（数据源是应用 qa_process.draft_cases）映射成 CaseSpec
- 启动多 case 回归（同步 / 后台线程两种模式）
- 真设备闸门：build_run_context 后若 adb=remote=false，立刻把 run 标失败
- AI provider 固定从「密钥配置 → 大模型 Key」里读取「可用 + 用例」那条，不在请求里覆盖
- 提供 trace / baseline / promote 三类只读 helper

不替代 feishu_regression_service：本模块是平行、新的 CaseRunner 入口；
路由层有 /feishu/run (legacy) 与 /case-runner/run (本模块) 两条路径。
"""
from __future__ import annotations

import json
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
from server.services.runtime.run_context import RunContext, build_run_context, device_platform_kind

TAG = "CaseRunner"


# ---------- 进程级 runs 状态 ----------

_RUNS: dict[str, dict[str, Any]] = {}
_LOCK = threading.RLock()

# 已 persist_run_start 过的 run_id（避免重复 INSERT）
_PERSISTED: set[str] = set()
# 用例终态（触发 case_finished 事件）
_CASE_TERMINAL = {"pass", "fail", "failed", "blocked", "declined", "skipped", "cancelled"}


def _snapshot(run_doc: dict[str, Any]) -> dict[str, Any]:
    """copy snapshot，避免外部修改污染内存。下划线开头的内部字段（如 _cancel）不外泄。"""
    return {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
            for k, v in run_doc.items() if not str(k).startswith("_")}


def _persist(run_doc: dict[str, Any], *, finish: bool = False) -> None:
    """把任务落库到 app_regression_runs（BE-P0-1）。payload 存完整 run_doc，重启不丢。"""
    rid = run_doc.get("run_id")
    if not rid:
        return
    with _LOCK:
        doc = _snapshot(run_doc)
    try:
        from server.core.database import SessionLocal
        from server.services import app_automation_service as aas

        with SessionLocal() as db:
            if rid not in _PERSISTED:
                aas.persist_run_start(
                    db, run_id=rid, app_id=doc.get("app_id", "") or "",
                    sn=doc.get("sn", "") or "", platform=doc.get("platform", "android") or "android",
                    total=int(doc.get("total") or 0),
                    run_type=doc.get("run_type") or "manual",
                    run_doc=doc,
                )
                _PERSISTED.add(rid)
            if finish:
                aas.persist_run_finish(db, doc)
            else:
                aas.persist_run_progress(db, doc)
    except Exception as e:  # pragma: no cover
        SLog.w(TAG, f"persist run {rid} failed: {e}")


def _emit_task(run_doc: dict[str, Any], event: str, case: dict[str, Any] | None = None) -> None:
    """广播任务级 WS 事件（BE-P0-4）。"""
    try:
        from server.services.regression import agent_stream, task_store

        with _LOCK:
            doc = _snapshot(run_doc)
        agent_stream.emit_testing_task(task_store.task_event_payload(doc, event, case))
    except Exception as e:  # pragma: no cover
        SLog.d(TAG, f"emit_task failed: {e}")


def _coverage_of(run_doc: dict[str, Any]) -> str:
    cov = str(run_doc.get("coverage") or "once").strip().lower()
    return cov if cov in ("once", "per_device") else "once"


def _sns_of(run_doc: dict[str, Any]) -> list[str]:
    raw = run_doc.get("sns")
    if isinstance(raw, list) and raw:
        out: list[str] = []
        seen: set[str] = set()
        for item in raw:
            sn = str(item or "").strip()
            if sn and sn not in seen:
                seen.add(sn)
                out.append(sn)
        if out:
            return out
    sn = str(run_doc.get("sn") or "").strip()
    return [sn] if sn else []


def _normalize_platform_kind(value: str) -> str:
    plat = str(value or "").lower()
    if plat in ("ios", "iphone", "ipad") or "ios" in plat:
        return "ios"
    if plat == "mixed":
        return "mixed"
    return "android"


def _task_platform_of(kinds: list[str]) -> str:
    uniq = [k for k in dict.fromkeys(kinds) if k in ("android", "ios")]
    if len(uniq) == 1:
        return uniq[0]
    if len(uniq) > 1:
        return "mixed"
    return "android"


def _device_platform_of(run_doc: dict[str, Any], sn: str) -> str:
    plats = run_doc.get("platforms_by_sn")
    if isinstance(plats, dict):
        kind = _normalize_platform_kind(str(plats.get(sn) or ""))
        if kind in ("android", "ios"):
            return kind
    kind = _normalize_platform_kind(str(run_doc.get("platform") or ""))
    return kind if kind in ("android", "ios") else "android"


def _package_of(run_doc: dict[str, Any], platform: str) -> str:
    pkgs = run_doc.get("packages_by_platform")
    if isinstance(pkgs, dict):
        pkg = str(pkgs.get(platform) or "").strip()
        if pkg:
            return pkg
    return str(run_doc.get("package") or "").strip()


def _resolve_platforms_by_sn(
    device_sns: list[str],
    *,
    db: Optional[Session],
    fallback: str,
    given: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    fb = _normalize_platform_kind(fallback)
    if fb not in ("android", "ios"):
        fb = "android"
    out: dict[str, str] = {}
    if isinstance(given, dict):
        for sn in device_sns:
            kind = _normalize_platform_kind(str(given.get(sn) or ""))
            if kind in ("android", "ios"):
                out[sn] = kind
    missing = [sn for sn in device_sns if sn not in out]
    if missing and db is not None:
        try:
            from server.models.mDevice import MDevice

            rows = db.query(MDevice).filter(MDevice.sn.in_(missing)).all()
            by_sn = {str(r.sn): r for r in rows}
            for sn in missing:
                row = by_sn.get(sn)
                if not row:
                    continue
                out[sn] = device_platform_kind(
                    getattr(row, "device_type", ""),
                    getattr(row, "channels", None),
                    sn=sn,
                )
        except Exception as exc:
            SLog.w(TAG, f"resolve platforms_by_sn failed: {exc}")
    for sn in device_sns:
        out.setdefault(sn, fb)
    return out


def _report_run_id(run_id: str, case_id: str, *, sn: str = "", coverage: str = "once") -> str:
    if coverage == "per_device" and sn:
        return f"{run_id}::{case_id}::{sn}"
    return f"{run_id}::{case_id}"


def _row_matches(row: dict[str, Any], entry: dict[str, Any]) -> bool:
    erid = str(entry.get("report_run_id") or "")
    rrid = str(row.get("report_run_id") or "")
    if erid and rrid:
        return erid == rrid
    cid = str(entry.get("case_id") or "")
    if not cid or str(row.get("case_id") or "") != cid:
        return False
    esn = str(entry.get("sn") or "")
    if esn:
        return str(row.get("sn") or "") == esn
    return True


def _upsert_case(run_doc: dict[str, Any], entry: dict[str, Any]) -> None:
    """按 report_run_id（否则 case_id+sn）更新 cases 列表项。调用方需已持 _LOCK。"""
    from server.services.regression.task_store import norm_case_status

    if "status" in entry:
        entry = {**entry, "status": norm_case_status(entry.get("status"))}
    rows = run_doc.setdefault("cases", [])
    merged = entry
    for i, row in enumerate(rows):
        if _row_matches(row, entry):
            merged = {**row, **entry}
            rows[i] = merged
            break
    else:
        rows.append(entry)
    _persist(run_doc)
    if str(entry.get("status") or "") in _CASE_TERMINAL:
        _emit_task(run_doc, "case_finished", merged)


def _mark_case_running(
    run_doc: dict[str, Any], case_id: str, *, name: str = "", sn: str = "", report_run_id: str = "",
) -> None:
    """把某执行单元标为 running。调用方需已持 _LOCK。"""
    rid = report_run_id or _report_run_id(
        str(run_doc.get("run_id") or ""), case_id, sn=sn, coverage=_coverage_of(run_doc),
    )
    entry = {
        "case_id": case_id,
        "name": name,
        "sn": sn,
        "device_platform": _device_platform_of(run_doc, sn) if sn else "",
        "status": "running",
        "report_run_id": rid,
        "summary": "执行中",
        "hitl": False,
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    _upsert_case(run_doc, entry)
    _emit_task(run_doc, "case_running", entry)


def _finish_run(run_doc: dict[str, Any], status: str, *, error: str = "") -> None:
    """收尾任务：置终态 + 落库 + 广播 task_finished。幂等。"""
    with _LOCK:
        run_doc["status"] = status
        if error:
            run_doc["error"] = error
        run_doc["finished_at"] = datetime.now().isoformat(timespec="seconds")
    _persist(run_doc, finish=True)
    _emit_task(run_doc, "task_finished")


def _terminate_remaining_cases(
    run_doc: dict[str, Any], reason: str, *, status: str = "fail", sn: str = "",
) -> int:
    """把还没跑完的执行单元打成终态，并同步任务计数。

    闸门失败要让 completed 追上 total，否则详情页会停在「0/3 已失败」这种自相矛盾
    的进度上。取消场景传 status="cancelled"。`sn` 非空时只收该设备上的单元
    （全机覆盖一台掉线；加速拆分未领取的 sn 为空，不会被误杀）。
    调用方需已持 _LOCK。
    """
    changed = 0
    only_sn = str(sn or "").strip()
    for row in run_doc.get("cases") or []:
        if str(row.get("status") or "") not in ("pending", "running"):
            continue
        if only_sn and str(row.get("sn") or "") != only_sn:
            continue
        row["status"] = status
        row["summary"] = reason
        row["hitl"] = False
        changed += 1
        run_doc["completed"] = int(run_doc.get("completed") or 0) + 1
        if status == "fail":
            run_doc["failed"] = int(run_doc.get("failed") or 0) + 1
        elif status in ("blocked", "declined"):
            run_doc[status] = int(run_doc.get(status) or 0) + 1
    return changed


# ---------- 取消（BE-P1-1） ----------


_CANCELABLE = {"running", "queued"}
_TERMINAL_TASK = {"cancelled", "done", "failed"}


def is_task_cancelled(run_or_task_id: str) -> bool:
    """供 orchestrator / agent 在步骤边界查询。run_id 形如 cr-xxx 或 cr-xxx::case。"""
    tid = str(run_or_task_id or "").split("::", 1)[0].strip()
    if not tid:
        return False
    with _LOCK:
        doc = _RUNS.get(tid)
        return bool(doc and doc.get("_cancel"))


def _revoke_hitl_for_task(task_id: str) -> None:
    try:
        from server.services.regression.hitl import get_session_manager, get_transport

        ids = get_session_manager().revoke_for_task(task_id, reason="task_cancelled")
        transport = get_transport()
        for rid in ids:
            try:
                transport.push_revoke(rid, reason="task_cancelled")
            except Exception:
                pass
        if ids:
            SLog.i(TAG, f"cancel woke {len(ids)} HITL session(s) task={task_id}")
    except Exception as exc:
        SLog.d(TAG, f"revoke hitl on cancel failed: {exc}")


def _cancel_db_only(task_id: str) -> dict[str, Any]:
    """内存没有 worker 时：把 DB 里仍显示 running 的任务收成 cancelled。"""
    from server.core.database import SessionLocal
    from server.models.app_regression_run import AppRegressionRun

    with SessionLocal() as db:
        row = db.query(AppRegressionRun).filter(AppRegressionRun.run_id == task_id).first()
        if row is None:
            return {"ok": False, "reason": f"task not found: {task_id}", "code": 404}
        st = str(row.status or "")
        if st in _TERMINAL_TASK:
            return {"ok": True, "already": True, "status": st, "task_id": task_id}
        payload = dict(row.payload) if isinstance(row.payload, dict) else {}
        reason = "任务已取消"
        completed = int(payload.get("completed") or 0)
        for case in payload.get("cases") or []:
            if not isinstance(case, dict):
                continue
            if str(case.get("status") or "") not in ("pending", "running"):
                continue
            case["status"] = "cancelled"
            case["summary"] = reason
            case["hitl"] = False
            completed += 1
        payload["run_id"] = task_id
        payload["completed"] = completed
        payload["status"] = "cancelled"
        payload["finished_at"] = datetime.now().isoformat(timespec="seconds")
        payload["error"] = payload.get("error") or reason
        from server.services.app_automation_service import persist_run_finish

        persist_run_finish(db, payload)
    SLog.i(TAG, f"cancel offline (no worker) task={task_id}")
    return {"ok": True, "task_id": task_id, "offline": True}


def request_cancel(task_id: str) -> dict[str, Any]:
    """请求取消任务。

    内存中的 worker：置 flag，步骤/HITL/用例边界尽快退出。
    仅 DB 残留 running：直接标 cancelled（进程重启后的僵尸任务）。
    已结束：幂等成功，避免前端报「取消失败」。
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        return {"ok": False, "reason": "task_id is required", "code": 400}

    with _LOCK:
        run_doc = _RUNS.get(task_id)
        if run_doc is not None:
            st = str(run_doc.get("status") or "")
            if st in _TERMINAL_TASK:
                return {"ok": True, "already": True, "status": st, "task_id": task_id}
            if st not in _CANCELABLE:
                return {"ok": False, "reason": f"任务已是 {st} 状态，无法取消", "code": 400}
            run_doc["_cancel"] = True
            current = next(
                (str(r.get("case_id") or "") for r in (run_doc.get("cases") or [])
                 if str(r.get("status") or "") == "running"),
                "",
            )

    if run_doc is None:
        return _cancel_db_only(task_id)

    _revoke_hitl_for_task(task_id)
    SLog.i(TAG, f"cancel requested task={task_id} current_case={current}")
    return {"ok": True, "task_id": task_id, "current_case_id": current}


def _cancelled(run_doc: dict[str, Any]) -> bool:
    with _LOCK:
        return bool(run_doc.get("_cancel"))


# ---------- HITL 联动（BE-P0-4） ----------


def mark_case_hitl(report_run_id: str, waiting: bool, *, question: str = "") -> None:
    """用例进入 / 离开「等待人工」子态。

    hitl 不是独立 status（见 PRD §0）：用例仍是 running，只多一个 hitl 标记，
    这样任务行 / 用例轨能一起点亮，而不是只弹一个全局弹窗。
    report_run_id 形如 "cr-xxx::row-59"。
    """
    rid = str(report_run_id or "")
    if "::" not in rid:
        return
    task_id = rid.split("::", 1)[0]
    with _LOCK:
        run_doc = _RUNS.get(task_id)
        if run_doc is None:
            return
        entry = None
        for row in run_doc.get("cases") or []:
            if str(row.get("report_run_id") or "") == rid:
                row["hitl"] = bool(waiting)
                if waiting and question:
                    row["summary"] = question[:200]
                entry = dict(row)
                break
        if entry is None:
            # 旧数据：report_run_id 可能还没写上，退回 task::case 切分
            case_key = rid.split("::", 1)[1]
            for row in run_doc.get("cases") or []:
                if str(row.get("case_id") or "") == case_key:
                    row["hitl"] = bool(waiting)
                    if waiting and question:
                        row["summary"] = question[:200]
                    entry = dict(row)
                    break
        if entry is None:
            return
    _persist(run_doc)
    _emit_task(run_doc, "hitl", entry)



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


def _normalize_sns(sn: str = "", sns: Optional[list[str]] = None) -> list[str]:
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


def _seed_unit(
    run_id: str, raw: dict[str, Any], index: int, *, sn: str, coverage: str,
    device_platform: str = "",
) -> dict[str, Any]:
    cid = str(raw.get("case_id") or "").strip() or f"case-{index}"
    return {
        "case_id": cid,
        "name": str(raw.get("name") or raw.get("title") or "").strip(),
        "sn": sn,
        "status": "pending",
        "report_run_id": _report_run_id(run_id, cid, sn=sn, coverage=coverage),
        "summary": "",
        "elapsed_ms": 0,
        "hitl": False,
        "module": str(raw.get("module") or "").strip(),
        "precondition": str(raw.get("precondition") or raw.get("precondition_raw") or "").strip(),
        "steps": list(raw.get("steps") or []),
        "expected": list(raw.get("expected") or []),
        "steps_raw": str(raw.get("steps_raw") or "").strip(),
        "expected_raw": str(raw.get("expected_raw") or "").strip(),
        "platform": str(raw.get("platform") or raw.get("client") or raw.get("terminal") or "").strip(),
        "device_platform": str(device_platform or "").strip(),
    }


def run_cases(
    app: Any,
    *,
    sn: str = "",
    sns: Optional[list[str]] = None,
    coverage: str = "",
    platform: str = "android",
    platforms_by_sn: Optional[dict[str, str]] = None,
    case_ids: Optional[list[str]] = None,
    start_index: int = 0,
    db: Optional[Session] = None,
    async_exec: bool = True,
    use_persisted_baseline: bool = True,
    use_cache: bool = True,
    options: Optional[OrchestratorOptions] = None,
    execution_mode: str = "auto",
    run_type: str = "manual",
    requirement_id: str = "",
    release_id: str = "",
    slot_id: str = "",
) -> dict[str, Any]:
    """启动一次 AI-led 回归（可多设备）。

    coverage=once：每条用例只跑一次，设备抢队列。
    coverage=per_device：每台设备各跑完整用例列表。
    单机或缺省覆盖方式都按 once。
    """
    from server.services import app_automation_service as aas
    from server.services.ai.regression.llm_client import resolve_regression_provider

    device_sns = _normalize_sns(sn, sns)
    if not device_sns:
        raise ValueError("请选择执行设备")
    cov = str(coverage or "").strip().lower()
    if cov not in ("", "once", "per_device"):
        raise ValueError("coverage 必须是 once 或 per_device")
    if not cov or len(device_sns) == 1:
        cov = "once"

    all_cases = aas.list_app_cases(app)
    if case_ids:
        by_id = {str(c.get("case_id")): c for c in all_cases if c.get("case_id")}
        cases = [by_id[str(cid)] for cid in case_ids if str(cid) in by_id]
        missing = [cid for cid in case_ids if str(cid) not in by_id]
        if missing:
            SLog.w(TAG, f"missing case_ids: {missing[:5]} (total {len(missing)})")
    else:
        cases = list(all_cases)
    if start_index > 0:
        cases = cases[start_index:]

    env_profile = aas.resolve_env_profile(app)
    packages_by_platform = {
        "android": aas.package_for_app(app, env_profile, platform="android") or "",
        "ios": aas.package_for_app(app, env_profile, platform="ios") or "",
    }
    resolved_platforms = _resolve_platforms_by_sn(
        device_sns, db=db, fallback=platform, given=platforms_by_sn,
    )
    task_platform = _task_platform_of([resolved_platforms[s] for s in device_sns])
    package = packages_by_platform.get(
        task_platform if task_platform in ("android", "ios") else "android",
        "",
    ) or packages_by_platform.get("android") or ""
    app_id = getattr(app, "id", "") or ""
    app_name = getattr(app, "name", "") or ""

    run_id = f"cr-{uuid.uuid4().hex[:12]}"
    if cov == "per_device":
        seeded_cases = [
            _seed_unit(
                run_id, c, i, sn=dsn, coverage=cov,
                device_platform=resolved_platforms.get(dsn, ""),
            )
            for dsn in device_sns
            for i, c in enumerate(cases)
        ]
    else:
        seeded_cases = [
            _seed_unit(run_id, c, i, sn="", coverage=cov)
            for i, c in enumerate(cases)
        ]

    run_doc: dict[str, Any] = {
        "run_id": run_id,
        "engine": "ai_led",
        "run_type": run_type,
        "app_id": app_id,
        "app_name": app_name,
        "sn": device_sns[0],
        "sns": device_sns,
        "coverage": cov,
        "platform": task_platform,
        "platforms_by_sn": resolved_platforms,
        "env_profile": env_profile,
        "package": package,
        "packages_by_platform": packages_by_platform,
        "requirement_id": str(requirement_id or "").strip(),
        "release_id": str(release_id or "").strip(),
        "slot_id": str(slot_id or "").strip(),
        "provider_id": "",
        "provider_name": "",
        "model_name": "",
        "total": len(seeded_cases),
        "completed": 0,
        "passed": 0,
        "failed": 0,
        "blocked": 0,
        "declined": 0,
        "status": "running",
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "finished_at": None,
        "cases": seeded_cases,
        "error": "",
        "connectivity": {},
        "connectivity_by_sn": {},
        "_workers_alive": len(device_sns),
    }
    with _LOCK:
        _RUNS[run_id] = run_doc
    _persist(run_doc)
    _emit_task(run_doc, "task_created")

    if not cases:
        SLog.w(TAG, f"run {run_id} no cases to execute (app={app_id})")
        _finish_run(run_doc, "done")
        return _snapshot(run_doc)

    provider, gate = resolve_regression_provider()
    if not provider:
        reason = gate.get("reason") or "未配置用例执行大模型（密钥配置 → 大模型 Key → 可用 + 用例）"
        with _LOCK:
            run_doc["provider_id"] = gate.get("provider_id") or ""
            _terminate_remaining_cases(run_doc, reason)
        SLog.e(TAG, f"run {run_id} aborted: {reason}")
        _finish_run(run_doc, "failed", error=reason)
        return _snapshot(run_doc)

    provider_id = (provider.get("id") or "").strip()
    model_name = (provider.get("model") or "").strip()
    run_doc["provider_id"] = provider_id
    run_doc["provider_name"] = (provider.get("name") or provider_id).strip()
    run_doc["model_name"] = model_name

    options = options or OrchestratorOptions()
    worker_kw = dict(
        run_doc=run_doc,
        env_profile=env_profile,
        app_id=app_id,
        app_name=app_name,
        use_persisted_baseline=use_persisted_baseline,
        provider_id=provider_id,
        model_name=model_name,
        options=options,
        execution_mode=execution_mode,
    )

    def _make_worker(dsn: str):
        def _worker() -> None:
            plat = _device_platform_of(run_doc, dsn)
            pkg = _package_of(run_doc, plat)
            try:
                _execute(**worker_kw, sn=dsn, platform=plat, package=pkg)
            except Exception as exc:  # pragma: no cover
                SLog.e(TAG, f"run {run_id} worker sn={dsn} crashed: {exc}")
                with _LOCK:
                    _terminate_remaining_cases(run_doc, f"worker crashed: {exc}", sn=dsn)
                _on_worker_exit(run_doc, app_id=app_id)
        return _worker

    threads = [
        threading.Thread(target=_make_worker(dsn), name=f"case-runner-{run_id}-{dsn[:12]}", daemon=True)
        for dsn in device_sns
    ]
    for t in threads:
        t.start()
    if not async_exec:
        for t in threads:
            t.join()
    return _snapshot(run_doc)


def _touch_case_trace(
    *, run_id: str, case_id: str, app_id: str, batch_id: str,
    sn: str, platform: str, device_signature: str = "", ai_provider_id: str = "",
) -> None:
    """用例开跑时在 m_case_run_trace 落一条 running 占位行（BE-P0-3）。失败只 log。"""
    try:
        with memory_repo.session_scope() as db:
            memory_repo.mark_run_trace_running(
                db, run_id=run_id, case_id=case_id, app_id=app_id, batch_id=batch_id,
                sn=sn, platform=platform, device_signature=device_signature,
                ai_provider_id=ai_provider_id,
            )
    except Exception as exc:  # pragma: no cover
        SLog.w(TAG, f"mark_run_trace_running failed {run_id}: {exc}")


def _on_worker_exit(run_doc: dict[str, Any], *, app_id: str) -> None:
    """设备 worker 退出。只有最后一个负责收口任务。"""
    run_id = str(run_doc.get("run_id") or "")
    with _LOCK:
        already = str(run_doc.get("status") or "") in _TERMINAL_TASK
        n = int(run_doc.get("_workers_alive") or 1) - 1
        run_doc["_workers_alive"] = max(0, n)
        last = n <= 0
        cancelled = bool(run_doc.get("_cancel"))
    if already or not last:
        return
    if cancelled:
        with _LOCK:
            left = _terminate_remaining_cases(run_doc, "任务已取消", status="cancelled")
        SLog.i(TAG, f"[{run_id}] cancelled after workers, leftover={left}")
        _emit_task(run_doc, "cancelled")
        _finish_run(run_doc, "cancelled")
        return
    with _LOCK:
        leftover = _terminate_remaining_cases(run_doc, "未执行：无可用设备或 worker 已退出")
    if leftover:
        SLog.w(TAG, f"[{run_id}] leftover {leftover} unit(s) marked fail at worker exit")
    try:
        from server.services.knowledge_capture_service import capture_task_knowledge

        with _LOCK:
            snap = _snapshot(run_doc)
        task_items = capture_task_knowledge(
            app_id=app_id,
            task_id=run_id,
            cases=snap.get("cases") or [],
            provider_id=str(snap.get("provider_id") or ""),
        )
        if task_items:
            with _LOCK:
                run_doc["knowledge_ids"] = [p.get("id") for p in task_items if p.get("id")]
                run_doc["knowledge_proposals"] = task_items
    except Exception as exc:
        SLog.w(TAG, f"[{run_id}] task knowledge capture failed: {exc}")
    _finish_run(run_doc, "done")


def _next_unit(run_doc: dict[str, Any], sn: str, coverage: str) -> Optional[dict[str, Any]]:
    """领取下一条执行单元。once：抢 pending 且尚未绑 sn 的行；per_device：本机 pending。"""
    with _LOCK:
        if run_doc.get("_cancel"):
            return None
        rows = run_doc.get("cases") or []
        if coverage == "per_device":
            for row in rows:
                if str(row.get("sn") or "") == sn and str(row.get("status") or "") == "pending":
                    return dict(row)
            return None
        for row in rows:
            if str(row.get("status") or "") != "pending":
                continue
            if str(row.get("sn") or ""):
                continue
            row["sn"] = sn
            row["device_platform"] = _device_platform_of(run_doc, sn)
            return dict(row)
        return None


def _execute(
    *,
    run_doc: dict[str, Any],
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
    execution_mode: str = "auto",
) -> None:
    """单台设备 worker：绑定线程 SN，按 coverage 领取单元并执行。"""
    from server.services.ai import dispatch_log as dispatch
    from server.services.runtime.device_bind import device_scope
    from server.services.ai import app_profile as app_profile_ctx

    tok = dispatch.bind(
        trigger="case_run",
        source="case_run",
        app_id=app_id,
        app_name=app_name,
        pipeline_id=str(run_doc.get("run_id") or "") or dispatch.new_pipeline_id(),
        role="test-engineer",
    )
    # 绑定被测应用的 UI 画像：登录判定 / 页面识别 / 导航这些通用服务在深层调用点
    # 拿不到 App 对象，靠这个 contextvar 取应用事实（tab 文案等）。
    # 没有对应画像时是通用默认，只用跨应用成立的信号。
    prof_tok = app_profile_ctx.bind(package=package)
    try:
        with device_scope(sn):
            _execute_on_device(
                run_doc=run_doc,
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
                execution_mode=execution_mode,
            )
            _on_worker_exit(run_doc, app_id=app_id)
    finally:
        dispatch.reset(tok)
        app_profile_ctx.reset(prof_tok)


def _execute_on_device(
    *,
    run_doc: dict[str, Any],
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
    execution_mode: str = "auto",
) -> None:
    run_id = run_doc["run_id"]
    coverage = _coverage_of(run_doc)

    # 1) 真设备探测 + 闸门
    try:
        ctx = build_run_context(
            sn=sn,
            platform=platform,
            run_id=run_id,
            app_id=app_id,
            batch_id=run_id,
            provider_id=provider_id,
            model_name=model_name,
            target_package=package,
        )
    except Exception as exc:
        SLog.e(TAG, f"build_run_context failed sn={sn}: {exc}")
        msg = f"build_run_context: {exc}"
        with _LOCK:
            if coverage == "per_device":
                _terminate_remaining_cases(run_doc, msg, sn=sn)
            elif len(_sns_of(run_doc)) <= 1:
                _terminate_remaining_cases(run_doc, msg)
        return

    flags = ctx.connectivity_flags
    conn = {
        "adb": flags.get("adb", False),
        "remote": flags.get("remote", False),
        "vlm": flags.get("vlm", False),
        "hitl": flags.get("hitl", False),
        "ios_wda": flags.get("ios_wda", False),
        "device_signature": ctx.device_signature,
        "channels": {
            "adb": ctx.adb,
            "remote": ctx.remote,
            "ios": ctx.ios,
            "vlm": ctx.vlm,
            "hitl": ctx.hitl,
        },
    }
    with _LOCK:
        by = run_doc.setdefault("connectivity_by_sn", {})
        by[sn] = conn
        run_doc["connectivity"] = conn

    if not flags.get("adb") and not flags.get("remote") and not flags.get("ios_wda"):
        msg = (
            f"device offline: sn={sn} 无可用执行通道；"
            f"adb={ctx.adb.get('state')} remote={ctx.remote.get('state')} "
            f"ios={ctx.ios.get('state')}"
        )
        SLog.e(TAG, f"[{run_id}] {msg}")
        with _LOCK:
            if coverage == "per_device":
                _terminate_remaining_cases(run_doc, msg, sn=sn)
            elif len(_sns_of(run_doc)) <= 1:
                _terminate_remaining_cases(run_doc, msg)
        return

    ios_run = bool(ctx.connectivity_flags.get("ios_wda"))
    if ios_run:
        prefer = ("ios_wda",)
    elif str(sn).startswith("claw-"):
        prefer = ("remote", "adb")
    else:
        prefer = ("adb", "remote")
    router = CapabilityRouter(ctx, capture_prefer=prefer)

    # 2) 领取并执行
    while True:
        if _cancelled(run_doc):
            return
        raw_case = _next_unit(run_doc, sn, coverage)
        if raw_case is None:
            return

        case_started_ts = time.time()
        unit_rid = str(raw_case.get("report_run_id") or "") or _report_run_id(
            run_id, str(raw_case.get("case_id") or ""), sn=sn, coverage=coverage,
        )
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
                cid = raw_case.get("case_id") or "(unknown)"
                run_doc["completed"] += 1
                run_doc["failed"] += 1
                _upsert_case(run_doc, {
                    "case_id": cid,
                    "name": raw_case.get("name") or "",
                    "sn": sn,
                    "status": "fail",
                    "report_run_id": unit_rid if cid != "(unknown)" else "",
                    "summary": f"to_case_spec error: {exc}",
                    "elapsed_ms": int((time.time() - case_started_ts) * 1000),
                })
            continue

        SLog.i(TAG, f"[{run_id}] >>> running case={spec.case_id} ({spec.name}) sn={sn}")
        with _LOCK:
            _mark_case_running(run_doc, spec.case_id, name=spec.name, sn=sn, report_run_id=unit_rid)
        _touch_case_trace(
            run_id=unit_rid, case_id=spec.case_id, app_id=app_id,
            batch_id=run_id, sn=sn, platform=platform,
            device_signature=ctx.device_signature, ai_provider_id=provider_id,
        )

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
                        run_doc["completed"] += 1
                        run_doc["failed"] += 1
                        _upsert_case(run_doc, {
                            "case_id": spec.case_id,
                            "name": spec.name,
                            "sn": sn,
                            "status": "fail",
                            "report_run_id": unit_rid,
                            "summary": before_res.get("msg") or "前置条件不满足",
                            "elapsed_ms": int((time.time() - case_started_ts) * 1000),
                        })
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
                run_id=unit_rid,
                use_persisted_baseline=use_persisted_baseline,
                app_cache_cleared=app_cache_cleared,
                execution_mode=execution_mode,
            )
        except Exception as exc:  # pragma: no cover
            SLog.e(TAG, f"run_case crashed case={spec.case_id}: {exc}")
            if _cancelled(run_doc):
                return
            with _LOCK:
                run_doc["completed"] += 1
                run_doc["failed"] += 1
                _upsert_case(run_doc, {
                    "case_id": spec.case_id,
                    "name": spec.name,
                    "sn": sn,
                    "status": "fail",
                    "report_run_id": unit_rid,
                    "summary": f"run_case crashed: {exc}",
                    "elapsed_ms": int((time.time() - case_started_ts) * 1000),
                })
            continue

        if _cancelled(run_doc):
            return

        with _LOCK:
            entry = {
                "case_id": spec.case_id,
                "name": spec.name,
                "sn": sn,
                "status": str(report.overall_status),
                "report_run_id": report.run_id or unit_rid,
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
                "hitl": False,
                # 统一失败分类（agent 引擎产出）：success|goal_unreachable|execution_error|budget_exhausted|needs_human
                "failure_category": getattr(report, "failure_category", "") or "",
                "failure_label": getattr(report, "failure_label", "") or "",
            }
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
            _upsert_case(run_doc, entry)

        try:
            from server.services.knowledge_capture_service import capture_case_knowledge

            events_raw = []
            for e in (getattr(report, "events", None) or []):
                if hasattr(e, "model_dump"):
                    events_raw.append(e.model_dump())
                elif isinstance(e, dict):
                    events_raw.append(e)
            proposals = capture_case_knowledge(
                app_id=app_id,
                task_id=run_id,
                case_id=spec.case_id,
                case_name=spec.name,
                status=str(report.overall_status),
                summary=str(entry.get("summary") or ""),
                events=events_raw,
                provider_id=str(run_doc.get("provider_id") or ""),
            )
            if proposals:
                with _LOCK:
                    _upsert_case(run_doc, {
                        "case_id": spec.case_id,
                        "sn": sn,
                        "report_run_id": unit_rid,
                        "status": str(report.overall_status),
                        "knowledge_ids": [p.get("id") for p in proposals if p.get("id")],
                        "knowledge_proposals": proposals,
                    })
        except Exception as exc:
            SLog.w(TAG, f"[{run_id}] knowledge capture failed case={spec.case_id}: {exc}")

        SLog.i(
            TAG,
            f"[{run_id}] <<< case={spec.case_id} status={report.overall_status} "
            f"({report.passed}P/{report.failed}F/{report.blocked}B in {report.elapsed_ms}ms) sn={sn}",
        )


# ---------- 重跑失败用例（BE-P1-2） ----------

# 「失败」= 需要复查的三类终态；skipped / cancelled 不算失败，不自动重跑
_RETRY_STATUS = {"fail", "failed", "blocked", "declined"}


def failed_case_ids(task: dict[str, Any]) -> list[str]:
    """从任务 JSON 里取出需要重跑的 case_id（去重、保持原顺序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for row in task.get("cases") or []:
        cid = str(row.get("case_id") or "")
        if not cid or cid in seen:
            continue
        if str(row.get("status") or "") in _RETRY_STATUS:
            seen.add(cid)
            out.append(cid)
    return out


def retry_failed(
    task_id: str,
    *,
    db: Session,
    sn: str = "",
    execution_mode: str = "auto",
) -> dict[str, Any]:
    """重跑某任务里失败的用例：开一条新任务（新 task_id），不改原任务。

    新任务比在原任务上挂 retry 子记录简单得多——进度/计数/trace 全部沿用既有模型，
    前端只要跳到新的 ?task= 即可。
    """
    from server.models.project import App
    from server.services.regression import task_store

    task = task_store.get_task(task_id)
    if task is None:
        return {"ok": False, "reason": f"task not found: {task_id}", "code": 404}
    case_ids = failed_case_ids(task)
    if not case_ids:
        return {"ok": False, "reason": "该任务没有失败/受阻/被拒的用例，无需重跑", "code": 400}

    app_id = str(task.get("app_id") or "")
    app = db.query(App).filter(App.id == app_id).first() if app_id else None
    if app is None:
        return {"ok": False, "reason": f"app not found: {app_id}", "code": 404}

    target_sns = [str(x or "").strip() for x in (task.get("sns") or []) if str(x or "").strip()]
    if sn:
        target_sns = [sn]
    if not target_sns:
        one = (sn or str(task.get("sn") or "")).strip()
        target_sns = [one] if one else []
    if not target_sns:
        return {"ok": False, "reason": "原任务没有记录执行设备，请显式指定 sn", "code": 400}

    for target_sn in target_sns:
        busy_task_id = task_store.busy_task_for_sn(target_sn)
        if busy_task_id:
            return {
                "ok": False,
                "reason": "device busy",
                "code": 409,
                "busy_task_id": busy_task_id,
                "sn": target_sn,
            }

    cov = str(task.get("coverage") or "once").strip().lower()
    if cov not in ("once", "per_device"):
        cov = "once"

    snapshot = run_cases(
        app,
        sn=target_sns[0],
        sns=target_sns,
        coverage=cov,
        platform=str(task.get("platform") or "android").lower(),
        platforms_by_sn=task.get("platforms_by_sn") if isinstance(task.get("platforms_by_sn"), dict) else None,
        case_ids=case_ids,
        db=db,
        async_exec=True,
        execution_mode=execution_mode,
        run_type=str(task.get("run_type") or "manual"),
    )
    SLog.i(TAG, f"retry-failed {task_id} → {snapshot.get('run_id')} cases={len(case_ids)}")
    return {"ok": True, "data": snapshot, "retried_from": task_id, "case_ids": case_ids}


# ---------- 读取 / 查询 ----------


def get_run(run_id: str) -> Optional[dict[str, Any]]:
    """从内存 _RUNS 拿 snapshot；未找到返回 None。"""
    with _LOCK:
        doc = _RUNS.get(run_id)
        return _snapshot(doc) if doc else None


def list_runs(limit: int = 30, *, app_id: str = "") -> list[dict[str, Any]]:
    """内存热快照列表。app_id 非空时只返回该应用的 run（过渡期用，任务列表请走 /tasks）。"""
    with _LOCK:
        items = [_snapshot(d) for d in _RUNS.values()]
    if app_id:
        items = [d for d in items if str(d.get("app_id") or "") == app_id]
    items.sort(key=lambda d: d.get("started_at") or "", reverse=True)
    return items[:limit]


def list_recent_traces(
    *,
    case_id: Optional[str] = None,
    device_signature: Optional[str] = None,
    app_id: Optional[str] = None,
    batch_id: Optional[str] = None,
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
                app_id=app_id,
                batch_id=batch_id,
                only_pass=only_pass,
                limit=limit,
            )
            for r in rows:
                fc, fl = "", ""
                try:
                    rp = getattr(r, "report_payload", None)
                    d = json.loads(rp) if isinstance(rp, str) else (rp or {})
                    if isinstance(d, dict):
                        fc = d.get("failure_category") or ""
                        fl = d.get("failure_label") or ""
                except Exception:
                    pass
                out.append({
                    "run_id": r.run_id,
                    "case_id": r.case_id,
                    "app_id": r.app_id or "",
                    "batch_id": r.batch_id or memory_repo.split_batch_id(r.run_id),
                    "device_signature": r.device_signature,
                    "sn": r.sn,
                    "platform": r.platform,
                    "ai_provider_id": r.ai_provider_id,
                    "overall_status": r.overall_status,
                    "failure_category": fc,
                    "failure_label": fl,
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
            _rp = r.report_payload or {}
            _rpd = json.loads(_rp) if isinstance(_rp, str) else (_rp if isinstance(_rp, dict) else {})
            return {
                "run_id": r.run_id,
                "case_id": r.case_id,
                "app_id": r.app_id or "",
                "batch_id": r.batch_id or memory_repo.split_batch_id(r.run_id),
                "device_signature": r.device_signature,
                "sn": r.sn,
                "platform": r.platform,
                "overall_status": r.overall_status,
                "failure_category": _rpd.get("failure_category", "") if isinstance(_rpd, dict) else "",
                "failure_label": _rpd.get("failure_label", "") if isinstance(_rpd, dict) else "",
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
