# server/services/run_service.py
from fastapi import Depends
from datetime import datetime
from server.core.database import get_db
from sqlalchemy.orm import Session
from server.models.workflow_run import WorkflowRun
from script.log import SLog, current_run_id, current_flow_id




def create_run(trigger="manual", db: Session = Depends(get_db)):
    """1. 开始执行时：创建记录"""
    new_run = WorkflowRun(
        workflow_id=current_flow_id,
        run_uuid=current_run_id,
        status="pending",
        trigger_type=trigger,
        start_time=datetime.now()
    )
    db.add(new_run)
    db.commit()
    db.refresh(new_run)
    return new_run

def finish_run(status: str, summary: str = None, db: Session = Depends(get_db)):
    """2. 执行结束时：更新状态和耗时"""
    run = db.query(WorkflowRun).filter(WorkflowRun.run_uuid == current_run_id).first()
    if run:
        run.end_time = datetime.now()
        run.status = status  # 'success' or 'failed'
        run.result_summary = summary

        # 计算耗时
        delta = run.end_time - run.start_time
        run.duration = delta.total_seconds()

        db.commit()