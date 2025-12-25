# !/usr/bin/env python
# -*-coding:utf-8 -*-
import uuid
from typing import Optional
from datetime import datetime
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from server.core.database import get_db
from server.models.task import Task

router = APIRouter(prefix="/task", tags=["Task Management"])

class TaskCreate(BaseModel):
    app_id: str
    name: str
    type: str

@router.post("/create")
def create_task(item: TaskCreate, db: Session = Depends(get_db)):
    task = Task(
        id=str(uuid.uuid4()),
        app_id=item.app_id,
        name=item.name,
        type=item.type,
        status="pending",
        created_at=datetime.now()
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task

@router.get("/list")
def list_tasks(
    app_id: Optional[str] = Query(None, alias="appId"),
    type: Optional[str] = None,
    keyword: Optional[str] = None,
    db: Session = Depends(get_db)
):
    query = db.query(Task)
    
    if app_id:
        query = query.filter(Task.app_id == app_id)
    
    if type and type != 'all':
        query = query.filter(Task.type == type)
        
    if keyword:
        query = query.filter(Task.name.contains(keyword))
        
    tasks = query.order_by(Task.created_at.desc()).all()
    return tasks