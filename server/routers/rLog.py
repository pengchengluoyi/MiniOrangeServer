from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from ..core.log_database import get_log_db
from ..models.log import WorkflowLog

router = APIRouter(prefix="/logs", tags=["Logs"])

# 1. 插入日志 (供 Python 脚本调用)
# 如果你是内部调用，可以直接用 Session，不必走 HTTP
@router.post("/")
def create_log(
    run_id: str, 
    flow_id: str, 
    message: str, 
    tag: str = None,
    level: str = "INFO", 
    node_id: str = None, 
    db: Session = Depends(get_log_db) # 注意这里用的是 log_db
):
    new_log = WorkflowLog(
        run_id=run_id,
        flow_id=flow_id,
        message=message,
        tag=tag,
        level=level,
        node_id=node_id
    )
    db.add(new_log)
    db.commit()
    return  {"code": 200, "msg": "ok"}

# 2. 查询日志 (供前端看板调用)
@router.get("/{run_id}")
def read_logs(run_id: str, db: Session = Depends(get_log_db)):
    logs = db.query(WorkflowLog)\
             .filter(WorkflowLog.run_id == run_id)\
             .order_by(WorkflowLog.created_at.asc())\
             .all()
    return logs