# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""账号：邮箱注册 / 登录，以及内部账号密码登录。"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from typing import Any, Optional

from server.core.security import SecurityManager

# 内部账号密码入口。邮箱仍可自行注册。已有同名账号不会改密码。
LOCAL_ACCOUNTS = [
    {"username": "admin", "password": "MiniOrange@local", "name": "管理员"},
]

_ALG = "pbkdf2_sha256"
_ITER = 210000
_SESSION_TTL = 30 * 24 * 3600
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_USER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,31}$")
_DISPOSABLE = {
    "mailinator.com", "tempmail.com", "10minutemail.com", "guerrillamail.com",
    "trashmail.com", "yopmail.com", "temp-mail.org", "sharklasers.com",
}
_CODE_TTL = 600
_SEND_COOLDOWN = 60
_MAX_SEND_HOUR = 5
_MAX_ATTEMPTS = 5


def _now() -> int:
    return int(time.time())


def _hash_password(password: str, salt: str = "") -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITER)
    return f"{_ALG}${_ITER}${salt}${dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        _alg, it, salt, hx = str(stored or "").split("$", 3)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(dk.hex(), hx)
    except Exception:
        return False


def _root() -> dict[str, Any]:
    SecurityManager.load()
    raw = SecurityManager._config.get("auth")
    if not isinstance(raw, dict):
        raw = {}
        SecurityManager._config["auth"] = raw
    if not isinstance(raw.get("users"), list):
        raw["users"] = []
    if not isinstance(raw.get("sessions"), list):
        raw["sessions"] = []
    if not isinstance(raw.get("codes"), list):
        raw["codes"] = []
    return raw


def _save() -> None:
    SecurityManager.save()


def _purge(root: dict[str, Any]) -> None:
    now = _now()
    root["sessions"] = [
        s for s in (root.get("sessions") or [])
        if isinstance(s, dict) and int(s.get("expires_at") or 0) > now
    ]


def _norm_email(email: str) -> str:
    return str(email or "").strip().lower()


def _norm_username(value: str) -> str:
    return str(value or "").strip().lower()


def _norm_name(name: str) -> str:
    return str(name or "").strip()


def _valid_username(username: str) -> bool:
    return bool(_USER_RE.match(username))


def _valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email)) and len(email) <= 120


