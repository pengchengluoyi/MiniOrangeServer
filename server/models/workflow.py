# models/rWorkflow.py
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, JSON
from datetime import datetime
from sqlalchemy.orm import relationship # <--- 新增导入
from server.core.database import Base

class Workflow(Base):
    __tablename__ = "workflows"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    desc = Column(String)
    nodes = Column(Text)  # 存 JSON 字符串
    
    # 3. Workflow 级变量: 针对具体 Case 的输入 (e.g. {"input_text": "123", "retry_count": 3})
    variables = Column(JSON, default={}) 

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 1. SOP (1) -> Workflow (N)
    # Workflow 充当 Test Case 的角色，归属于某个 SOP
    sop_id = Column(Integer, ForeignKey("app_sops.id"), nullable=True)
    sop = relationship("AppSOP", back_populates="workflows")

    # <--- 新增这行，关联执行记录
    runs = relationship("WorkflowRun", back_populates="workflow", cascade="all, delete-orphan")