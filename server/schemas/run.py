# server/schemas/run.py
from pydantic import BaseModel, ConfigDict # 导入 ConfigDict
from typing import Dict, Any, Optional


class RunCreate(BaseModel):
    # 如果你想在运行时临时修改变量，可以传这个
    params: Optional[Dict[str, Any]] = {}


class RunResponse(BaseModel):
    run_id: str
    status: str
    message: str

class RunList(BaseModel):
    workflow_id: int
    run_uuid: str
    status: str
    result_summary: Optional[Dict[str, Any]] = None # 建议设为 Optional，防止数据库里是空

    # --- 关键修复：允许从 ORM 对象（SQLAlchemy）读取数据 ---
    model_config = ConfigDict(from_attributes=True)