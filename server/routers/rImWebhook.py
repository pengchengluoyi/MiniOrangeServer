# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""IM 机器人入站：飞书事件订阅。不走登录。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

router = APIRouter(prefix="/webhooks", tags=["IM Webhooks"])


@router.post("/feishu")
async def feishu_event(request: Request):
    try:
        payload = await request.json()
    except Exception as e:
        raise HTTPException(status_code=400, detail="请求体不是 JSON") from e
    from server.services.im_bot_service import accept_feishu_event, record_im_inbound

    record_im_inbound({"source": "http", "received": True, "has_encrypt": bool((payload or {}).get("encrypt"))})
    try:
        result = accept_feishu_event(payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if "challenge" in result:
        return JSONResponse({"challenge": result.get("challenge")})
    return JSONResponse(result)


@router.get("/feishu")
def feishu_event_probe():
    return {"ok": True, "path": "/webhooks/feishu"}
