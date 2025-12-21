# app/schemas/rWorkflow.py
from pydantic import BaseModel
from typing import Dict, Any, Optional
from datetime import datetime


# 前端传过来的创建参数
class WorkflowCreate(BaseModel):
    name: str
    desc: str
    nodes: Dict[str, Any]

# 前端传过来的保存参数
class WorkflowSave(BaseModel):
    id: int
    name: str
    desc: str
    nodes: Dict[str, Any]


# 返回给前端的列表项
class WorkflowItem(BaseModel):
    id: int
    name: str
    desc: str
    updated_at: datetime

    class Config:
        from_attributes = True  # 允许从 ORM 模型读取数据


# 返回给前端的详情
class WorkflowDetail(BaseModel):
    id: int
    name: str
    desc: str
    nodes: Dict[str, Any]

    class Config:
        from_attributes = True