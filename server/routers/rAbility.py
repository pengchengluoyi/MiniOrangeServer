# !/usr/bin/env python
# -*-coding:utf-8 -*-
from fastapi import APIRouter, Body
from ability.manager import Manager
from ability.component.router import BaseRouter

router = APIRouter(prefix="/ability", tags=["Ability"])

@router.post("/execute")
def execute_ability(data: dict = Body(...)):
    """
    通用能力执行接口。
    
    Payload Example:
    {
        "nodeCode": "public/ocr",
        "path": "/path/to/image.png",
        "platform": "web"
    }
    """
    return Manager().execute_interface(data.get("data"))

# (可选) 动态注册所有已知的组件路由
# 这样可以提供更直观的接口，例如 POST /ability/public/ocr
# 注意：这依赖于 BaseRouter.routes 已经加载了所有组件
if hasattr(BaseRouter, 'routes') and isinstance(BaseRouter.routes, dict):
    for node_code in BaseRouter.routes.keys():
        # 将 nodeCode 转换为 URL 路径
        # 例如: public/ocr -> /public/ocr
        safe_path = "/" + node_code.strip("/")
        
        # 使用闭包绑定 node_code
        @router.post(safe_path, summary=f"Execute {node_code}")
        def dynamic_endpoint(data: dict = Body(...), code: str = node_code):
            # 强制注入 nodeCode，确保调用正确的组件
            data['nodeCode'] = code
            return Manager().execute_interface(data)