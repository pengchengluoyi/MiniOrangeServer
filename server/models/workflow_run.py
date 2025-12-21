# models/workflow_run.py
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float
from sqlalchemy.orm import relationship
from datetime import datetime
from server.core.database import Base


class WorkflowRun(Base):
    __tablename__ = "workflow_run"

    id = Column(Integer, primary_key=True, index=True)

    # 关键关联：关联到具体的 workflow
    workflow_id = Column(Integer, ForeignKey("workflows.id"), index=True)

    # 业务ID：用来和日志系统(logs.db)关联的唯一UUID
    run_uuid = Column(String, unique=True, index=True)

    # 核心统计字段
    status = Column(String, default="pending")  # pending, running, success, failed
    trigger_type = Column(String, default="manual")  # manual(手动), schedule(定时), api(接口)

    # 时间统计
    start_time = Column(DateTime, default=datetime.now)
    end_time = Column(DateTime, nullable=True)
    duration = Column(Float, default=0.0)  # 耗时(秒)

    # 执行结果摘要 (不是详细日志，只是简短的报错信息或成功消息)
    result_summary = Column(Text, nullable=True)

    # 建立反向关系
    workflow = relationship("Workflow", back_populates="runs")