# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""登录凭证统一出入口。来源由环境解释决定，不由当前步模型挑选。"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from script.log import SLog

TAG = "ResourceGateway"

FIELD_TO_SLOT = {
    "sms_code": "otp",
    "phone": "phone",
    "password": "password",
}

SLOT_TO_KNOWLEDGE = {
    "otp": "identity.otp",
    "phone": "identity.phone",
    "password": "identity.password",
}

_PHONE_RE = re.compile(r"\d{8,13}")
_META_HOSTS = {"169.254.169.254", "metadata.google.internal"}


@dataclass
class SecretHit:
    value: str = ""
    source: str = "none"
    field: str = ""
    slot: str = ""
    need_hitl: bool = False
    detail: str = ""

    def as_public(self) -> dict[str, str]:
        return {
            "source": self.source,
            "field": self.field,
            "slot": self.slot,
            "need_hitl": "yes" if self.need_hitl else "no",
            "has_value": "yes" if self.value else "no",
            "detail": (self.detail or "")[:120],
        }


def _phone_ok(phone: str) -> str:
    p = re.sub(r"\s+", "", str(phone or ""))
    return p if _PHONE_RE.fullmatch(p) else ""


def _account_from_ctx(ctx) -> dict[str, Any]:
    picked = getattr(ctx, "picked_account", None) or {}
    return picked if isinstance(picked, dict) else {}


def _env_key(ctx) -> str:
    snap = getattr(ctx, "resource_env", None) or {}
    if isinstance(snap, dict) and snap.get("env_key"):
        return str(snap.get("env_key") or "")
    profile = str(getattr(ctx, "env_profile", "") or "").strip()
    if profile:
        return profile
    acc = _account_from_ctx(ctx)
    return str(acc.get("env") or "").strip()


def _secrets_for(ctx, slot: str) -> dict[str, Any]:
    snap = getattr(ctx, "resource_env", None) or {}
    secrets = snap.get("secrets") if isinstance(snap, dict) else None
    if not isinstance(secrets, dict):
        secrets = {}
    block = secrets.get(slot) if isinstance(secrets.get(slot), dict) else {}
    return block if isinstance(block, dict) else {}


def _account_fixed_otp(acc: dict) -> str:
    from server.services.account_issue_service import extract_account_sms

    return str(acc.get("sms_code") or extract_account_sms(str(acc.get("note") or "")) or "").strip()


def _knowledge_value(ctx, slot: str) -> str:
    from server.services.knowledge_situation import lookup_bind_value

    kb_slot = SLOT_TO_KNOWLEDGE.get(slot) or ""
    if not kb_slot:
        return ""
    surface = "web"
    try:
        from server.services.runtime.playwright_hub import is_web_slot

        if not is_web_slot(getattr(ctx, "sn", ""), getattr(ctx, "platform", "")):
            surface = "app"
    except Exception:
        surface = "app"
    hit = lookup_bind_value(
        app_id=str(getattr(ctx, "app_id", "") or ""),
        slot=kb_slot,
        env=_env_key(ctx),
        surface=surface,
        backfill=True,
        app_version=str(getattr(ctx, "app_version", "") or ""),
    )
    return str((hit or {}).get("value") or "").strip()


def _http_post(url: str, body: dict[str, Any], header: str = "") -> str:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("adapter_url 只支持 http/https")
    host = parsed.hostname.lower()
    if host in _META_HOSTS:
        raise ValueError("adapter_url 主机不允许")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        for info in infos:
            ip = str(info[4][0] or "")
            if ip.startswith("169.254."):
                raise ValueError("adapter_url 主机不允许")
    except socket.gaierror as exc:
        raise ValueError(f"adapter_url 无法解析: {exc}") from exc
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    token = str(header or "").strip()
    if token:
        headers["Authorization"] = token if " " in token else f"Bearer {token}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        return ""
    return str(data.get("value") or data.get("code") or data.get("otp") or data.get("phone") or "").strip()


def _adapter_value(
    ctx,
    slot: str,
    spec: dict[str, Any],
    *,
    post: Optional[Callable[..., str]] = None,
) -> str:
    url = str(spec.get("adapter_url") or "").strip()
    if not url:
        return ""
    acc = _account_from_ctx(ctx)
    body = {
        "slot": slot,
        "env": _env_key(ctx),
        "app_id": str(getattr(ctx, "app_id", "") or ""),
        "account": {
            "id": str(acc.get("id") or ""),
            "phone": str(acc.get("phone") or ""),
            "tags": list(acc.get("tags") or []),
        },
    }
    fn = post or _http_post
    try:
        return str(fn(url, body, str(spec.get("adapter_header") or "")) or "").strip()
    except Exception as exc:
        SLog.w(TAG, f"adapter {slot} failed: {exc}")
        return ""


def _hit(field: str, slot: str, value: str, source: str, *, detail: str = "") -> SecretHit:
    val = str(value or "").strip()
    if slot == "phone":
        val = _phone_ok(val)
    return SecretHit(
        value=val,
        source=source if val else "none",
        field=field,
        slot=slot,
        need_hitl=not bool(val),
        detail=detail,
    )


def resolve_secret(
    ctx,
    field: str,
    *,
    post: Optional[Callable[..., str]] = None,
) -> SecretHit:
    """按环境解释取可填字段。模型看不到这条决策。"""
    field = str(field or "").strip().lower()
    slot = FIELD_TO_SLOT.get(field) or ""
    if not slot:
        return SecretHit(field=field, source="none", need_hitl=True, detail="未知字段")
    spec = _secrets_for(ctx, slot)
    mode = str(spec.get("mode") or "auto").strip().lower() or "auto"
    acc = _account_from_ctx(ctx)

    def account_val() -> str:
        if slot == "otp":
            return _account_fixed_otp(acc)
        if slot == "phone":
            return _phone_ok(str(acc.get("phone") or ""))
        if slot == "password":
            return str(acc.get("password") or "").strip()
        return ""

    def env_fixed() -> str:
        return str(spec.get("fixed") or "").strip() if slot == "otp" else ""

    def knowledge() -> str:
        return _knowledge_value(ctx, slot)

    def adapter() -> str:
        return _adapter_value(ctx, slot, spec, post=post)

    chain: list[tuple[str, Any]] = []
    if mode == "fixed":
        chain = [("account_fixed", account_val), ("env_fixed", env_fixed)]
    elif mode == "adapter":
        chain = [("adapter", adapter)]
    elif mode == "hitl":
        chain = []
    elif mode == "pool" and slot == "phone":
        chain = [("account_pool", account_val)]
    else:
        # auto
        chain = [
            ("account_fixed" if slot == "otp" else "account_pool", account_val),
            ("env_fixed", env_fixed),
            ("knowledge_bind", knowledge),
            ("adapter", adapter),
        ]

    for source, fn in chain:
        try:
            val = fn()
        except Exception as exc:
            SLog.d(TAG, f"{source} {slot} skip: {exc}")
            continue
        if str(val or "").strip():
            hit = _hit(field, slot, str(val), source, detail=mode)
            SLog.i(TAG, f"secret field={field} source={hit.source} mode={mode}")
            return hit

    return SecretHit(
        field=field,
        slot=slot,
        source="hitl",
        need_hitl=True,
        detail=f"mode={mode}",
    )
