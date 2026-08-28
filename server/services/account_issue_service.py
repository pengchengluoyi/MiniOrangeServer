# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""开跑前从号池申请本条用例要用的测试账号。不锁号、不切会话。"""
from __future__ import annotations

import re
from typing import Any, Optional

from script.log import SLog

TAG = "AccountIssue"

EMPTY_BRIEF = "（号池无可用账号；登录所需手机号只能问人）"

_SMS_IN_NOTE = re.compile(
    r"(?:验证码|sms(?:_?code)?)\s*(?:为|是|=|:|：)\s*(\d{4,8})",
    re.I,
)


def extract_account_sms(note: str = "") -> str:
    """只认备注里写明的固定验证码，不把手机号当成验证码。"""
    m = _SMS_IN_NOTE.search(str(note or ""))
    return (m.group(1) if m else "").strip()


def _phone_ok(phone: str) -> bool:
    p = re.sub(r"\s+", "", str(phone or ""))
    return bool(re.fullmatch(r"\d{8,13}", p))


def format_accounts_brief(ranked: list[dict], *, picked: Optional[dict] = None) -> str:
    from server.services.project_env import account_ident, account_label

    rows = [r for r in (ranked or []) if isinstance(r, dict)]
    top = picked if isinstance(picked, dict) and picked else (rows[0] if rows else None)
    if not top:
        return EMPTY_BRIEF
    lines = ["登录页优先用【首选】的手机号 input_text，禁止再问人要号。"]
    sms = str(top.get("sms_code") or extract_account_sms(str(top.get("note") or "")))
    phone = str(top.get("phone") or "").strip()
    tags = [str(t).strip() for t in (top.get("tags") or []) if str(t).strip()]
    head = f"首选：{account_label(top)}"
    if phone and phone != account_ident(top):
        head += f" 手机号 {phone}"
    if tags:
        head += " 标签：" + "、".join(tags[:6])
    lines.append(head)
    reason = str(top.get("reason") or "").strip()
    score = top.get("score")
    if reason or score is not None:
        extra = reason
        if score is not None:
            extra = f"{extra}（{score} 分）" if extra else f"{score} 分"
        lines.append(f"匹配：{extra}")
    if sms:
        lines.append(f"固定验证码：{sms}（来自号池备注，验证码页直接填，不要问人）")
    elif str(top.get("note") or "").strip():
        lines.append(f"备注：{str(top.get('note')).strip()[:120]}")
    rest = [r for r in rows[1:6] if str(r.get("id") or "") != str(top.get("id") or "")]
    if rest:
        lines.append("其它候选：")
        for row in rest:
            bit = f"- {account_label(row)}"
            if row.get("reason"):
                bit += f" · {row.get('reason')}"
            lines.append(bit)
    if not phone:
        lines.append("首选没有手机号，登录页才允许 ask_human 要号码。")
    return "\n".join(lines)


def _env_doc_for_app(app_id: str) -> dict:
    """读项目 env JSON。走 SQL 文本，避开未装配完的 Graph ORM。"""
    import json
    from sqlalchemy import text
    from server.core.database import SessionLocal
    from server.services.project_env import default_project_env, normalize_project_env

    with SessionLocal() as db:
        row = db.execute(text("SELECT project_id FROM apps WHERE id = :id"), {"id": app_id}).first()
        if not row or not row[0]:
            return {}
        proj = db.execute(text("SELECT env FROM projects WHERE id = :id"), {"id": row[0]}).first()
    raw = proj[0] if proj else None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except Exception:
            raw = {}
    if not isinstance(raw, dict):
        return default_project_env()
    return normalize_project_env(raw)


def issue_account_for_case(
    *,
    app_id: str = "",
    env_profile: str = "",
    case_id: str = "",
    case_name: str = "",
    preconditions: str = "",
) -> dict[str, Any]:
    """按用例前置+名称从项目号池挑匹配最高的号。失败返回空 brief，不抛给执行链。"""
    from server.services.project_env import account_ident, list_test_accounts, pick_test_accounts

    empty = {"picked": {}, "ranked": [], "brief": EMPTY_BRIEF, "phone": "", "sms_code": ""}
    if not str(app_id or "").strip():
        return empty
    prompt = " ".join(x for x in (preconditions, case_name, case_id) if str(x).strip())
    try:
        doc = _env_doc_for_app(app_id)
        ranked = pick_test_accounts(
            list_test_accounts(doc), prompt=prompt, env=env_profile or "",
        )
    except Exception as exc:
        SLog.w(TAG, f"pick failed case={case_id}: {exc}")
        return {**empty, "error": str(exc)}

    if not ranked:
        _record_pick(case_id, prompt, env_profile, [], {})
        return empty
    top = dict(ranked[0])
    sms = extract_account_sms(str(top.get("note") or ""))
    top["sms_code"] = sms
    phone = str(top.get("phone") or "").strip()
    brief = format_accounts_brief(ranked, picked=top)
    issued = {
        "picked": top,
        "ranked": ranked,
        "brief": brief,
        "phone": phone if _phone_ok(phone) else "",
        "sms_code": sms,
    }
    _record_pick(case_id, prompt, env_profile, ranked, top)
    SLog.i(
        TAG,
        f"case={case_id} picked={phone or account_ident(top)!r} "
        f"env={top.get('env')!r} score={top.get('score')} sms={'yes' if sms else 'no'}",
    )
    return issued


def bind_account_for_case(
    ctx,
    *,
    app_id: str = "",
    env_profile: str = "",
    case_id: str = "",
    case_name: str = "",
    preconditions: str = "",
) -> dict[str, Any]:
    issued = issue_account_for_case(
        app_id=app_id,
        env_profile=env_profile,
        case_id=case_id,
        case_name=case_name,
        preconditions=preconditions,
    )
    if ctx is None:
        return issued
    ctx.accounts_brief = str(issued.get("brief") or EMPTY_BRIEF)
    ctx.picked_account = dict(issued.get("picked") or {})
    return issued


def _record_pick(case_id: str, prompt: str, env: str, ranked: list, top: dict) -> None:
    try:
        from server.services.ai import dispatch_log as dispatch

        public = [
            {
                "id": r.get("id"),
                "phone": r.get("phone"),
                "env": r.get("env"),
                "tags": r.get("tags") or [],
                "score": r.get("score"),
                "reason": r.get("reason"),
            }
            for r in (ranked or [])[:6]
        ]
        dispatch.record_job(
            status="done" if top else "skipped",
            job="pick_account",
            role="test-engineer",
            skill="pick_account",
            source="case_run",
            detail="筛测试账号",
            input_data={"case_id": case_id, "env": env, "prompt": (prompt or "")[:400]},
            output_data={
                "picked": public[0] if public else {},
                "ranked": public,
            },
        )
    except Exception as exc:
        SLog.d(TAG, f"dispatch pick_account failed: {exc}")
