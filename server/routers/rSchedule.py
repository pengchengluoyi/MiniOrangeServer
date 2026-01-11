from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List, Any
import json

from server.core.database import get_db
from server.models.schedule import ScheduledTask, ScheduledTaskHistory
from server.core.scheduler import SchedulerService

router = APIRouter(prefix="/schedule", tags=["Schedule"])


class ScheduleCreate(BaseModel):
    name: str
    app_id: Optional[str] = None
    cron_expression: str
    flow_id: str
    target_sn: Optional[str] = None
    is_active: bool = True
    skip_nodes: Optional[List[str]] = []


class ScheduleUpdate(BaseModel):
    name: Optional[str] = None
    app_id: Optional[str] = None
    cron_expression: Optional[str] = None
    flow_id: Optional[str] = None
    target_sn: Optional[str] = None
    is_active: Optional[bool] = None
    skip_nodes: Optional[List[str]] = None


@router.post("/create")
def create_schedule(item: ScheduleCreate, db: Session = Depends(get_db)):
    # Validate cron expression (simple check, APScheduler will validate strictly on add)
    if len(item.cron_expression.split()) != 5:
        raise HTTPException(status_code=400, detail="Invalid cron expression. Format: 'min hour day month day_of_week'")

    db_item = ScheduledTask(
        name=item.name,
        app_id=item.app_id,
        cron_expression=item.cron_expression,
        flow_id=item.flow_id,
        target_sn=item.target_sn,
        is_active=item.is_active,
        skip_nodes=json.dumps(item.skip_nodes) if item.skip_nodes else "[]"
    )
    db.add(db_item)
    db.commit()
    db.refresh(db_item)

    SchedulerService().add_job(db_item)
    return {"code": 200, "data": db_item}


@router.get("/list")
def list_schedules(db: Session = Depends(get_db)):
    tasks = db.query(ScheduledTask).all()
    res = []
    for t in tasks:
        d = {
            "id": t.id,
            "name": t.name,
            "app_id": t.app_id,
            "cron_expression": t.cron_expression,
            "flow_id": t.flow_id,
            "target_sn": t.target_sn,
            "is_active": t.is_active,
            "last_run_time": t.last_run_time,
            "created_at": t.created_at,
            "skip_nodes": json.loads(t.skip_nodes) if t.skip_nodes else []
        }
        res.append(d)
    return {"code": 200, "data": res}


@router.post("/update/{task_id}")
def update_schedule(task_id: str, item: ScheduleUpdate, db: Session = Depends(get_db)):
    task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
    if not task:
        return {"code": 404, "msg": "Task not found"}

    if item.name is not None: task.name = item.name
    if item.app_id is not None: task.app_id = item.app_id
    if item.cron_expression is not None: task.cron_expression = item.cron_expression
    if item.flow_id is not None: task.flow_id = item.flow_id
    if item.target_sn is not None: task.target_sn = item.target_sn
    if item.is_active is not None: task.is_active = item.is_active
    if item.skip_nodes is not None: task.skip_nodes = json.dumps(item.skip_nodes)

    db.commit()
    db.refresh(task)

    SchedulerService().update_job(task)
    return {"code": 200, "data": "Updated"}


@router.delete("/delete/{task_id}")
def delete_schedule(task_id: str, db: Session = Depends(get_db)):
    task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
    if not task:
        return {"code": 404, "msg": "Task not found"}

    SchedulerService().remove_job(task_id)
    db.delete(task)
    db.commit()
    return {"code": 200, "msg": "Deleted"}

@router.get("/{task_id}/history")
def get_schedule_history(task_id: str, limit: int = 20, db: Session = Depends(get_db)):
    """获取指定定时任务的执行历史"""
    history_records = db.query(ScheduledTaskHistory)\
        .filter(ScheduledTaskHistory.schedule_id == task_id)\
        .order_by(ScheduledTaskHistory.created_at.desc())\
        .limit(limit)\
        .all()
    
    return {"code": 200, "data": [{
        "run_id": h.run_id,
        "status": h.status,
        "details": h.details,
        "created_at": h.created_at
    } for h in history_records]}