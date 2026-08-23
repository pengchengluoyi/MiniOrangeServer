# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""账号：邮箱注册 / 登录，以及内部账号密码登录。"""
from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from server.services import auth_service as auth
from server.services import agent_session_store

router = APIRouter(prefix="/auth", tags=["Auth"])


class AccountBody(BaseModel):
    email: str = ""
    password: str = ""
    name: str = ""
    username: str = ""
    code: str = ""


class SendCodeBody(BaseModel):
    email: str = ""
    purpose: str = "register"


def _bearer(authorization: str = "") -> str:
    raw = str(authorization or "").strip()
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw


def _http_error(exc: Exception, fallback: int = 400) -> NoReturn:
    if isinstance(exc, PermissionError):
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=fallback, detail=str(exc)) from exc


@router.get("/status")
def auth_status(authorization: str = Header(default="")):
    return {"code": 200, "data": auth.status(_bearer(authorization))}


@router.post("/send-code")
def auth_send_code(body: SendCodeBody):
    try:
        data = auth.send_code(body.email, purpose=body.purpose)
    except Exception as e:
        _http_error(e)
    return {"code": 200, "msg": "验证码已发送", "data": data}


@router.post("/register")
def auth_register(body: AccountBody):
    try:
        data = auth.register(
            email=body.email,
            password=body.password,
            name=body.name,
            username=body.username,
            code=body.code,
        )
    except Exception as e:
        _http_error(e)
    return {"code": 200, "msg": "账号已创建", "data": data}


@router.get("/users")
def auth_list_users(authorization: str = Header(default="")):
    try:
        auth.require_session(_bearer(authorization))
        rows = auth.list_accounts()
    except Exception as e:
        _http_error(e)
    return {"code": 200, "data": {"users": rows}}


@router.post("/users")
def auth_create_user(body: AccountBody, authorization: str = Header(default="")):
    try:
        auth.require_session(_bearer(authorization))
        row = auth.create_local_user(
            username=body.username or body.email,
            password=body.password,
            name=body.name,
            email=body.email,
        )
    except Exception as e:
        _http_error(e)
    return {"code": 200, "msg": "账号已添加", "data": row}


@router.delete("/users/{user_id}")
def auth_delete_user(user_id: str, authorization: str = Header(default="")):
    try:
        sess = auth.require_session(_bearer(authorization))
        auth.delete_account(user_id, actor_id=str(sess.get("user_id") or ""))
    except Exception as e:
        _http_error(e)
    return {"code": 200, "msg": "已删除"}


class AgentSessionsBody(BaseModel):
    sessions: list[dict[str, Any]] = []


@router.get("/agent-sessions")
def auth_list_agent_sessions(authorization: str = Header(default="")):
    try:
        rows = agent_session_store.list_sessions(_bearer(authorization))
    except Exception as e:
        _http_error(e)
    return {"code": 200, "data": {"sessions": rows}}


@router.put("/agent-sessions")
def auth_save_agent_sessions(body: AgentSessionsBody, authorization: str = Header(default="")):
    try:
        rows = agent_session_store.save_sessions(_bearer(authorization), body.sessions)
    except Exception as e:
        _http_error(e)
    return {"code": 200, "data": {"sessions": rows}}


@router.post("/login")
def auth_login(body: AccountBody):
    try:
        data = auth.login(email=body.email, password=body.password, username=body.username)
    except Exception as e:
        _http_error(e)
    return {"code": 200, "msg": "登录成功", "data": data}


@router.post("/logout")
def auth_logout(authorization: str = Header(default="")):
    auth.logout(_bearer(authorization))
    return {"code": 200, "msg": "已退出"}
