# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""任务中心存储/查询层（BE-P0-2）。

把 app_regression_runs 行（payload = 完整 run_doc）与内存 _RUNS 热快照，统一成
docs/prd_testing_platform.md §0 的「任务 JSON」形状。running 任务以内存为准（更热），
已结束任务从 DB payload 还原（重启不丢）。

也提供任务级 WS 事件 payload 构造 与 设备占用检测（BE-P0-4/5 复用）。
"""
from __future__ import annotations

from typing import Any, Optional

from script.log import SLog

TAG = "TaskStore"

# 用例终态（用于算 completed）
_CASE_TERMINAL = {"pass", "fail", "blocked", "declined", "skipped", "cancelled"}

# 引擎内部状态 → PRD §0 用例枚举。禁止让 failed / partial 这类同义词漏到前端：
#   - failed  : 旧数据别名
#   - partial : RunReport 的「部分通过」，任务计数里本来就按失败算
_CASE_STATUS_ALIAS = {"failed": "fail", "partial": "fail"}


def _norm_case_status(status: Any) -> str:
    st = str(status or "").strip().lower()
    return _CASE_STATUS_ALIAS.get(st, st)


norm_case_status = _norm_case_status


def _int(v) -> int:
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return 0


def _norm_cases(cases: Any) -> list[dict[str, Any]]:
    """统一用例行：状态收敛到契约枚举，补齐前端要用的字段。"""
    out: list[dict[str, Any]] = []
    for c in cases or []:
        if not isinstance(c, dict):
            continue
        row = dict(c)
        raw = str(row.get("status") or "")
        norm = _norm_case_status(raw)
        row["status"] = norm
        if norm != raw.strip().lower():
            row["raw_status"] = raw  # 保留引擎原值，便于排查
        row["hitl"] = bool(row.get("hitl", False))
        row["summary"] = row.get("summary", "") or ""
        row["elapsed_ms"] = _int(row.get("elapsed_ms"))
        out.append(row)
    return out


def to_task_json(doc: dict[str, Any] | None, *, run_type: str = "manual",
                 status: str = "", run_type_col: str = "",
                 started_at: str = "", finished_at: str = "",
                 include_cases: bool = True) -> dict[str, Any]:
    """run_doc / payload → 统一任务 JSON（§0 形状）。"""
    doc = doc or {}
    cases = _norm_cases(doc.get("cases"))
    total = _int(doc.get("total")) or len(cases)
    completed = _int(doc.get("completed"))
    if not completed and cases:
        completed = sum(1 for c in cases if c.get("status") in _CASE_TERMINAL)
    passed = _int(doc.get("passed"))
    failed = _int(doc.get("failed"))
    blocked = _int(doc.get("blocked"))
    declined = _int(doc.get("declined"))
    st = status or doc.get("status") or "running"
    current = ""
    hitl = False
    for c in cases:
        if c.get("status") == "running":
            current = current or (c.get("case_id") or "")
            hitl = hitl or bool(c.get("hitl"))
    sns: list[str] = []
    seen: set[str] = set()
    for item in list(doc.get("sns") or []):
        s = str(item or "").strip()
        if s and s not in seen:
            seen.add(s)
            sns.append(s)
    primary_sn = str(doc.get("sn") or "").strip()
    if primary_sn and primary_sn not in seen:
        sns.insert(0, primary_sn)
        seen.add(primary_sn)
    if not sns and primary_sn:
        sns = [primary_sn]
    coverage = str(doc.get("coverage") or "once").strip().lower()
    if coverage not in ("once", "per_device"):
        coverage = "once"

    task = {
        "task_id": doc.get("run_id") or doc.get("task_id") or "",
        "app_id": doc.get("app_id", "") or "",
        "app_name": doc.get("app_name", "") or "",
        "run_type": doc.get("run_type") or run_type_col or run_type,
        "sn": sns[0] if sns else "",
        "sns": sns,
        "coverage": coverage,
        "platform": doc.get("platform", "android") or "android",
        "platforms_by_sn": dict(doc.get("platforms_by_sn") or {}) if isinstance(doc.get("platforms_by_sn"), dict) else {},
        "packages_by_platform": dict(doc.get("packages_by_platform") or {}) if isinstance(doc.get("packages_by_platform"), dict) else {},
        "env_profile": doc.get("env_profile", "") or "",
        "package": doc.get("package", "") or "",
        "requirement_id": doc.get("requirement_id", "") or "",
        "release_id": doc.get("release_id", "") or "",
        "slot_id": doc.get("slot_id", "") or "",
        "status": st,
        "total": total,
        "completed": min(completed, total) if total else completed,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "declined": declined,
        "progress": min(100, int(round(completed / total * 100))) if total else 0,
        "pass_rate": int(round(passed / completed * 100)) if completed else 0,
        "error": doc.get("error", "") or "",
        "provider_name": doc.get("provider_name", "") or "",
        "model_name": doc.get("model_name", "") or "",
        "started_at": doc.get("started_at") or started_at or None,
        "finished_at": doc.get("finished_at") or finished_at or None,
        "busy": st == "running",
        "current_case_id": current,
        "hitl": hitl,
        "title": doc.get("title") or "",
        "role": doc.get("role") or "",
        "kind": doc.get("kind") or "",
        "knowledge_ids": list(doc.get("knowledge_ids") or []),
        "knowledge_proposals": list(doc.get("knowledge_proposals") or []),
    }
    if include_cases:
        task["cases"] = cases
        if doc.get("atlas_patch"):
            task["atlas_patch"] = doc.get("atlas_patch")
    return task


def task_event_payload(run_doc: dict[str, Any], event: str,
                       case: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """构造 WS testing_task 事件 data（§12.1）。"""
    t = to_task_json(run_doc, include_cases=False)
    data = {
        "event": event,
        "task_id": t["task_id"],
        "app_id": t["app_id"],
        "status": t["status"],
        "completed": t["completed"],
        "total": t["total"],
        "passed": t["passed"],
        "failed": t["failed"],
        "blocked": t["blocked"],
        "declined": t["declined"],
        "progress": t["progress"],
        "current_case_id": t["current_case_id"],
        "hitl": t["hitl"],
        "sn": t.get("sn") or "",
        "sns": list(t.get("sns") or []),
        "coverage": t.get("coverage") or "once",
        "platform": t.get("platform") or "android",
        "platforms_by_sn": dict(t.get("platforms_by_sn") or {}),
        "knowledge_ids": list(t.get("knowledge_ids") or []),
        "knowledge_proposals": list(t.get("knowledge_proposals") or []),
    }
    if case:
        data["case"] = {
            "case_id": case.get("case_id"),
            "status": _norm_case_status(case.get("status")),
            "report_run_id": case.get("report_run_id"),
            "sn": case.get("sn") or "",
            "device_platform": case.get("device_platform") or "",
            "summary": case.get("summary", ""),
            "hitl": bool(case.get("hitl", False)),
            "knowledge_ids": list(case.get("knowledge_ids") or []),
            "knowledge_proposals": list(case.get("knowledge_proposals") or []),
        }
    return data


def _memory_runs() -> dict[str, dict]:
    from server.services.regression import case_runner
    with case_runner._LOCK:  # noqa: SLF001
        return {k: case_runner._snapshot(v) for k, v in case_runner._RUNS.items()}  # noqa: SLF001


def list_tasks(app_id: str, *, status: str = "", limit: int = 30, offset: int = 0) -> dict[str, Any]:
    """按 app_id 列任务。DB 权威 + 内存热快照覆盖 running。"""
    from server.core.database import SessionLocal
    from server.models.app_regression_run import AppRegressionRun

    tasks: dict[str, dict] = {}
    # 1) 内存热任务（含刚下发、尚未落库的）
    for rid, doc in _memory_runs().items():
        if str(doc.get("app_id") or "") != app_id:
            continue
        tasks[rid] = to_task_json(doc, include_cases=False)
    # 2) DB 历史任务（payload = 完整 run_doc）
    try:
        with SessionLocal() as db:
            rows = (
                db.query(AppRegressionRun)
                .filter(AppRegressionRun.app_id == app_id)
                .order_by(AppRegressionRun.started_at.desc())
                .limit(500)
                .all()
            )
            for r in rows:
                if r.run_id in tasks:
                    continue  # 内存更热
                payload = r.payload if isinstance(r.payload, dict) else {}
                tasks[r.run_id] = to_task_json(
                    payload, status=r.status, run_type_col=r.run_type,
                    started_at=r.started_at.isoformat() if r.started_at else "",
                    finished_at=r.finished_at.isoformat() if r.finished_at else "",
                    include_cases=False,
                )
    except Exception as exc:
        SLog.w(TAG, f"list_tasks db failed app={app_id}: {exc}")

    items = list(tasks.values())
    items.sort(key=lambda t: (t.get("started_at") or ""), reverse=True)
    if status:
        items = [t for t in items if t.get("status") == status]
    total = len(items)
    items = items[offset: offset + limit] if limit else items[offset:]
    return {"items": items, "total": total}


def _patch_proposal_status(doc: dict[str, Any], kid: str, review_status: str) -> bool:
    changed = False

    def patch_list(items):
        nonlocal changed
        for row in items or []:
            if isinstance(row, dict) and str(row.get("id") or "") == kid:
                if str(row.get("review_status") or "") != review_status:
                    row["review_status"] = review_status
                    changed = True

    patch_list(doc.get("knowledge_proposals"))
    for case in doc.get("cases") or []:
        if isinstance(case, dict):
            patch_list(case.get("knowledge_proposals"))
    return changed


def _hydrate_proposal_review(task: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """用知识库里的审核状态覆盖任务快照，避免已通过的草稿刷新后又冒出来。"""
    if not task:
        return task
    try:
        from server.services.system_settings_service import list_testing_knowledge

        by_id = {
            str(x.get("id")): str(x.get("review_status") or "pending")
            for x in (list_testing_knowledge() or [])
            if isinstance(x, dict) and x.get("id")
        }
    except Exception as exc:
        SLog.w(TAG, f"hydrate knowledge failed: {exc}")
        return task

    def apply(items):
        for row in items or []:
            if not isinstance(row, dict):
                continue
            kid = str(row.get("id") or "")
            if kid and kid in by_id:
                row["review_status"] = by_id[kid]

    apply(task.get("knowledge_proposals"))
    for case in task.get("cases") or []:
        if isinstance(case, dict):
            apply(case.get("knowledge_proposals"))
    return task


def mark_knowledge_proposal(task_id: str, kid: str, review_status: str) -> bool:
    """把审核结果写回任务快照（内存 + 落库），刷新后不再回到待审核。"""
    from server.services.regression import case_runner
    from server.core.database import SessionLocal
    from server.models.app_regression_run import AppRegressionRun
    from sqlalchemy.orm.attributes import flag_modified

    tid = str(task_id or "").strip()
    kid = str(kid or "").strip()
    st = str(review_status or "").strip().lower()
    if not tid or not kid or st not in ("pending", "approved", "rejected"):
        return False

    live = None
    with case_runner._LOCK:  # noqa: SLF001
        live = case_runner._RUNS.get(tid)  # noqa: SLF001
        if live is not None:
            _patch_proposal_status(live, kid, st)
    if live is not None:
        case_runner._persist(live)  # noqa: SLF001
        return True

    try:
        with SessionLocal() as db:
            row = db.query(AppRegressionRun).filter(AppRegressionRun.run_id == tid).first()
            if row is None:
                return False
            payload = dict(row.payload) if isinstance(row.payload, dict) else {}
            if not _patch_proposal_status(payload, kid, st):
                return False
            row.payload = payload
            flag_modified(row, "payload")
            db.commit()
            return True
    except Exception as exc:
        SLog.w(TAG, f"mark_knowledge_proposal failed {tid} {kid}: {exc}")
        return False


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    """任务详情（含全量 cases）。running 优先内存，否则 DB payload。"""
    from server.services.regression import case_runner
    from server.core.database import SessionLocal
    from server.models.app_regression_run import AppRegressionRun

    mem = case_runner.get_run(task_id)
    if mem is not None:
        return _hydrate_proposal_review(to_task_json(mem, include_cases=True))
    try:
        with SessionLocal() as db:
            r = db.query(AppRegressionRun).filter(AppRegressionRun.run_id == task_id).first()
            if r is None:
                return None
            payload = r.payload if isinstance(r.payload, dict) else {}
            return _hydrate_proposal_review(to_task_json(
                payload, status=r.status, run_type_col=r.run_type,
                started_at=r.started_at.isoformat() if r.started_at else "",
                finished_at=r.finished_at.isoformat() if r.finished_at else "",
                include_cases=True,
            ))
    except Exception as exc:
        SLog.w(TAG, f"get_task db failed {task_id}: {exc}")
        return None


def busy_task_for_sn(sn: str) -> str:
    """该 SN 是否有正在运行的任务；返回占用 task_id 或空串。

    只看内存：worker 只活在本进程，DB 里若还留着 running 行那是上次进程的残留
    （启动时由 reconcile_stale_runs 收尾），不该继续占着设备。
    """
    if not sn:
        return ""
    for rid, doc in _memory_runs().items():
        if str(doc.get("status") or "") != "running":
            continue
        sns = [str(x or "").strip() for x in (doc.get("sns") or []) if str(x or "").strip()]
        if not sns:
            one = str(doc.get("sn") or "").strip()
            sns = [one] if one else []
        if sn in sns:
            return rid
    return ""


def busy_map() -> dict[str, str]:
    """{sn: 占用它的 task_id}，供 GET /devices 一次性标注 busy_task_id。"""
    out: dict[str, str] = {}
    for rid, doc in _memory_runs().items():
        if str(doc.get("status") or "") != "running":
            continue
        sns = [str(x or "").strip() for x in (doc.get("sns") or []) if str(x or "").strip()]
        if not sns:
            one = str(doc.get("sn") or "").strip()
            sns = [one] if one else []
        for sn in sns:
            out.setdefault(sn, rid)
    return out


def summary_for_apps(app_ids: list[str]) -> list[dict[str, Any]]:
    """每个 app 的「运行中条数 + 最近一条任务」（BE-P1-3，应用卡片角标用）。

    走任务表而不是扫 traces，所以一次查询就能覆盖多个 app。
    """
    from server.core.database import SessionLocal
    from server.models.app_regression_run import AppRegressionRun

    ids = [a for a in (app_ids or []) if a]
    out: dict[str, dict[str, Any]] = {
        a: {
            "app_id": a,
            "running_count": 0,
            "latest_task_id": "",
            "status": "",
            "completed": 0,
            "total": 0,
            "started_at": None,
            "pass_rate": 0,
            "last_status": "",
            "last_task_id": "",
            "last_started_at": None,
            "last_pass_rate": 0,
            "total_count": 0,
        }
        for a in ids
    }
    if not ids:
        return list(out.values())

    def _apply_latest(item: dict[str, Any], task: dict[str, Any]) -> None:
        started = str(task.get("started_at") or "")
        prev = str(item.get("started_at") or "")
        if item["latest_task_id"] and started <= prev:
            return
        item["latest_task_id"] = task.get("task_id") or ""
        item["last_task_id"] = item["latest_task_id"]
        item["status"] = task.get("status") or ""
        item["last_status"] = item["status"]
        item["completed"] = task.get("completed") or 0
        item["total"] = task.get("total") or 0
        item["started_at"] = task.get("started_at")
        item["last_started_at"] = item["started_at"]
        item["pass_rate"] = task.get("pass_rate") or 0
        item["last_pass_rate"] = item["pass_rate"]

    running_by_app: dict[str, set[str]] = {a: set() for a in ids}
    for rid, doc in _memory_runs().items():
        aid = str(doc.get("app_id") or "")
        if aid not in out:
            continue
        task = to_task_json(doc, include_cases=False)
        if task.get("status") == "running":
            running_by_app[aid].add(rid)
        _apply_latest(out[aid], task)

    try:
        with SessionLocal() as db:
            rows = (
                db.query(AppRegressionRun)
                .filter(AppRegressionRun.app_id.in_(ids))
                .order_by(AppRegressionRun.started_at.desc())
                .all()
            )
        for r in rows:
            item = out.get(r.app_id)
            if item is None:
                continue
            item["total_count"] += 1
            payload = r.payload if isinstance(r.payload, dict) else {}
            task = to_task_json(
                payload, status=r.status, run_type_col=r.run_type,
                started_at=r.started_at.isoformat() if r.started_at else "",
                include_cases=False,
            )
            _apply_latest(item, task)
    except Exception as exc:
        SLog.w(TAG, f"summary_for_apps db failed: {exc}")

    for aid, rids in running_by_app.items():
        out[aid]["running_count"] = len(rids)
    return list(out.values())
