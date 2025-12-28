# app/routers/rWorkflow.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
import json

from server.core.database import get_db
from server.models.workflow_run import WorkflowRun
from server.schemas.run import RunList

router = APIRouter(prefix="/workflow_run", tags=["Workflow run"])

@router.get("/list", response_model=dict)
def get_list(db: Session = Depends(get_db)):
    wfs = db.query(WorkflowRun).all()
    data = [RunList.model_validate(w).model_dump() for w in wfs]

    return {"code": 200, "data": data}


@router.get("/detail_simple/{run_uuid}")
def get_workflow_run_detail_simple(run_uuid: str, db: Session = Depends(get_db)):
    """
    根据 ID 获取工作流详情，包括 nodes
    """
    # 1. 查询数据库
    wf = db.query(WorkflowRun).filter(WorkflowRun.run_uuid == run_uuid).first()
    # 2. 如果找不到，抛出 404 错误
    if not wf:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 4. 返回标准格式
    return {
        "code": 200,
        "data": {
            "success": wf.status,
            "start_time": wf.start_time,
            "end_time": wf.end_time,
            "duration": wf.duration
        }
    }

@router.get("/detail/{run_uuid}")
def get_workflow_run_detail(run_uuid: str, db: Session = Depends(get_db)):
    """
    根据 ID 获取工作流详情，包括 nodes
    """
    # 1. 查询数据库
    wf = db.query(WorkflowRun).filter(WorkflowRun.run_uuid == run_uuid).first()

    # 2. 如果找不到，抛出 404 错误
    if not wf:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 4. 返回标准格式
    return {
        "code": 200,
        "data": {
            "success": wf.status,
            "trigger_type": wf.trigger_type,
            "start_time": wf.start_time,
            "end_time": wf.end_time,
            "duration": wf.duration,
            "result_summary": wf.result_summary
        }
    }