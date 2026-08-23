# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""应用自动化配置 API（Skills、图标目标、用例缓存）。"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload

from server.core.database import APP_DATA_DIR, get_db
from server.models.project import App
from server.services import app_automation_service as aas
from server.services.shared import icon_target_service as its
from server.services.project_env import load_project_env, profile_keys
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


class CaseSuiteBody(BaseModel):
    id: str = ""
    name: str
    case_ids: List[str] = []
    updated_at: str = ""


class AutomationConfigUpdate(BaseModel):
    env_profile: Optional[str] = None
    execution_env: Optional[ExecutionEnvConfig] = None
    skills: Optional[AutomationSkills] = None
    figma: Optional[FigmaDesignConfig] = None
    suites: Optional[List[CaseSuiteBody]] = None
    qa_process: Optional[Dict[str, Any]] = None


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
    cases = aas.list_app_cases(app)
    env_doc = load_project_env(db, app.project_id) if app.project_id else None
    return {
        "code": 200,
        "data": {
            "app_id": app.id,
            "app_name": app.name,
            "project_name": app.project.name if app.project else "",
            "env_profile": cfg.get("env_profile"),
            "env_profiles": profile_keys(env_doc) if env_doc is not None else ["test", "pre", "prod"],
            "package": pkg,
            "automation": cfg,
            "stats": {
                "icon_targets": icon_page.get("total", 0),
                "case_count": len(cases),
                "feishu_cases": len(cases),
            },
        },
    }


@router.get("/qa-process/summary")
def qa_process_summary(db: Session = Depends(get_db)):
    """实验室排期：一次返回所有应用的需求/版本/排期，供「全部项目」日历聚合。"""
    apps = db.query(App).options(joinedload(App.project)).order_by(App.name).all()
    items = []
    for app in apps:
        proc = aas.get_automation_config(app).get("qa_process") or {}
        items.append({
            "app_id": app.id,
            "app_name": app.name or "",
            "project_id": app.project_id or "",
            "project_name": app.project.name if app.project else "",
            "requirements": proc.get("requirements") or [],
            "releases": proc.get("releases") or [],
            "schedule": proc.get("schedule") or [],
            "workflow": proc.get("workflow") or None,
        })
    return {"code": 200, "ok": True, "data": {"items": items}}


class QaProcessAssistBody(BaseModel):
    entity: str
    id: str
    job: str
    requirement: Optional[Dict[str, Any]] = None
    release: Optional[Dict[str, Any]] = None
    requirements: Optional[List[Dict[str, Any]]] = None
    cases: Optional[List[Dict[str, Any]]] = None
    tasks: Optional[List[Dict[str, Any]]] = None
    suites: Optional[List[Dict[str, Any]]] = None


@router.post("/qa-process/assist/{app_id}")
def qa_process_assist(app_id: str, body: QaProcessAssistBody, db: Session = Depends(get_db)):
    """流程建议。只返回草稿，不改 gate、不写飞书。"""
    from server.services import qa_process_assist as assist

    _get_app(db, app_id)
    if body.job not in assist.ASSIST_JOBS:
        raise HTTPException(status_code=400, detail="unknown assist job")
    if body.entity not in ("req", "rel"):
        raise HTTPException(status_code=400, detail="entity must be req or rel")
    art = assist.run_job(
        body.job,
        requirement=body.requirement,
        release=body.release,
        requirements=body.requirements or [],
        cases=body.cases or [],
        tasks=body.tasks or [],
        suites=body.suites or [],
    )
    return {"code": 200, "ok": True, "data": {"artifact": art}}


class QaProcessTickBody(BaseModel):
    requirement_id: str = ""
    user_note: str = ""
    force: bool = False
    jobs: List[str] = []


class AtlasPatchBody(BaseModel):
    patch_id: str
    action: str = "accept"
    after: dict | None = None
    reason: str = ""
    note: str = ""
    rerun: bool = True
    run_pipeline: bool = True
    release_id: str = ""


def _followup_in_background(
    *,
    app_id: str,
    qa_process: dict,
    cases: list,
    requirement_ids: list,
    trigger: str,
    app_name: str,
    pipeline_id: str,
    force: bool,
) -> None:
    from server.core.database import SessionLocal
    from server.services.qa_role_jobs import run_followup_pipeline

    try:
        result = run_followup_pipeline(
            qa_process=qa_process,
            cases=cases,
            requirement_ids=requirement_ids,
            trigger=trigger,
            app_id=app_id,
            app_name=app_name,
            pipeline_id=pipeline_id,
            force=force,
        )
        with SessionLocal() as db:
            app = db.query(App).filter(App.id == app_id).first()
            if not app:
                return
            aas.save_automation_config(app, {"qa_process": result.get("qa_process") or qa_process})
            db.commit()
    except Exception:
        logging.exception("atlas followup failed for %s", app_id)
        try:
            from server.services.ai import dispatch_log as dispatch

            err_tok = dispatch.bind(
                trigger=trigger,
                app_id=app_id,
                app_name=app_name,
                pipeline_id=pipeline_id,
                role="req-analyst",
                job="atlas_followup",
            )
            dispatch.record_job(status="error", job="atlas_followup", role="req-analyst", error="后台补脑图/用例失败")
            dispatch.reset(err_tok)
        except Exception:
            pass


