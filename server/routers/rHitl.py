# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""HITL（Human-In-The-Loop）HTTP API。

桌面端配合：
  - 通过 WS 帧 type="hitl_request" 接到弹框请求（由 HitlExecutor 推送）
  - 通过 GET /hitl/pending 主动拉取所有待处理项（断线重连后用）
  - 通过 POST /hitl/reply 投递人工答案
  - 通过 POST /hitl/skip 主动跳过当前请求
  - 通过 POST /hitl/revoke (admin) 终止某条请求（一般给运维 / 调试）

注意：本路由不依赖鉴权（与其它 router 保持一致）；如需鉴权后期通过 Depends 注入。
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from script.log import SLog

from server.services.regression.hitl import (
    HitlReply,
    get_session_manager,
    get_transport,
)
from server.services.regression.hitl.schemas import VALID_HITL_KINDS

router = APIRouter(prefix="/hitl", tags=["HITL"])

TAG = "HitlRouter"


# ---------- 请求体 ----------


class ReplyBody(BaseModel):
    """POST /hitl/reply 的请求体。"""

    request_id: str = Field(..., description="HitlRequest.request_id")
    kind: str = Field(..., description=f"必须 ∈ {sorted(VALID_HITL_KINDS)}")
    answer: Any = Field(None, description="见 HitlReply.answer 文档")
    skipped: bool = Field(False, description="True=用户主动跳过；answer 可忽略")
    extra: dict[str, Any] = Field(default_factory=dict)
    replied_by: Optional[str] = Field(None, description="操作员标识")


class RevokeBody(BaseModel):
    request_id: str
    reason: str = "revoked_by_user"


# ---------- 接口 ----------


@router.get("/pending")
def list_pending() -> dict[str, Any]:
    """返回当前所有 pending HITL 请求（用于桌面断线重连恢复弹框）。"""
    manager = get_session_manager()
    items = manager.list_pending()
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "count": len(items),
            "items": [it.model_dump(mode="json") for it in items],
        },
    }


@router.get("/pending/{request_id}")
def get_pending(request_id: str) -> dict[str, Any]:
    """单条详情（含 body / options / constraints 等渲染数据）。"""
    manager = get_session_manager()
    req = manager.get_request(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"no pending request {request_id}")
    return {"code": 0, "msg": "ok", "data": req.to_payload()}


@router.post("/reply")
def submit_reply(body: ReplyBody) -> dict[str, Any]:
    """桌面端投递人工答复。"""
    if body.kind not in VALID_HITL_KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind={body.kind}")
    reply = HitlReply(
        request_id=body.request_id,
        kind=body.kind,
        answer=body.answer,
        skipped=body.skipped,
        extra=body.extra or {},
        replied_by=body.replied_by,
    )
    ok = get_session_manager().submit_reply(reply)
    if not ok:
        # 可能是超时已被 executor 收走了
        raise HTTPException(
            status_code=410,
            detail=f"hitl request {body.request_id} no longer pending (timeout or already replied)",
        )
    SLog.i(TAG, f"reply ok request={body.request_id} kind={body.kind} skipped={body.skipped}")
    return {"code": 0, "msg": "ok", "data": {"request_id": body.request_id}}


@router.post("/skip")
def submit_skip(body: ReplyBody) -> dict[str, Any]:
    """语法糖：用户跳过当前请求（等价于 reply with skipped=True）。"""
    body.skipped = True
    body.answer = None
    return submit_reply(body)


@router.post("/revoke")
def revoke(body: RevokeBody) -> dict[str, Any]:
    """运维/调试：终止某条 HITL 请求（executor 端会收到 None reply → BLOCKED）。"""
    manager = get_session_manager()
    ok = manager.revoke(body.request_id, reason=body.reason)
    if not ok:
        raise HTTPException(status_code=404, detail=f"no pending request {body.request_id}")
    # 通知前端把弹框关掉
    try:
        get_transport().push_revoke(body.request_id, body.reason)
    except Exception as exc:
        SLog.w(TAG, f"push_revoke transport failed: {exc}")
    return {"code": 0, "msg": "ok", "data": {"request_id": body.request_id}}


@router.get("/health")
def health() -> dict[str, Any]:
    """便于桌面端探测 HITL 通道在线状态。"""
    manager = get_session_manager()
    return {
        "code": 0,
        "msg": "ok",
        "data": {
            "pending_count": manager.pending_count(),
            "transport": type(get_transport()).__name__,
        },
    }
