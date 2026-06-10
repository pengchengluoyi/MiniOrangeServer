# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""应用自动化配置 API（Skills、图标目标、用例缓存）。"""
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from server.core.database import APP_DATA_DIR, get_db
from server.models.project import App
from server.services import app_automation_service as aas
from server.services import icon_target_service as its
from server.services.project_env import ENV_PROFILE_KEYS
from server.services.crawl_persistence import save_screenshot_file
from server.services import figma_service as fs
from server.services import figma_logic_service as fls

router = APIRouter(prefix="/app-automation", tags=["App Automation"])


class SkillsBlock(BaseModel):
    pre: List[str] = []
    post: List[str] = []


class AutomationSkills(BaseModel):
    default: SkillsBlock = SkillsBlock()
    devices: Dict[str, SkillsBlock] = {}


class IconTargetBody(BaseModel):
    id: str = ""
    name: str
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    image_url: str = ""
    aliases: List[str] = []
    note: str = ""
    component_uid: str = ""


class IconFromLocateBody(BaseModel):
    name: str = ""
    target_label: str = ""
    target_rect: Optional[Dict[str, Any]] = None
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    screenshot: str = ""
    aliases: List[str] = []
    note: str = "从执行回放定位导入"


class ExecutionEnvConfig(BaseModel):
    mode: str = "fixed"
    profile: str = "test"


class FigmaDesignConfig(BaseModel):
    file_url: str = ""
    file_key: str = ""
    last_sync_at: str = ""
    pages_summary: List[str] = []


class AutomationConfigUpdate(BaseModel):
    env_profile: str = "test"
    execution_env: Optional[ExecutionEnvConfig] = None
    skills: Optional[AutomationSkills] = None
    figma: Optional[FigmaDesignConfig] = None


