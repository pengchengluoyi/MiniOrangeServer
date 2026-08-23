# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""服务端全局设置 API。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, joinedload

from server.core.database import get_db
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


class RobotIntegrationCreate(BaseModel):
    platform: str = "lark"
    name: str = ""
    credentials: Dict[str, Any] = Field(default_factory=dict)


class RobotIntegrationUpdate(BaseModel):
    platform: Optional[str] = None
    name: Optional[str] = None
    credentials: Dict[str, Any] = Field(default_factory=dict)
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


@router.get("/robots/bots")
def list_robot_bots():
    return {"code": 200, "data": {"bots": ss.list_robot_integrations()}}


@router.post("/robots/bots")
def create_robot_bot(body: RobotIntegrationCreate):
    try:
        row = ss.create_robot_integration(
            platform=body.platform,
            name=body.name,
            credentials=body.credentials,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 200, "msg": "机器人已添加", "data": row}


@router.put("/robots/bots/{bot_id}")
def update_robot_bot(bot_id: str, body: RobotIntegrationUpdate):
    try:
        row = ss.update_robot_integration(
            bot_id,
            platform=body.platform,
            name=body.name,
            credentials=body.credentials,
            clear_secret=body.clear_secret,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 200, "msg": "已保存", "data": row}


@router.delete("/robots/bots/{bot_id}")
def delete_robot_bot(bot_id: str):
    try:
        ss.delete_robot_integration(bot_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 200, "msg": "已删除"}


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
    model_config = ConfigDict(extra="allow")
    id: str = ""
    title: str = ""
    content: str = ""
    category: str = "其他"
    tags: List[str] = Field(default_factory=list)
    app_ids: List[str] = Field(default_factory=list)
    enabled: bool = True
    source: str = "manual"
    review_status: str = "approved"


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


class KnowledgeUpsertBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str = ""
    title: str
    content: str
    category: str = "其他"
    tags: List[str] = Field(default_factory=list)
    app_ids: List[str] = Field(default_factory=list)
    enabled: bool = True
    source: str = ""
    review_status: str = ""


@router.put("/knowledge/{kid}")
def upsert_knowledge_item(kid: str, body: KnowledgeUpsertBody):
    """新建或更新单条知识条目（kid 若与 body.id 不一致以 kid 为准）。"""
    item = body.model_dump()
    item["id"] = "" if kid.strip() == "new" else (kid.strip() or item.get("id") or "")
    try:
        row = ss.upsert_knowledge_item(item)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 200, "msg": "已保存", "data": {"item": row}}


@router.delete("/knowledge/{kid}")
def delete_knowledge_item(kid: str):
    """删除单条知识条目。"""
    found = ss.delete_knowledge_item(kid.strip())
    if not found:
        raise HTTPException(status_code=404, detail=f"未找到知识条目：{kid}")
    return {"code": 200, "msg": "已删除"}


class KnowledgeReviewBody(BaseModel):
    action: str
    title: str = ""
    content: str = ""
    category: str = ""
    tags: List[str] = Field(default_factory=list)
    origin_task_id: str = ""


@router.post("/knowledge/{kid}/review")
def review_knowledge_item(kid: str, body: KnowledgeReviewBody):
    """待审核知识：approve 后才进入匹配；reject 删除。"""
    updates: dict = {}
    if body.title.strip():
        updates["title"] = body.title.strip()
    if body.content.strip():
        updates["content"] = body.content.strip()
    if body.category.strip():
        updates["category"] = body.category.strip()
    if body.tags:
        updates["tags"] = body.tags
    try:
        row = ss.review_knowledge_item(kid.strip(), action=body.action, updates=updates or None)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"未找到知识条目：{kid}") from e
    origin = str(row.get("origin_task_id") or body.origin_task_id or "").strip()
    if origin:
        try:
            from server.services.regression import task_store

            st = "rejected" if str(body.action or "").strip().lower() == "reject" else "approved"
            task_store.mark_knowledge_proposal(origin, kid.strip(), st)
        except Exception as exc:
            from script.log import SLog
            SLog.w("Settings", f"mark_knowledge_proposal failed {origin} {kid}: {exc}")
    return {"code": 200, "msg": "已审核", "data": {"item": row}}


class KnowledgeAutoReviewBody(BaseModel):
    app_id: str = ""


@router.post("/knowledge/auto-review")
def auto_review_knowledge(body: KnowledgeAutoReviewBody):
    """对 pending 知识跑知识审核员机审。高置信自动过/驳，其余留人工。"""
    from server.services.knowledge_review_service import review_pending

    data = review_pending(app_id=body.app_id)
    return {"code": 200, "msg": "机审完成", "data": data}


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