def _email_domain(email: str) -> str:
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def _hash_code(code: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{code}".encode("utf-8")).hexdigest()


def users() -> list[dict[str, Any]]:
    return [u for u in (_root().get("users") or []) if isinstance(u, dict)]


def needs_setup() -> bool:
    return not users()


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    email = _norm_email(user.get("email") or "")
    username = _norm_username(user.get("username") or "")
    name = _norm_name(user.get("name") or username or "")
    handle = username or name or (email.split("@")[0] if email else "")
    return {
        "user_id": str(user.get("id") or user.get("user_id") or ""),
        "email": email,
        "name": name or handle,
        "username": handle,
        "email_verified": user.get("email_verified") is not False,
    }


def _find_user(ident: str) -> Optional[dict[str, Any]]:
    email_key = _norm_email(ident)
    name_key = _norm_username(ident)
    if not email_key and not name_key:
        return None
    for user in users():
        if email_key and _norm_email(user.get("email") or "") == email_key:
            return user
        if name_key and _norm_username(user.get("username") or "") == name_key:
            return user
        if name_key and _norm_name(user.get("name") or "").lower() == name_key:
            return user
    return None


def _session_of(token: str) -> Optional[dict[str, Any]]:
    tok = str(token or "").strip()
    if not tok:
        return None
    root = _root()
    _purge(root)
    for row in root.get("sessions") or []:
        if isinstance(row, dict) and hmac.compare_digest(str(row.get("token") or ""), tok):
            return row
    return None


def _issue_session(user: dict[str, Any]) -> dict[str, Any]:
    root = _root()
    _purge(root)
    token = secrets.token_hex(24)
    pub = _public_user(user)
    sess = {
        "token": token,
        "user_id": pub["user_id"],
        "email": pub["email"],
        "name": pub["name"],
        "username": pub["username"],
        "email_verified": pub.get("email_verified") is not False,
        "expires_at": _now() + _SESSION_TTL,
    }
    root["sessions"] = [s for s in (root.get("sessions") or []) if s.get("user_id") != pub["user_id"]]
    root["sessions"].append(sess)
    _save()
    return {
        **pub,
        "token": token,
        "ws_token": str(SecurityManager.get_token() or ""),
        "expires_at": sess["expires_at"],
    }


def require_session(token: str = "") -> dict[str, Any]:
    sess = _session_of(token)
    if not sess:
        raise PermissionError("请先登录")
    return sess


def status(token: str = "") -> dict[str, Any]:
    sess = _session_of(token)
    pub = _public_user(sess or {}) if sess else {"user_id": "", "email": "", "name": "", "username": ""}
    from server.services.mail_service import mail_ready

    ws_token = ""
    if sess:
        ws_token = str(SecurityManager.get_token() or "")
    return {
        "logged_in": bool(sess),
        "needs_setup": not any(_norm_email(u.get("email") or "") for u in users()),
        "mail_configured": mail_ready(),
        "ws_token": ws_token,
        **pub,
    }


def _purge_codes(root: dict[str, Any]) -> None:
    now = _now()
    root["codes"] = [
        c for c in (root.get("codes") or [])
        if isinstance(c, dict) and int(c.get("expires_at") or 0) > now
    ]


def _codes_for(email: str, purpose: str) -> list[dict[str, Any]]:
    em = _norm_email(email)
    return [
        c for c in (_root().get("codes") or [])
        if isinstance(c, dict)
        and _norm_email(c.get("email") or "") == em
        and str(c.get("purpose") or "") == purpose
    ]


def send_code(email: str, purpose: str = "register") -> dict[str, Any]:
    from server.services.mail_service import mail_ready, send_mail

    em = _norm_email(email)
    purpose = str(purpose or "register").strip() or "register"
    if purpose not in ("register",):
        raise ValueError("不支持的验证码用途")
    if not _valid_email(em):
        raise ValueError("请填写有效邮箱")
    if _email_domain(em) in _DISPOSABLE:
        raise ValueError("请用常用邮箱，不要用临时邮箱")
    if purpose == "register" and any(_norm_email(u.get("email") or "") == em for u in users()):
        raise ValueError("这个邮箱已经注册")
    if not mail_ready():
        raise RuntimeError("还没有配置发信邮箱。到设置 → 密钥配置 → 发信邮箱填 SMTP。")
    now = _now()
    recent = [c for c in _codes_for(em, purpose) if now - int(c.get("sent_at") or 0) < 3600]
    last = max((int(c.get("sent_at") or 0) for c in recent), default=0)
    if last and now - last < _SEND_COOLDOWN:
        raise ValueError(f"请 { _SEND_COOLDOWN - (now - last) } 秒后再发")
    if len(recent) >= _MAX_SEND_HOUR:
        raise ValueError("这个邮箱一小时内发得太勤，稍后再试")
    code = f"{secrets.randbelow(1000000):06d}"
    salt = secrets.token_hex(8)
    root = _root()
    _purge_codes(root)
    root["codes"] = [
        c for c in (root.get("codes") or [])
        if not (isinstance(c, dict) and _norm_email(c.get("email") or "") == em and c.get("purpose") == purpose)
    ]
    root["codes"].append({
        "email": em,
        "purpose": purpose,
        "salt": salt,
        "code_hash": _hash_code(code, salt),
        "sent_at": now,
        "expires_at": now + _CODE_TTL,
        "attempts": 0,
    })
    _save()
    send_mail(
        to=em,
        subject="MiniOrange 邮箱验证码",
        body=(
            f"你的验证码是 {code}，10 分钟内有效。\n"
            "如果不是你在注册 MiniOrange，忽略这封信即可。\n"
        ),
    )
    return {"email": em, "ttl_sec": _CODE_TTL, "resend_sec": _SEND_COOLDOWN}


def _consume_code(email: str, purpose: str, code: str) -> None:
    em = _norm_email(email)
    raw = str(code or "").strip()
    if not re.fullmatch(r"\d{6}", raw):
        raise ValueError("请填写 6 位验证码")
    root = _root()
    _purge_codes(root)
    idx = next(
        (
            i for i, c in enumerate(root.get("codes") or [])
            if isinstance(c, dict)
            and _norm_email(c.get("email") or "") == em
            and c.get("purpose") == purpose
        ),
        -1,
    )
    if idx < 0:
        raise ValueError("请先获取验证码")
    row = root["codes"][idx]
    if int(row.get("attempts") or 0) >= _MAX_ATTEMPTS:
        root["codes"].pop(idx)
        _save()
        raise ValueError("验证码试错太多次，请重新获取")
    expected = str(row.get("code_hash") or "")
    got = _hash_code(raw, str(row.get("salt") or ""))
    if not hmac.compare_digest(expected, got):
        row["attempts"] = int(row.get("attempts") or 0) + 1
        _save()
        raise ValueError("验证码不对")
    root["codes"].pop(idx)
    _save()


def register(email: str = "", password: str = "", name: str = "", username: str = "", code: str = "") -> dict[str, Any]:
    from server.services.mail_service import mail_ready

    em = _norm_email(email)
    pwd = str(password or "")
    display = _norm_name(name) or (em.split("@")[0] if em else "")
    if not _valid_email(em):
        raise ValueError("请填写有效邮箱")
    if _email_domain(em) in _DISPOSABLE:
        raise ValueError("请用常用邮箱，不要用临时邮箱")
    if len(pwd) < 8:
        raise ValueError("密码至少 8 位")
    if len(display) > 32:
        raise ValueError("名称最多 32 个字符")
    if any(_norm_email(u.get("email") or "") == em for u in users()):
        raise ValueError("这个邮箱已经注册")
    need_code = any(_norm_email(u.get("email") or "") for u in users()) or mail_ready()
    if need_code:
        _consume_code(em, "register", code)
    uname = _norm_username(username) or _norm_username(em.split("@")[0])
    if not _valid_username(uname):
        uname = f"u{secrets.token_hex(3)}"
    base = uname
    n = 0
    while any(_norm_username(u.get("username") or "") == uname for u in users()):
        n += 1
        uname = f"{base}{n}"
    root = _root()
    user = {
        "id": secrets.token_hex(8),
        "email": em,
        "username": uname,
        "name": display,
        "password_hash": _hash_password(pwd),
        "email_verified": bool(need_code or str(code or "").strip()),
        "created_at": _now(),
    }
    root["users"] = [*(root.get("users") or []), user]
    _save()
    return _issue_session(user)


def login(email: str = "", password: str = "", username: str = "") -> dict[str, Any]:
    ensure_seed_users()
    ident = _norm_username(username) or _norm_email(email) or _norm_name(username or email)
    pwd = str(password or "")
    hit = _find_user(ident)
    if not hit or not _verify_password(pwd, str(hit.get("password_hash") or "")):
        raise PermissionError("账号或密码不对")
    return _issue_session(hit)


def create_local_user(
    username: str = "",
    password: str = "",
    name: str = "",
    email: str = "",
) -> dict[str, Any]:
    uname = _norm_username(username or email)
    pwd = str(password or "")
    display = _norm_name(name) or uname
    em = _norm_email(email)
    if em and not _valid_username(uname):
        uname = _norm_username(em.split("@")[0])
    if not _valid_username(uname):
        raise ValueError("账号用 2–32 位字母开头，可含数字、点、下划线或短横线")
    if em and not _valid_email(em):
        raise ValueError("邮箱格式不对")
    if len(pwd) < 8:
        raise ValueError("密码至少 8 位")
    if len(display) > 32:
        raise ValueError("名称最多 32 个字符")
    if any(_norm_username(u.get("username") or "") == uname for u in users()):
        raise ValueError("这个账号已经存在")
    if em and any(_norm_email(u.get("email") or "") == em for u in users()):
        raise ValueError("这个邮箱已经占用")
    root = _root()
    user = {
        "id": secrets.token_hex(8),
        "email": em,
        "username": uname,
        "name": display,
        "password_hash": _hash_password(pwd),
        "email_verified": bool(em),
        "created_at": _now(),
    }
    root["users"] = [*(root.get("users") or []), user]
    _save()
    return _public_user(user)


def ensure_seed_users() -> None:
    have_names = {_norm_username(u.get("username") or "") for u in users()}
    have_emails = {_norm_email(u.get("email") or "") for u in users() if _norm_email(u.get("email") or "")}
    for row in LOCAL_ACCOUNTS:
        if not isinstance(row, dict):
            continue
        uname = _norm_username(row.get("username") or "")
        em = _norm_email(row.get("email") or "")
        if uname and uname in have_names:
            continue
        if em and em in have_emails:
            continue
        created = create_local_user(
            username=str(row.get("username") or ""),
            password=str(row.get("password") or ""),
            name=str(row.get("name") or ""),
            email=str(row.get("email") or ""),
        )
        have_names.add(_norm_username(created.get("username") or ""))
        if created.get("email"):
            have_emails.add(_norm_email(created.get("email") or ""))


def list_accounts() -> list[dict[str, Any]]:
    ensure_seed_users()
    return [_public_user(u) for u in users()]


def delete_account(user_id: str, *, actor_id: str = "") -> None:
    uid = str(user_id or "").strip()
    if not uid:
        raise ValueError("缺少账号")
    if actor_id and uid == str(actor_id):
        raise ValueError("不能删当前登录的账号")
    rows = users()
    if len(rows) <= 1:
        raise ValueError("至少保留一个账号")
    if not any(str(u.get("id") or u.get("user_id") or "") == uid for u in rows):
        raise ValueError("没有这个账号")
    root = _root()
    root["users"] = [
        u for u in (root.get("users") or [])
        if not (isinstance(u, dict) and str(u.get("id") or u.get("user_id") or "") == uid)
    ]
    root["sessions"] = [
        s for s in (root.get("sessions") or [])
        if not (isinstance(s, dict) and str(s.get("user_id") or "") == uid)
    ]
    _save()


def logout(token: str = "") -> None:
    tok = str(token or "").strip()
    if not tok:
        return
    root = _root()
    root["sessions"] = [
        s for s in (root.get("sessions") or [])
        if not (isinstance(s, dict) and hmac.compare_digest(str(s.get("token") or ""), tok))
    ]
    _save()