def _get_app(db: Session, app_id: str) -> App:
    app = db.query(App).options(joinedload(App.project)).filter(App.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    return app


@router.get("/config/{app_id}")
def get_automation_config(app_id: str, db: Session = Depends(get_db)):
    app = _get_app(db, app_id)
    cfg = aas.get_automation_config(app)
    pkg = aas.package_for_app(app)
    icon_page = its.list_icon_targets(db, app_id, page=1, page_size=1)
    cache = aas.get_feishu_cases_cache(app) or {}
    return {
        "code": 200,
        "data": {
            "app_id": app.id,
            "app_name": app.name,
            "project_name": app.project.name if app.project else "",
            "env_profile": cfg.get("env_profile"),
            "env_profiles": list(ENV_PROFILE_KEYS),
            "package": pkg,
            "automation": cfg,
            "feishu_cases_cache": cache,
            "stats": {
                "icon_targets": icon_page.get("total", 0),
                "feishu_cases": len(cache.get("cases") or []),
            },
        },
    }


class FigmaSyncBody(BaseModel):
    file_url: str = ""
    file_key: str = ""


@router.post("/config/{app_id}/figma/sync")
def sync_app_figma(app_id: str, body: FigmaSyncBody, db: Session = Depends(get_db)):
    app = _get_app(db, app_id)
    try:
        synced = fs.sync_figma_file(
            file_url=body.file_url,
            file_key=body.file_key,
            depth=8,
            include_raw_document=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    cfg = aas.save_automation_config(app, {"figma": synced})
    db.commit()

    login_icons: Dict[str, Any] = {}
    try:
        from server.core.database import SessionLocal
        from server.services.figma_icon_service import seed_login_icons_from_figma

        with SessionLocal() as seed_db:
            app_row = seed_db.query(App).filter(App.id == app.id).first()
            if app_row:
                login_icons = seed_login_icons_from_figma(
                    seed_db,
                    app_row,
                    document=synced.get("raw_document"),
                )
                seed_db.commit()
    except Exception as e:
        login_icons = {"msg": str(e)}
    return {
        "code": 200,
        "msg": "设计稿已同步",
        "data": {
            "figma": cfg.get("figma") or synced,
            "page_count": synced.get("page_count", 0),
            "frame_count": synced.get("frame_count", 0),
            "logic_pages": len((synced.get("logic") or {}).get("pages") or []),
            "login_icons": login_icons,
        },
    }


class FigmaApplyLogicBody(BaseModel):
    file_url: str = ""
    file_key: str = ""
    write_knowledge: bool = True
    write_graph: bool = True


@router.post("/config/{app_id}/figma/apply-logic")
def apply_figma_app_logic(app_id: str, body: FigmaApplyLogicBody, db: Session = Depends(get_db)):
    """从 Figma 学习应用逻辑：同步设计稿 → 写入图谱节点 + 应用知识库。"""
    app = _get_app(db, app_id)
    try:
        result = fls.sync_and_apply_figma_logic(
            app,
            db,
            file_url=body.file_url,
            file_key=body.file_key,
            write_knowledge=body.write_knowledge,
            write_graph=body.write_graph,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    db.commit()
    return {
        "code": 200,
        "msg": "已从 Figma 学习应用逻辑",
        "data": result,
    }


@router.put("/config/{app_id}")
def update_automation_config(app_id: str, body: AutomationConfigUpdate, db: Session = Depends(get_db)):
    app = _get_app(db, app_id)
    payload: Dict[str, Any] = {"env_profile": body.env_profile}
    if body.execution_env is not None:
        payload["execution_env"] = body.execution_env.model_dump()
    if body.skills is not None:
        payload["skills"] = body.skills.model_dump()
    if body.figma is not None:
        payload["figma"] = body.figma.model_dump()
    cfg = aas.save_automation_config(app, payload)
    db.commit()
    return {"code": 200, "msg": "自动化配置已保存", "data": {"automation": cfg}}


@router.get("/icon-targets/{app_id}")
def list_icon_targets(
    app_id: str,
    page: int = 1,
    page_size: int = 50,
    keyword: str = "",
    db: Session = Depends(get_db),
):
    _get_app(db, app_id)
    data = its.list_icon_targets(db, app_id, page=page, page_size=page_size, keyword=keyword)
    return {"code": 200, "data": data}


@router.post("/icon-targets/{app_id}/seed-login-templates")
def seed_login_icon_templates(app_id: str, db: Session = Depends(get_db)):
    app = _get_app(db, app_id)
    from server.services.execution_clarification_service import ensure_default_login_icon_templates

    result = ensure_default_login_icon_templates(db, app_id)
    msg = (
        f"已写入 {result.get('created', 0)} 条登录图标占位"
        if result.get("created")
        else "登录图标模板已存在；若需从设计稿导入请点「从 Figma 导入」"
    )
    return {"code": 200, "msg": msg, "data": result}


@router.post("/icon-targets/{app_id}/seed-from-figma")
def seed_login_icons_from_figma(app_id: str, db: Session = Depends(get_db)):
    app = _get_app(db, app_id)
    from server.services.figma_icon_service import seed_login_icons_from_figma as seed_fn

    try:
        result = seed_fn(db, app)
        db.commit()
    except ValueError as e:
        msg = str(e)
        if "429" in msg:
            raise HTTPException(status_code=429, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    if not (result.get("created") or result.get("updated")):
        msg = result.get("msg") or "未能从 Figma 提取登录图标，请确认已配置设计稿且存在登录/注册页"
        if "429" in msg:
            raise HTTPException(status_code=429, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return {
        "code": 200,
        "msg": f"已从 Figma「{result.get('frame_name') or '登录页'}」导入 {len(result.get('icons') or [])} 个图标",
        "data": result,
    }


@router.post("/icon-targets/{app_id}/from-locate")
def import_icon_from_locate(app_id: str, body: IconFromLocateBody, db: Session = Depends(get_db)):
    _get_app(db, app_id)
    try:
        row = its.import_from_locate(db, app_id, body.model_dump())
        return {"code": 200, "msg": "已加入图标库", "data": row}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/icon-targets/{app_id}")
def save_icon_target(app_id: str, body: IconTargetBody, db: Session = Depends(get_db)):
    _get_app(db, app_id)
    row = its.upsert_icon_target(db, app_id, body.model_dump())
    return {"code": 200, "data": row}


@router.delete("/icon-targets/{app_id}/{target_id}")
def delete_icon_target(app_id: str, target_id: str, db: Session = Depends(get_db)):
    if not its.delete_icon_target(db, app_id, target_id):
        raise HTTPException(status_code=404, detail="Target not found")
    return {"code": 200, "msg": "已删除"}


@router.post("/icon-targets/{app_id}/upload")
async def upload_icon_image(
    app_id: str,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    _get_app(db, app_id)
    uploads = os.path.join(APP_DATA_DIR, "uploads")
    os.makedirs(uploads, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1] or ".png"
    name = f"icon_{uuid.uuid4().hex[:12]}{ext}"
    path = os.path.join(uploads, name)
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)
    return {"code": 200, "data": {"image_url": f"/static/{name}"}}


@router.get("/icon-targets/{app_id}/graph-candidates")
def graph_icon_candidates(app_id: str, db: Session = Depends(get_db)):
    _get_app(db, app_id)
    items = its.list_graph_import_candidates(db, app_id)
    return {"code": 200, "data": {"items": items}}


@router.post("/icon-targets/{app_id}/import-graph")
def import_graph_icon(app_id: str, body: Dict[str, Any], db: Session = Depends(get_db)):
    uid = (body.get("component_uid") or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="component_uid required")
    row = its.import_from_graph_component(db, app_id, uid)
    if not row:
        raise HTTPException(status_code=404, detail="组件不存在")
    return {"code": 200, "data": row}


@router.get("/cases/{app_id}")
def list_cached_cases(app_id: str, refresh: bool = False, db: Session = Depends(get_db)):
    from server.services import feishu_regression_service as frs

    app = _get_app(db, app_id)
    try:
        if refresh:
            data = frs.fetch_cases_for_app(app, persist=True)
            db.commit()
        else:
            data = frs.list_cases_for_app(app, refresh=False)
        return {"code": 200, "data": data}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/runs/{app_id}")
def list_app_runs(app_id: str, limit: int = 30, db: Session = Depends(get_db)):
    _get_app(db, app_id)
    runs = aas.list_runs_for_app(db, app_id, limit=limit)
    return {"code": 200, "data": {"runs": runs}}
