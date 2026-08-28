# !/usr/bin/env python
# -*-coding:utf-8 -*-
import uuid
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from server.core.database import get_db
from server.models.project import Project, App
from pydantic import BaseModel

router = APIRouter(prefix="/project", tags=["Project Management"])

# 定义 Pydantic 模型用于参数校验
class ProjectCreate(BaseModel):
    name: str
    description: Optional[str] = None

class AppCreate(BaseModel):
    project_id: str
    name: str
    description: Optional[str] = None
    platforms: str
    env: Dict[str, Any] = {}


class AppEnvUpdate(BaseModel):
    env: Dict[str, Any]


class ProjectEnvUpdate(BaseModel):
    default_profile: Optional[str] = "test"
    profiles: Dict[str, Any] = {}
    environments: Optional[list] = None
    channels: Optional[list] = None
    pipeline: Optional[list] = None

@router.post("/create")
def create_project(item: ProjectCreate, db: Session = Depends(get_db)):
    db_project = Project(
        id=str(uuid.uuid4()),
        name=item.name,
        description=item.description
    )
    db.add(db_project)
    db.commit()
    db.refresh(db_project)
    return db_project

@router.post("/app/create")
def create_app(item: AppCreate, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == item.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
        
    db_app = App(
        id=str(uuid.uuid4()),
        name=item.name,
        description=item.description,
        platforms=item.platforms,
        env=item.env,
        project_id=item.project_id
    )
    db.add(db_app)
    db.commit()
    db.refresh(db_app)
    return db_app

@router.get("/list")
def list_projects(db: Session = Depends(get_db)):
    from server.models.app_icon_target import AppIconTarget
    from server.services import app_automation_service as aas

    projects = db.query(Project).options(joinedload(Project.apps)).all()
    out = []
    for p in projects:
        apps_out = []
        for a in p.apps or []:
            env = a.env if isinstance(a.env, dict) else {}
            case_n = aas.count_qa_process_cases_from_env(env)
            icon_n = db.query(AppIconTarget).filter(AppIconTarget.app_id == a.id).count()
            apps_out.append(
                {
                    "id": a.id,
                    "uid": a.uid,
                    "name": a.name,
                    "description": a.description,
                    "platforms": a.platforms,
                    "project_id": a.project_id,
                    "automation_stats": {
                        "icon_targets": icon_n,
                        "case_count": case_n,
                        "feishu_cases": case_n,
                    },
                }
            )
        out.append(
            {
                "id": p.id,
                "uid": p.uid,
                "name": p.name,
                "description": p.description,
                "env": p.env,
                "apps": apps_out,
            }
        )
    return out


def _purge_app(db: Session, app_id: str) -> None:
    from server.models.app_icon_target import AppIconTarget
    from server.models.app_regression_run import AppRegressionRun
    from server.models.AppGraph.app_structure import AppGraph

    db.query(AppIconTarget).filter(AppIconTarget.app_id == app_id).delete(synchronize_session=False)
    db.query(AppRegressionRun).filter(AppRegressionRun.app_id == app_id).delete(synchronize_session=False)
    db.query(AppGraph).filter(AppGraph.app_id == app_id).update(
        {AppGraph.app_id: None},
        synchronize_session=False,
    )
    app = db.query(App).filter(App.id == app_id).first()
    if app:
        db.delete(app)


@router.delete("/app/{app_id}")
def delete_app(app_id: str, db: Session = Depends(get_db)):
    """删除应用。图标目标和回归记录一并清掉；图谱只解绑，不删历史任务。"""
    app = db.query(App).filter(App.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    name = app.name
    project_id = app.project_id
    _purge_app(db, app_id)
    db.commit()
    return {
        "code": 200,
        "ok": True,
        "msg": "deleted",
        "data": {"id": app_id, "name": name, "project_id": project_id},
    }


@router.get("/app/{app_id}")
def get_app(app_id: str, db: Session = Depends(get_db)):
    app = db.query(App).options(joinedload(App.project)).filter(App.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    return {
        "code": 200,
        "data": {
            "id": app.id,
            "name": app.name,
            "description": app.description,
            "platforms": app.platforms,
            "env": app.env or {},
            "project_id": app.project_id,
            "project_name": app.project.name if app.project else None,
        },
    }


@router.put("/app/{app_id}/env")
def update_app_env(app_id: str, item: AppEnvUpdate, db: Session = Depends(get_db)):
    """已废弃：写入项目 test 环境，请使用 PUT /project/{id}/env。"""
    from server.services.project_env import normalize_project_env, default_project_env

    app = db.query(App).options(joinedload(App.project)).filter(App.id == app_id).first()
    if not app:
        raise HTTPException(status_code=404, detail="App not found")
    project = db.query(Project).filter(Project.id == app.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    doc = normalize_project_env(project.env or default_project_env())
    doc["profiles"]["test"] = normalize_project_env(item.env or {})["profiles"]["test"]
    project.env = doc
    db.commit()
    return {"code": 200, "msg": "已保存到项目环境(测试)", "data": doc}


@router.get("/{project_id}/env")
def get_project_env(project_id: str, db: Session = Depends(get_db)):
    from server.services.project_env import load_project_env, ENV_PROFILE_LABELS, list_test_accounts, public_test_accounts

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    doc = load_project_env(db, project_id)
    safe = dict(doc)
    safe["test_accounts"] = public_test_accounts(list_test_accounts(doc))
    return {
        "code": 200,
        "data": {
            "project_id": project.id,
            "project_name": project.name,
            "env": safe,
            "profile_labels": ENV_PROFILE_LABELS,
        },
    }


@router.put("/{project_id}/env")
def update_project_env(project_id: str, item: ProjectEnvUpdate, db: Session = Depends(get_db)):
    from server.services.project_env import normalize_project_env

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    prev = project.env if isinstance(project.env, dict) else {}
    doc = normalize_project_env(
        {
            "default_profile": item.default_profile,
            "profiles": item.profiles,
            "environments": item.environments,
            "channels": item.channels,
            "pipeline": item.pipeline,
            "test_accounts": prev.get("test_accounts"),
        }
    )
    if not doc.get("environments"):
        raise HTTPException(status_code=400, detail="至少保留一个环境")
    if doc["default_profile"] not in {e["key"] for e in doc["environments"]}:
        raise HTTPException(status_code=400, detail="默认环境不在环境列表里")
    project.env = doc
    db.commit()
    db.refresh(project)
    from server.services.project_env import list_test_accounts, public_test_accounts
    safe = dict(project.env or {})
    safe["test_accounts"] = public_test_accounts(list_test_accounts(safe))
    return {"code": 200, "msg": "Project env updated", "data": {"env": safe}}


class TestAccountSaveBody(BaseModel):
    accounts: List[Dict[str, Any]] = []


class TestAccountPickBody(BaseModel):
    prompt: str = ""
    env: str = ""


@router.get("/{project_id}/accounts")
def list_project_accounts(project_id: str, env: str = "", db: Session = Depends(get_db)):
    from server.services.project_env import load_project_env, list_test_accounts, public_test_accounts

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    doc = load_project_env(db, project_id)
    rows = list_test_accounts(doc)
    if env:
        rows = [x for x in rows if str(x.get("env") or "") == env]
    return {
        "code": 200,
        "data": {
            "accounts": public_test_accounts(rows, include_password=True),
            "environments": doc.get("environments") or [],
        },
    }


@router.put("/{project_id}/accounts")
def save_project_accounts(project_id: str, body: TestAccountSaveBody, db: Session = Depends(get_db)):
    from server.services.project_env import load_project_env, save_test_accounts, public_test_accounts, list_test_accounts
    from sqlalchemy.orm.attributes import flag_modified

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    doc = save_test_accounts(load_project_env(db, project_id), body.accounts)
    project.env = doc
    flag_modified(project, "env")
    db.commit()
    return {"code": 200, "msg": "已保存", "data": {"accounts": public_test_accounts(list_test_accounts(doc), include_password=True)}}


@router.post("/{project_id}/accounts/pick")
def pick_project_accounts(project_id: str, body: TestAccountPickBody, db: Session = Depends(get_db)):
    from server.services.project_env import load_project_env, list_test_accounts, pick_test_accounts, public_test_accounts

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    doc = load_project_env(db, project_id)
    ranked = pick_test_accounts(list_test_accounts(doc), prompt=body.prompt, env=body.env)
    return {"code": 200, "data": {"accounts": public_test_accounts(ranked)}}


@router.delete("/{project_id}")
def delete_project(project_id: str, db: Session = Depends(get_db)):
    """删除项目及其下全部应用。图谱只解绑，不删历史任务。"""
    project = db.query(Project).options(joinedload(Project.apps)).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    name = project.name
    apps = list(project.apps or [])
    app_ids = [a.id for a in apps]
    app_names = [a.name for a in apps]
    for aid in app_ids:
        _purge_app(db, aid)
    db.delete(project)
    db.commit()
    return {
        "code": 200,
        "ok": True,
        "msg": "deleted",
        "data": {"id": project_id, "name": name, "app_ids": app_ids, "app_names": app_names},
    }