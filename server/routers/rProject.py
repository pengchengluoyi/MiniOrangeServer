# !/usr/bin/env python
# -*-coding:utf-8 -*-
import uuid
from typing import Any, Dict, Optional
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
    # 使用 joinedload 预加载关联的 apps
    projects = db.query(Project).options(joinedload(Project.apps)).all()
    return projects


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
    from server.services.project_env import load_project_env, ENV_PROFILE_LABELS

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    doc = load_project_env(db, project_id)
    return {
        "code": 200,
        "data": {
            "project_id": project.id,
            "project_name": project.name,
            "env": doc,
            "profile_labels": ENV_PROFILE_LABELS,
        },
    }


@router.put("/{project_id}/env")
def update_project_env(project_id: str, item: ProjectEnvUpdate, db: Session = Depends(get_db)):
    from server.services.project_env import normalize_project_env, ENV_PROFILE_KEYS

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    doc = normalize_project_env(
        {"default_profile": item.default_profile, "profiles": item.profiles}
    )
    if doc["default_profile"] not in ENV_PROFILE_KEYS:
        raise HTTPException(status_code=400, detail="Invalid default_profile")
    project.env = doc
    db.commit()
    db.refresh(project)
    return {"code": 200, "msg": "Project env updated", "data": {"env": project.env}}