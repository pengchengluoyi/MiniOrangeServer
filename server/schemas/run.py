# server/schemas/run.py
from pydantic import BaseModel
from typing import Dict, Any, Optional


class RunCreate(BaseModel):
    # 如果你想在运行时临时修改变量，可以传这个
    params: Optional[Dict[str, Any]] = {}


class RunResponse(BaseModel):
    run_id: str
    status: str
    message: str