class MailSettingsUpdate(BaseModel):
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    clear_password: bool = False
    from_email: str = ""
    from_name: str = ""
    use_tls: bool = True


class MailTestBody(BaseModel):
    to: str = ""


@router.get("/mail")
def get_mail_settings():
    return {"code": 200, "data": ss.get_mail_settings()}


@router.put("/mail")
def save_mail_settings(body: MailSettingsUpdate):
    data = ss.save_mail_settings(
        host=body.host,
        port=body.port,
        username=body.username,
        password=body.password,
        clear_password=body.clear_password,
        from_email=body.from_email,
        from_name=body.from_name,
        use_tls=body.use_tls,
    )
    return {"code": 200, "msg": "已保存", "data": data}


@router.post("/mail/test")
def test_mail_settings(body: MailTestBody):
    from server.services.mail_service import test_mail

    try:
        data = test_mail(body.to)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 200, "msg": "测试信已发出", "data": data}


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


class AIProviderSaveBody(BaseModel):
    name: str = ""
    api_type: str = ""
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    enabled: bool = True
    clear_key: bool = False
    set_default: bool = False
    plan_compress_ratio: float = 3.0
    case_execution_use: bool = False


class AIUsageSaveBody(BaseModel):
    copilot_enabled: bool = False
    case_execution_enabled: bool = False
    case_execution_provider_id: str = ""
    mode: str = "local_first"
    plan_compress_image: bool = True


@router.get("/ai/providers")
def list_ai_providers():
    return {"code": 200, "data": ss.list_ai_provider_settings()}


@router.put("/ai/providers/{provider_id}")
def save_ai_provider(provider_id: str, body: AIProviderSaveBody):
    try:
        row = ss.save_ai_provider_settings(
            provider_id,
            name=body.name,
            api_type=body.api_type,
            api_key=body.api_key,
            base_url=body.base_url,
            model=body.model,
            enabled=body.enabled,
            clear_key=body.clear_key,
            set_default=body.set_default,
            plan_compress_ratio=body.plan_compress_ratio,
            case_execution_use=body.case_execution_use,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 200, "msg": "已保存", "data": row}


@router.delete("/ai/providers/{provider_id}")
def delete_ai_provider(provider_id: str):
    ss.delete_ai_provider_settings(provider_id)
    return {"code": 200, "msg": "已删除"}


@router.put("/ai/usage")
def save_ai_usage(body: AIUsageSaveBody):
    data = ss.save_ai_usage_settings(
        copilot_enabled=body.copilot_enabled,
        case_execution_enabled=body.case_execution_enabled,
        case_execution_provider_id=body.case_execution_provider_id,
        mode=body.mode,
        plan_compress_image=body.plan_compress_image,
    )
    return {"code": 200, "msg": "已保存", "data": data}


@router.get("/ai/plan-prompt")
def get_ai_plan_prompt():
    from server.services.ai.plan.prompt import (
        AI_CASE_PLAN_SYSTEM_PROMPT,
        AI_PLAN_SYSTEM_PROMPT,
        AI_PLAN_USER_PROMPT_TEMPLATE,
    )

    return {
        "code": 200,
        "data": {
            "system": AI_PLAN_SYSTEM_PROMPT,
            "case_system": AI_CASE_PLAN_SYSTEM_PROMPT,
            "user_template": AI_PLAN_USER_PROMPT_TEMPLATE,
        },
    }


class RoleChatBody(BaseModel):
    role_id: str = ""
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    explain_mode: bool = False


@router.get("/ai/roles")
def list_ai_roles():
    from server.services.ai.roles_catalog import list_roles

    return {"code": 200, "data": list_roles()}


@router.get("/ai/roles/{role_id}")
def get_ai_role(role_id: str):
    from server.services.ai.roles_catalog import get_role

    row = get_role(role_id)
    if not row:
        raise HTTPException(status_code=404, detail=f"未知角色：{role_id}")
    return {"code": 200, "data": row}


class RoleRouteBody(BaseModel):
    scene: str = "case_exec_agent"
    state: Dict[str, Any] = Field(default_factory=dict)
    use_llm: bool = False


@router.post("/ai/roles/route")
def route_ai_role(body: RoleRouteBody):
    from server.services.ai.role_router import route_playbook, route_with_llm

    if body.use_llm:
        data = route_with_llm(scene=body.scene, state=body.state)
    else:
        data = route_playbook(body.scene)
    return {"code": 200, "data": data}