def _reanalyze_in_background(
    *,
    app_id: str,
    qa_process: dict,
    cases: list,
    requirement_ids: list,
    user_note: str,
    app_name: str,
    pipeline_id: str,
) -> None:
    from server.core.database import SessionLocal
    from server.services.qa_role_jobs import tick

    try:
        result = tick(
            qa_process=qa_process,
            cases=cases,
            requirement_ids=requirement_ids,
            app_id=app_id,
            app_name=app_name,
            user_note=user_note,
            force=True,
        )
        with SessionLocal() as db:
            app = db.query(App).filter(App.id == app_id).first()
            if not app:
                return
            aas.save_automation_config(app, {"qa_process": result.get("qa_process") or qa_process})
            db.commit()
    except Exception:
        logging.exception("atlas reject reanalyze failed for %s", app_id)
        try:
            from server.services.ai import dispatch_log as dispatch

            err_tok = dispatch.bind(
                trigger="atlas_reject",
                app_id=app_id,
                app_name=app_name,
                pipeline_id=pipeline_id,
                role="req-analyst",
                job="qa_tick",
            )
            dispatch.record_job(status="error", job="qa_tick", role="req-analyst", error="按驳回说明重跑分析失败")
            dispatch.reset(err_tok)
        except Exception:
            pass


@router.post("/qa-process/atlas-patch/{app_id}")
def qa_process_atlas_patch(app_id: str, body: AtlasPatchBody, db: Session = Depends(get_db)):
    """人审影响范围：确认、驳回，或保存人手改过的骨架。确认后立刻返回，脑图→用例在后台跑。"""
    from server.services.ai import app_atlas as atlas
    from server.services.ai import dispatch_log as dispatch

    app = _get_app(db, app_id)
    cfg = aas.get_automation_config(app)
    doc = dict(cfg.get("qa_process") or {})
    action = (body.action or "").strip().lower()
    hung_ids: list[str] = []
    if body.after and isinstance(body.after, dict):
        patches = [dict(x) for x in (doc.get("atlas_patches") or []) if isinstance(x, dict)]
        for row in patches:
            if row.get("id") == body.patch_id:
                row["after"] = atlas.normalize_atlas(body.after)
                row["diff"] = atlas.diff_atlas(row.get("before"), row["after"])
                row["lines"] = atlas.diff_lines(row["diff"], doc.get("requirements") or [])
                if body.reason:
                    row["reason"] = body.reason
                break
        doc["atlas_patches"] = patches
    if action == "save":
        found = next((x for x in (doc.get("atlas_patches") or []) if isinstance(x, dict) and x.get("id") == body.patch_id), None)
        if not found:
            raise HTTPException(status_code=404, detail="没有这条图谱变更")
        if found.get("status") == "accepted" and found.get("after"):
            after = atlas.normalize_atlas(found.get("after"))
            after["updated_at"] = found.get("decided_at") or ""
            doc = atlas.stamp_atlas_on_release(doc, after, body.release_id)
            doc["requirements"] = atlas.apply_hangs_to_reqs(doc.get("requirements") or [], after)
            doc["features"] = atlas.flatten_features(after, doc.get("requirements") or [])
        patch = found
        next_doc = doc
    elif action == "accept":
        next_doc, patch = atlas.accept_patch(doc, body.patch_id)
        if next_doc and patch:
            next_doc = atlas.stamp_atlas_on_release(next_doc, next_doc.get("app_atlas") or {}, body.release_id)
    elif action == "reject":
        next_doc, patch = atlas.reject_patch(doc, body.patch_id, note=body.note)
        if next_doc and patch:
            next_doc, hung_ids = atlas.apply_reject_feedback(next_doc, patch, body.note)
        else:
            hung_ids = []
    else:
        raise HTTPException(status_code=400, detail="action must be accept, reject or save")
    if not patch:
        raise HTTPException(status_code=404, detail="没有这条待确认的图谱变更")
    log = [x for x in (next_doc.get("role_log") or []) if isinstance(x, dict)]
    log.append(
        {
            "at": patch.get("decided_at") or "",
            "role": "req-analyst",
            "job": "review_impact" if action != "save" else "edit_atlas",
            "action": action,
            "patch_id": body.patch_id,
        }
    )
    next_doc["role_log"] = log[-80:]
    pipeline_id = ""
    if body.run_pipeline and action in ("accept", "save"):
        cases = aas.list_app_cases(app)
        hung = atlas.patch_followup_req_ids(patch)
        force = atlas.patch_is_structural(patch)
        if not hung:
            hung = [str(r.get("id") or "") for r in (next_doc.get("requirements") or []) if isinstance(r, dict) and r.get("id")]
            force = False
        if hung:
            pipeline_id = dispatch.new_pipeline_id()
            start = dispatch.bind(
                trigger="atlas_confirm" if action == "accept" else "atlas_edit",
                app_id=app.id,
                app_name=app.name or "",
                pipeline_id=pipeline_id,
                role="req-analyst",
                job="atlas_followup",
            )
            dispatch.record_job(
                status="running",
                job="atlas_followup",
                role="req-analyst",
                detail="确认后正在补脑图和用例",
                input_data={"requirement_ids": hung},
            )
            dispatch.reset(start)
            threading.Thread(
                target=_followup_in_background,
                kwargs={
                    "app_id": app.id,
                    "qa_process": next_doc,
                    "cases": cases,
                    "requirement_ids": hung,
                    "trigger": "atlas_confirm" if action == "accept" else "atlas_edit",
                    "app_name": app.name or "",
                    "pipeline_id": pipeline_id,
                    "force": force,
                },
                daemon=True,
            ).start()
    elif action == "reject" and body.rerun:
        cases = aas.list_app_cases(app)
        hung = hung_ids
        if not hung:
            hung = atlas.patch_followup_req_ids(patch)
        if hung:
            pipeline_id = dispatch.new_pipeline_id()
            start = dispatch.bind(
                trigger="atlas_reject",
                app_id=app.id,
                app_name=app.name or "",
                pipeline_id=pipeline_id,
                role="req-analyst",
                job="qa_tick",
            )
            dispatch.record_job(
                status="running",
                job="qa_tick",
                role="req-analyst",
                detail="按驳回说明重跑需求分析",
                input_data={"requirement_ids": hung, "note": (body.note or "")[:240]},
            )
            dispatch.reset(start)
            threading.Thread(
                target=_reanalyze_in_background,
                kwargs={
                    "app_id": app.id,
                    "qa_process": next_doc,
                    "cases": cases,
                    "requirement_ids": hung,
                    "user_note": body.note or "",
                    "app_name": app.name or "",
                    "pipeline_id": pipeline_id,
                },
                daemon=True,
            ).start()
    saved = aas.save_automation_config(app, {"qa_process": next_doc})
    db.commit()
    return {
        "code": 200,
        "ok": True,
        "data": {
            "qa_process": saved.get("qa_process") or next_doc,
            "patch": patch,
            "action": action,
            "pipeline_id": pipeline_id,
            "pipeline_pending": bool(pipeline_id),
            "actions": [],
        },
    }


