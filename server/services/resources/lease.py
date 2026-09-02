# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""开跑前租账号、跑完还账号。锁的是租约，不切会话。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from script.log import SLog

TAG = "ResourceLease"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _project_id_for_app(app_id: str) -> str:
    from sqlalchemy import text
    from server.core.database import SessionLocal

    if not str(app_id or "").strip():
        return ""
    with SessionLocal() as db:
        row = db.execute(text("SELECT project_id FROM apps WHERE id = :id"), {"id": app_id}).first()
    return str(row[0] or "") if row else ""


def _patch_account(app_id: str, account_id: str, patch: dict[str, Any]) -> None:
    from sqlalchemy.orm.attributes import flag_modified
    from server.core.database import SessionLocal
    from server.models.project import Project
    from server.services.project_env import load_project_env, save_test_accounts, list_test_accounts

    pid = _project_id_for_app(app_id)
    aid = str(account_id or "").strip()
    if not pid or not aid:
        return
    with SessionLocal() as db:
        project = db.query(Project).filter(Project.id == pid).first()
        if not project:
            return
        doc = load_project_env(db, pid)
        rows = list_test_accounts(doc)
        changed = False
        next_rows = []
        for row in rows:
            item = dict(row)
            if str(item.get("id") or "") == aid:
                item.update(patch)
                changed = True
            next_rows.append(item)
        if not changed:
            return
        project.env = save_test_accounts(doc, next_rows)
        flag_modified(project, "env")
        db.commit()


def _attach_env_snapshot(ctx, *, app_id: str, env_key: str) -> None:
    from server.services.runtime.env_gate import attach_run_env

    attach_run_env(
        ctx,
        app_id=app_id,
        env_profile=env_key,
        platform=str(getattr(ctx, "platform", "") or ""),
    )


def lease_account(
    ctx,
    *,
    app_id: str = "",
    env_profile: str = "",
    case_id: str = "",
    case_name: str = "",
    preconditions: str = "",
    run_id: str = "",
    surface: str = "",
    platform: str = "",
    target_id: str = "",
) -> dict[str, Any]:
    from server.services.account_issue_service import issue_account_for_case, EMPTY_BRIEF

    issued = issue_account_for_case(
        app_id=app_id,
        env_profile=env_profile,
        case_id=case_id,
        case_name=case_name,
        preconditions=preconditions,
        surface=surface,
        platform=platform,
        target_id=target_id,
    )
    picked = dict(issued.get("picked") or {})
    env_key = str(
        (getattr(ctx, "env_profile", None) if ctx is not None else "")
        or env_profile
        or picked.get("env")
        or ""
    ).strip()
    if ctx is not None:
        ctx.accounts_brief = str(issued.get("brief") or EMPTY_BRIEF)
        ctx.picked_account = picked
        _attach_env_snapshot(ctx, app_id=app_id, env_key=env_key)
        rid = str(run_id or getattr(ctx, "run_id", "") or getattr(ctx, "batch_id", "") or "").strip()
        if picked.get("id") and rid:
            lease = {"run_id": rid, "case_id": str(case_id or ""), "at": _now()}
            ctx.resource_lease = {"account_id": picked.get("id"), "run_id": rid, "case_id": case_id}
            try:
                _patch_account(app_id, str(picked.get("id")), {"lease": lease})
                ctx.picked_account["lease"] = lease
            except Exception as exc:
                SLog.w(TAG, f"lease persist failed: {exc}")
        else:
            ctx.resource_lease = {}
    return issued


def release_account(ctx) -> None:
    if ctx is None:
        return
    lease = getattr(ctx, "resource_lease", None) or {}
    aid = str(lease.get("account_id") or "").strip()
    app_id = str(getattr(ctx, "app_id", "") or "")
    if not aid or not app_id:
        ctx.resource_lease = {}
        return
    try:
        _patch_account(app_id, aid, {"lease": {}})
    except Exception as exc:
        SLog.w(TAG, f"release failed account={aid}: {exc}")
    ctx.resource_lease = {}
