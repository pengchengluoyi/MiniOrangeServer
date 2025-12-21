# models/rWorkflow.py
from sqlalchemy import Column, Integer, String, Text, DateTime
from datetime import datetime
from sqlalchemy.orm import relationship # <--- 新增导入
from server.core.database import Base

class Workflow(Base):
    __tablename__ = "workflows"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    desc = Column(String)
    nodes = Column(Text)  # 存 JSON 字符串
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # <--- 新增这行，关联执行记录
    runs = relationship("WorkflowRun", back_populates="workflow", cascade="all, delete-orphan")