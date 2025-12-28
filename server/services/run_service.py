# server/services/run_service.py
from fastapi import Depends
from datetime import datetime
from server.core.database import SessionLocal
from sqlalchemy.orm import Session
from server.models.workflow_run import WorkflowRun
from script.log import SLog, current_run_id, current_flow_id




def create_run(trigger="manual", db: Session = None):
    """1. 开始执行时：创建记录"""
    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True
    try:
        new_run = WorkflowRun(
            workflow_id=current_flow_id.get(),
            run_uuid=current_run_id.get(),
            status="pending",
            trigger_type=trigger,
            start_time=datetime.now()
        )
        db.add(new_run)
        db.commit()
        db.refresh(new_run)
    except Exception as e:
        # 如果是本地创建的 session，出错时回滚
        if close_session:
            db.rollback()
        raise e
    finally:
        # 3. 关闭 Session
        if close_session:
            db.close()
    return new_run



def finish_run(status: str, summary: str = None, db: Session = None):
    """2. 执行结束时：更新状态和耗时"""
    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True
    try:
        run = db.query(WorkflowRun).filter(WorkflowRun.run_uuid == current_run_id.get()).first()
        if run:
            run.end_time = datetime.now()
            run.status = status  # 'success' or 'failed'
            run.result_summary = summary

            # 计算耗时
            delta = run.end_time - run.start_time
            run.duration = delta.total_seconds()

            db.commit()
    except Exception as e:
        # 如果是本地创建的 session，出错时回滚
        if close_session:
            db.rollback()
        raise e
    finally:
        # 3. 关闭 Session
        if close_session:
            db.close()
    return run