class RolePromptBody(BaseModel):
    system_prompt: str = ""
    reset: bool = False


@router.put("/ai/roles/{role_id}/prompt")
def save_ai_role_prompt(role_id: str, body: RolePromptBody):
    try:
        data = ss.save_role_prompt(role_id, system_prompt=body.system_prompt, reset=body.reset)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 200, "msg": "已保存", "data": data}


@router.post("/ai/roles/chat")
def chat_ai_role(body: RoleChatBody):
    from server.services.ai.roles_catalog import chat_with_role
    from server.services.ai import dispatch_log as dispatch

    tok = dispatch.bind(trigger="settings_chat", role=body.role_id, job="role_chat")
    try:
        data = chat_with_role(
            role_id=body.role_id,
            messages=body.messages,
            explain_mode=body.explain_mode,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        dispatch.reset(tok)
    return {"code": 200, "data": data}


@router.get("/dispatch")
def list_dispatch_calls(
    limit: int = 80,
    kind: str = "",
    role: str = "",
    trigger: str = "",
    app_id: str = "",
    pipeline_id: str = "",
):
    from server.services.ai.dispatch_log import list_calls

    rows = list_calls(limit=limit, kind=kind, role=role, trigger=trigger, app_id=app_id, pipeline_id=pipeline_id)
    return {"code": 200, "data": {"calls": rows, "total": len(rows)}}


@router.get("/dispatch/{call_id}")
def get_dispatch_call(call_id: str):
    from server.services.ai.dispatch_log import get_call

    row = get_call(call_id)
    if not row:
        raise HTTPException(status_code=404, detail="没有这条调度记录")
    return {"code": 200, "data": row}


@router.get("/skills")
def get_skills_catalog():
    from server.services.skills_registry import list_skills_catalog

    return {"code": 200, "data": list_skills_catalog()}


# ------------------------------
# 系统设置：ClawNode 日志存储路径等
# ------------------------------

class ClawnodeLogsDirBody(BaseModel):
    path: str = ""


@router.get("/system/clawnode/logs-dir")
def get_clawnode_logs_dir():
    from server.core.security import SecurityManager
    configured = SecurityManager.get_clawnode_logs_dir()
    # 也返回一个建议的默认值（前端可用于“恢复默认”）
    try:
        from pathlib import Path
        default = str((Path.home() / "Downloads" / "ClawNodeLogs").resolve())
    except Exception:
        default = ""
    effective = configured or default
    return {
        "code": 200,
        "data": {
            "configured": configured,
            "effective": effective,
            "default": default,
        }
    }


@router.put("/system/clawnode/logs-dir")
def set_clawnode_logs_dir(body: ClawnodeLogsDirBody):
    from server.core.security import SecurityManager
    SecurityManager.set_clawnode_logs_dir(body.path or "")
    return {"code": 200, "msg": "已保存", "data": {"configured": SecurityManager.get_clawnode_logs_dir()}}


# ------------------------------
# 集成插件
# ------------------------------

class PluginSaveBody(BaseModel):
    model_config = ConfigDict(extra="allow")
    enabled: Optional[bool] = None
    capabilities: Optional[Dict[str, Any]] = None
    wiki: Optional[Dict[str, Any]] = None
    notify: Optional[Dict[str, Any]] = None
    writeback: Optional[Dict[str, Any]] = None
    flow: Optional[Dict[str, Any]] = None
    templates: Optional[List[Dict[str, Any]]] = None
    bindings: Optional[List[Dict[str, Any]]] = None
    url: Optional[str] = None
    account: Optional[str] = None
    token: Optional[str] = None
    clear_token: bool = False
    access_token: Optional[str] = None
    default_file_url: Optional[str] = None
    chat: Optional[Dict[str, Any]] = None


class ZentaoTestBody(BaseModel):
    url: str = ""
    account: str = ""
    token: str = ""


class ZentaoTokenBody(BaseModel):
    url: str = ""
    account: str = ""
    password: str = ""


class ZentaoBugTestBody(BaseModel):
    project_id: str = ""
    product_id: str = ""
    template_id: str = ""
    title: str = ""


def _plugin_app_bindings(db: Session, plugin_id: str) -> List[Dict[str, Any]]:
    from server.models.project import Project
    from server.services import app_automation_service as aas

    bots = {str(b.get("id")): b for b in ss.list_feishu_bots()}
    rows: List[Dict[str, Any]] = []
    projects = db.query(Project).options(joinedload(Project.apps)).all()
    for project in projects:
        for app in project.apps or []:
            env = app.env if isinstance(app.env, dict) else {}
            if plugin_id == "feishu":
                feishu = env.get("feishu") if isinstance(env.get("feishu"), dict) else {}
                bot_id = str(feishu.get("bot_id") or "")
                rows.append(
                    {
                        "project_id": project.id,
                        "project_name": project.name,
                        "app_id": app.id,
                        "app_name": app.name,
                        "doc_url": feishu.get("doc_url") or "",
                        "bot_id": bot_id,
                        "bot_name": (bots.get(bot_id) or {}).get("name") or "",
                        "enabled": feishu.get("enabled", True) is not False,
                        "env_profile": feishu.get("env_profile") or "test",
                        "data_range": feishu.get("data_range") or "A1:O500",
                        "case_count": aas.count_qa_process_cases_from_env(env),
                    }
                )
            elif plugin_id == "figma":
                automation = env.get("automation") if isinstance(env.get("automation"), dict) else {}
                figma = automation.get("figma") if isinstance(automation.get("figma"), dict) else {}
                rows.append(
                    {
                        "project_id": project.id,
                        "project_name": project.name,
                        "app_id": app.id,
                        "app_name": app.name,
                        "file_url": figma.get("file_url") or "",
                        "file_key": figma.get("file_key") or "",
                        "last_sync_at": figma.get("last_sync_at") or "",
                    }
                )
    return rows


@router.get("/plugins")
def list_integration_plugins():
    return {"code": 200, "data": ss.list_integration_plugins()}


@router.get("/plugins/{plugin_id}")
def get_integration_plugin(plugin_id: str, db: Session = Depends(get_db)):
    try:
        data = ss.get_integration_plugin(plugin_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    if plugin_id in ("feishu", "figma"):
        data["bindings"] = _plugin_app_bindings(db, plugin_id)
    return {"code": 200, "data": data}


@router.put("/plugins/{plugin_id}")
def save_integration_plugin(plugin_id: str, body: PluginSaveBody, db: Session = Depends(get_db)):
    payload = body.model_dump(exclude_unset=True)
    try:
        data = ss.save_integration_plugin(plugin_id, payload)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if plugin_id in ("feishu", "figma"):
        data["bindings"] = _plugin_app_bindings(db, plugin_id)
    return {"code": 200, "msg": "已保存", "data": data}


class PluginChatBody(BaseModel):
    text: str = ""
    mode: str = ""
    history: List[Dict[str, Any]] = Field(default_factory=list)


@router.post("/plugins/feishu/listener/sync")
def sync_feishu_listener():
    from server.services.feishu_ws_listener import sync_feishu_event_listener

    return {"code": 200, "data": sync_feishu_event_listener()}


@router.post("/plugins/{plugin_id}/chat")
def chat_integration_plugin(plugin_id: str, body: PluginChatBody):
    from server.services.im_bot_service import IM_PLUGIN_IDS, reply_im_message

    if plugin_id not in IM_PLUGIN_IDS:
        raise HTTPException(status_code=400, detail="这个插件没有 IM 对话")
    try:
        data = reply_im_message(
            text=body.text,
            history=body.history,
            mode=body.mode if body.mode in ("dialogue", "defect") else "",
            plugin_id=plugin_id,
            require_enabled=False,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 200, "data": data}


@router.post("/plugins/zentao/test")
def test_zentao_plugin(body: ZentaoTestBody):
    try:
        info = ss.test_zentao_connection(url=body.url, account=body.account, token=body.token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 200, "msg": "已连通", "data": info}


@router.post("/plugins/zentao/token")
def fetch_zentao_plugin_token(body: ZentaoTokenBody):
    try:
        info = ss.fetch_zentao_token(url=body.url, account=body.account, password=body.password)
        plugin = ss.save_integration_plugin(
            "zentao",
            {
                "url": info.get("url") or "",
                "account": info.get("account") or "",
                "token": info.get("token") or "",
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "code": 200,
        "msg": "已获取并保存 Token",
        "data": {
            "ok": True,
            "url": info.get("url") or "",
            "account": info.get("account") or "",
            "has_token": True,
            "plugin": plugin,
        },
    }


@router.post("/plugins/zentao/bugs/test")
def test_zentao_plugin_bug(body: ZentaoBugTestBody):
    try:
        info = ss.create_zentao_test_bug(
            project_id=body.project_id,
            product_id=body.product_id,
            template_id=body.template_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"code": 200, "msg": "已在禅道建测试单", "data": info}
