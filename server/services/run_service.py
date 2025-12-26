# server/services/run_service.py
from datetime import datetime
from sqlalchemy.orm import Session
from server.models.workflow_run import WorkflowRun



class RunService:
    @staticmethod
    def create_run(workflow_id: int, run_uuid: str, trigger="manual", db: Session = Depends(get_db)):
        """1. 开始执行时：创建记录"""
        new_run = WorkflowRun(
            workflow_id=workflow_id,
            run_uuid=run_uuid,
            status="pending",
            trigger_type=trigger,
            start_time=datetime.now()
        )
        db.add(new_run)
        db.commit()
        db.refresh(new_run)
        return new_run

    @staticmethod
    def finish_run(run_uuid: str, status: str, summary: str = None, db: Session = Depends(get_db)):
        """2. 执行结束时：更新状态和耗时"""
        run = db.query(WorkflowRun).filter(WorkflowRun.run_uuid == run_uuid).first()
        if run:
            run.end_time = datetime.now()
            run.status = status  # 'success' or 'failed'
            run.result_summary = summary

            # 计算耗时
            delta = run.end_time - run.start_time
            run.duration = delta.total_seconds()

            db.commit()