# !/usr/bin/env python
# -*-coding:utf-8 -*-
import uuid
from typing import Dict, Optional
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
    env: Dict[str, str] = {}

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