@router.post("/qa-process/tick/{app_id}")
def qa_process_tick(app_id: str, body: QaProcessTickBody, db: Session = Depends(get_db)):
    """角色自动推进：分析需求、写脑图、补用例草稿。不改验收/发版门禁，不自动下发设备。"""
    from server.services.qa_role_jobs import tick

    app = _get_app(db, app_id)
    cfg = aas.get_automation_config(app)
    cases = aas.list_app_cases(app)
    result = tick(
        qa_process=cfg.get("qa_process") or {},
        cases=cases,
        requirement_id=body.requirement_id,
        app_id=app.id,
        app_name=app.name or "",
        user_note=body.user_note,
        force=body.force,
        jobs=body.jobs or [],
    )
    saved = aas.save_automation_config(app, {"qa_process": result.get("qa_process") or {}})
    qa = saved.get("qa_process") or result.get("qa_process") or {}
    db.commit()
    return {
        "code": 200,
        "ok": True,
        "data": {
            "qa_process": qa,
            "actions": result.get("actions") or [],
            "autonomy": result.get("autonomy") or {},
            "usage": result.get("usage") or {},
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
    payload: Dict[str, Any] = {}
    if body.env_profile is not None:
        payload["env_profile"] = body.env_profile
    if body.execution_env is not None:
        payload["execution_env"] = body.execution_env.model_dump()
    if body.skills is not None:
        payload["skills"] = body.skills.model_dump()
    if body.figma is not None:
        payload["figma"] = body.figma.model_dump()
    if body.suites is not None:
        payload["suites"] = [s.model_dump() for s in body.suites]
    if body.qa_process is not None:
        payload["qa_process"] = body.qa_process
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
    app = _get_app(db, app_id)
    return {"code": 200, "data": aas.cases_payload(app)}


@router.get("/runs/{app_id}")
def list_app_runs(app_id: str, limit: int = 30, db: Session = Depends(get_db)):
    _get_app(db, app_id)
    runs = aas.list_runs_for_app(db, app_id, limit=limit)
    return {"code": 200, "data": {"runs": runs}}
