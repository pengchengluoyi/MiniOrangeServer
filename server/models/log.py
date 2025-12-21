# server/app/models/rLog.py
from sqlalchemy import Column, Integer, String, Text, DateTime
from datetime import datetime
from server.core.log_database import LogBase

class WorkflowLog(LogBase):
    __tablename__ = "workflow_logs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String, index=True)       # 运行批次ID
    flow_id = Column(String, index=True)      # 哪个工作流
    node_id = Column(String, nullable=True)   # 哪个节点
    tag = Column(String, nullable=True)       # 标签
    level = Column(String, default="INFO")    # INFO, ERROR
    message = Column(Text)                    # 日志内容
    created_at = Column(DateTime, default=datetime.now)
