# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""服务端全局设置 API。"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.services import system_settings_service as ss

router = APIRouter(prefix="/settings", tags=["Settings"])


class FeishuBotCreate(BaseModel):
    name: str = "飞书机器人"
    app_id: str = ""
    app_secret: str = ""


class FeishuBotUpdate(BaseModel):
    name: Optional[str] = None
    app_id: Optional[str] = None
    app_secret: str = ""
    clear_secret: bool = False


class FeishuBotSettingsUpdate(BaseModel):
    """兼容旧接口。"""
    app_id: str = ""
    app_secret: str = ""
    clear_secret: bool = False


@router.get("/feishu/bots")
def list_feishu_bots():
    return {"code": 200, "data": {"bots": ss.list_feishu_bots()}}


@router.post("/feishu/bots")
def create_feishu_bot(body: FeishuBotCreate):
    try:
        bot = ss.create_feishu_bot(
            name=body.name,
            app_id=body.app_id,
            app_secret=body.app_secret,
        )
        return {"code": 200, "msg": "机器人已添加", "data": bot}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/feishu/bots/{bot_id}")
def update_feishu_bot(bot_id: str, body: FeishuBotUpdate):
    try:
        bot = ss.update_feishu_bot(
            bot_id,
            name=body.name,
            app_id=body.app_id,
            app_secret=body.app_secret,
            clear_secret=body.clear_secret,
        )
        return {"code": 200, "msg": "已保存", "data": bot}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/feishu/bots/{bot_id}")
def delete_feishu_bot(bot_id: str):
    try:
        ss.delete_feishu_bot(bot_id)
        return {"code": 200, "msg": "已删除"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/feishu")
def get_feishu_settings():
    return {"code": 200, "data": ss.get_feishu_bot_settings()}


@router.put("/feishu")
def update_feishu_settings_legacy(body: FeishuBotSettingsUpdate):
    data = ss.save_feishu_bot_settings(
        app_id=body.app_id,
        app_secret=body.app_secret,
        clear_secret=body.clear_secret,
    )
    return {"code": 200, "msg": "已保存", "data": data}


class KnowledgeItem(BaseModel):
    id: str = ""
    title: str = ""
    content: str = ""
    category: str = "其他"
    tags: List[str] = Field(default_factory=list)
    app_ids: List[str] = Field(default_factory=list)
    enabled: bool = True


class KnowledgeSaveBody(BaseModel):
    items: List[KnowledgeItem] = Field(default_factory=list)


@router.get("/knowledge")
def get_testing_knowledge():
    return {"code": 200, "data": {"items": ss.list_testing_knowledge()}}


@router.put("/knowledge")
def save_testing_knowledge(body: KnowledgeSaveBody):
    items = ss.save_testing_knowledge([x.model_dump() for x in body.items])
    return {"code": 200, "msg": "已保存", "data": {"items": items}}


@router.get("/knowledge/match")
def match_testing_knowledge(q: str = "", app_id: str = ""):
    hits = ss.match_testing_knowledge(q, app_id=app_id or None)
    return {"code": 200, "data": {"items": hits}}


class FailureAnalyzeBody(BaseModel):
    app_id: str = ""
    case_name: str = ""
    command: str = ""
    step_text: str = ""
    action_text: str = ""
    expected_text: str = ""
    title: str = ""
    msg: str = ""
    method: str = ""
    role: str = "action"
    ok: bool = False
    assert_invalid: str = ""


class KnowledgeAppendBody(BaseModel):
    app_id: str
    item: KnowledgeItem


@router.post("/knowledge/analyze-failure")
def analyze_failure_for_knowledge(body: FailureAnalyzeBody):
    from server.services.failure_knowledge_service import analyze_step_failure

    data = analyze_step_failure(body.model_dump())
    return {"code": 200, "data": data}


@router.post("/knowledge/append")
def append_app_knowledge(body: KnowledgeAppendBody):
    from server.services.failure_knowledge_service import append_app_knowledge

    try:
        row = append_app_knowledge(body.app_id, body.item.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 200, "msg": "已写入应用知识库", "data": {"item": row}}


@router.get("/knowledge/app/{app_id}")
def list_app_knowledge(app_id: str):
    items = [
        x
        for x in ss.list_testing_knowledge()
        if str(app_id) in [str(a) for a in (x.get("app_ids") or [])]
    ]
    return {"code": 200, "data": {"items": items}}


class FigmaSettingsUpdate(BaseModel):
    access_token: str = ""
    clear_token: bool = False
    default_file_url: str = ""


@router.get("/figma")
def get_figma_settings():
    return {"code": 200, "data": ss.get_figma_settings()}


@router.put("/figma")
def save_figma_settings(body: FigmaSettingsUpdate):
    data = ss.save_figma_settings(
        access_token=body.access_token,
        clear_token=body.clear_token,
        default_file_url=body.default_file_url,
    )
    return {"code": 200, "msg": "已保存", "data": data}


class FigmaTestBody(BaseModel):
    access_token: str = ""


@router.post("/figma/test")
def test_figma_settings(body: FigmaTestBody):
    from server.services import figma_service as fs

    try:
        info = fs.test_figma_token(body.access_token or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"code": 200, "msg": "Token 有效", "data": info}
