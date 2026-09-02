# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""用例跑完后给账号管理里的账号追加标签。不改发号、不切换登录。"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm.attributes import flag_modified

from script.log import SLog

TAG = "AccountTag"
_TAG_MAX_CHARS = 5
_TAG_ADD_LIMIT = 4

_TAG_SYSTEM = """你根据一条测试用例的执行结果，给「本次用到的测试账号」更新业务标签。

只返回 JSON：
{"tags":["已注册"],"replaces":["未注册手机号"],"reason":"一句话"}

规则：
- tags：本步要追加或确认的状态。每条最多 5 个字，最多 4 条。
- 优先从【账号已有标签】里原样复用（旧标签即使超过 5 字也要照抄，不要改字）。
- 已有标签都不贴切时，自己生成新标签，仍不超过 5 字。
- 不要只从「已注册 / 已登录 / 未注册」里选；领礼、付费、游客等业务状态都可以。
- replaces：与本次新标签逻辑互斥、必须删掉的旧标签。必须用【当前号已有标签】里的原文。
  例如打「已注册」或「已登录」时，把「未注册」「未注册手机号」放进 replaces。
  打「已登录」时同时删掉「未登录」。
- 只根据执行事实。看不出变化就 tags=[]、replaces=[]。
- 禁止 Markdown。"""

# 互斥：一边出现就要清掉另一边（含「未注册手机号」这种加长写法）
_MUTEX_PAIRS = (
    ("已注册", "未注册"),
    ("已登录", "未登录"),
    ("已登录", "未注册"),
    ("已领取", "未领取"),
    ("已付费", "未付费"),
)


def _normalize_tags(
    raw: Any,
    *,
    limit: int = _TAG_ADD_LIMIT,
    max_chars: int = _TAG_MAX_CHARS,
    reuse: Optional[set[str]] = None,
) -> list[str]:
    rows = raw if isinstance(raw, list) else []
    known = set(reuse or [])
    out: list[str] = []
    seen: set[str] = set()
    for item in rows:
        s = str(item or "").strip().replace(" ", "")
        if not s:
            continue
        if s not in known:
            s = s[:max_chars]
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _tags_conflict(left: str, right: str) -> bool:
    if not left or not right or left == right:
        return False
    for pos, neg in _MUTEX_PAIRS:
        if (pos in left and neg in right) or (neg in left and pos in right):
            return True
    return False


def exclusive_drops(add: list[str], existing: list[str]) -> set[str]:
    """新标签与旧标签互斥时，要删的旧标签（含未注册手机号这类加长写法）。"""
    drop: set[str] = set()
    for new in add:
        for old in existing:
            if _tags_conflict(new, old):
                drop.add(old)
    return drop


def apply_tag_update(existing: list[str], add: list[str], drop: list[str]) -> list[str]:
    have = [str(t).strip() for t in (existing or []) if str(t).strip()]
    incoming = [str(t).strip() for t in (add or []) if str(t).strip()]
    removed = {str(t).strip() for t in (drop or []) if str(t).strip()}
    removed |= exclusive_drops(incoming, have)
    tags = [t for t in have if t not in removed]
    for t in incoming:
        if t not in tags:
            tags.append(t)
    return tags[:24]


def _pool_tag_catalog(rows: list[dict]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for row in rows or []:
        for t in row.get("tags") or []:
            s = str(t or "").strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out[:80]


def tag_account_after_case(
    *,
    app_id: str,
    env_profile: str = "",
    case_id: str = "",
    case_name: str = "",
    preconditions: str = "",
    status: str = "",
    summary: str = "",
    provider_id: str = "",
    account_id: str = "",
) -> Optional[dict[str, Any]]:
    from server.core.database import SessionLocal
    from server.models.project import App
    from server.services.ai.regression.llm_client import call_chat_text, resolve_regression_provider
    from server.services.project_env import (
        account_ident,
        account_label,
        list_test_accounts,
        load_project_env,
        pick_test_accounts,
        save_test_accounts,
    )

    prompt = " ".join(x for x in (preconditions, case_name, case_id) if str(x).strip())
    if not app_id:
        return None
    with SessionLocal() as db:
        app = db.query(App).filter(App.id == app_id).first()
        if not app or not app.project_id:
            return None
        doc = load_project_env(db, app.project_id)
        accounts = list_test_accounts(doc)
        top = None
        want = str(account_id or "").strip()
        if want:
            top = next((r for r in accounts if str(r.get("id") or "") == want), None)
        if top is None:
            ranked = pick_test_accounts(
                accounts,
                prompt=prompt,
                env=env_profile or "",
                channels=doc.get("channels") or [],
                env_doc=doc,
            )
            top = ranked[0] if ranked else None
            if top is not None and int(top.get("score") or 0) < 10:
                SLog.i(TAG, f"skip tag case={case_id}: account match too weak score={top.get('score')}")
                return None
        if not top:
            return None
        provider, gate = resolve_regression_provider(provider_id or None)
        if provider is None:
            SLog.i(TAG, f"skip tag case={case_id}: {gate.get('reason')}")
            return None
        existing = [str(t).strip() for t in (top.get("tags") or []) if str(t).strip()]
        catalog = _pool_tag_catalog(accounts)
        user = {
            "account": account_label(top),
            "account_tags": existing,
            "pool_tags": catalog,
            "case_id": case_id,
            "case_name": case_name,
            "preconditions": preconditions,
            "status": status,
            "summary": (summary or "")[:400],
        }
        from server.services.ai import dispatch_log as dispatch

        tok = dispatch.bind(
            job="account-tag",
            skill="account-tag",
            role="test-engineer",
        )
        try:
            raw, meta = call_chat_text(
                provider=provider,
                messages=[
                    {"role": "system", "content": _TAG_SYSTEM},
                    {"role": "user", "content": str(user)},
                ],
                temperature=0.1,
                max_tokens=256,
                timeout_sec=30,
            )
        finally:
            dispatch.reset(tok)
        if not isinstance(raw, dict):
            SLog.w(TAG, f"tag llm failed case={case_id} err={meta.get('error')!r}")
            return None
        reuse = set(existing) | set(catalog)
        add = _normalize_tags(raw.get("tags"), reuse=reuse)
        drop = _normalize_tags(raw.get("replaces"), limit=12, max_chars=40, reuse=reuse)
        next_tags = apply_tag_update(existing, add, drop)
        if next_tags == existing:
            return None
        removed = [t for t in existing if t not in next_tags]
        added = [t for t in next_tags if t not in existing]
        next_rows = []
        updated = None
        for row in accounts:
            if str(row.get("id")) != str(top.get("id")):
                next_rows.append(row)
                continue
            row = dict(row)
            row["tags"] = next_tags
            updated = row
            next_rows.append(row)
        if not updated:
            return None
        from server.models.project import Project

        project = db.query(Project).filter(Project.id == app.project_id).first()
        if not project:
            return None
        project.env = save_test_accounts(doc, next_rows)
        flag_modified(project, "env")
        db.commit()
        SLog.i(
            TAG,
            f"tagged account={account_ident(updated)} +{added} -{removed} "
            f"case={case_id} reason={str(raw.get('reason') or '')[:80]!r}",
        )
        return {
            "account_id": updated.get("id"),
            "account": account_label(updated),
            "tags": updated.get("tags"),
            "added": added,
            "removed": removed,
            "reason": str(raw.get("reason") or "").strip(),
        }
