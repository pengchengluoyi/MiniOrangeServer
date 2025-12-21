# app/routers/rWorkflow.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List
import json
from multiprocessing import Process
import uuid

from server.core.database import get_db
from server.models.workflow import Workflow
from server.models.workflow_run import WorkflowRun
from server.schemas.workflow import WorkflowCreate, WorkflowItem, WorkflowDetail, WorkflowSave

# 引入你的包装器
from driver.agent.actuator import process_runner_wrapper

router = APIRouter(prefix="/workflow", tags=["Workflow"])

@router.post("/add", response_model=dict)
def add_workflow(item: WorkflowCreate, db: Session = Depends(get_db)):
    json_str = json.dumps(item.nodes, ensure_ascii=False)

    new_wf = Workflow(name=item.name, desc=item.desc, nodes=json_str)
    db.add(new_wf)
    db.commit()
    db.refresh(new_wf)
    return {"code": 200, "msg": "创建成功", "id": new_wf.id}

@router.post("/save", response_model=dict)
def save_workflow(item: WorkflowSave, db: Session = Depends(get_db)):
    json_str = json.dumps(item.nodes, ensure_ascii=False)

    existing = db.query(Workflow).filter(Workflow.id == item.id).first()
    if existing:
        existing.nodes = json_str
        db.commit()
        return {"code": 200, "msg": "更新成功", "id": existing.id}
    else:
        new_wf = Workflow(name=item.name, desc=item.desc, nodes=json_str)
        db.add(new_wf)
        db.commit()
        db.refresh(new_wf)
        return {"code": 200, "msg": "创建成功", "id": new_wf.id}


@router.get("/list", response_model=dict)
def get_list(db: Session = Depends(get_db)):
    wfs = db.query(Workflow).all()
    # 手动转换或使用 Pydantic List 模型
    data = [WorkflowItem.model_validate(w).model_dump() for w in wfs]
    return {"code": 200, "data": data}


@router.get("/get", response_model=dict)
def get_list(db: Session = Depends(get_db)):
    wfs = db.query(Workflow).all()
    # 手动转换或使用 Pydantic List 模型
    data = [WorkflowItem.model_validate(w).model_dump() for w in wfs]
    return {"code": 200, "data": data}


@router.get("/detail/{workflow_id}")
def get_workflow_detail(workflow_id: int, db: Session = Depends(get_db)):
    """
    根据 ID 获取工作流详情，包括 nodes
    """
    # 1. 查询数据库
    wf = db.query(Workflow).filter(Workflow.id == workflow_id).first()

    # 2. 如果找不到，抛出 404 错误
    if not wf:
        raise HTTPException(status_code=404, detail="工作流不存在")

    try:
        nodes_json = json.loads(wf.nodes) if wf.nodes else {}
    except json.JSONDecodeError:
        nodes_json = {}  # 防止数据库里存了脏数据导致报错

    # 4. 返回标准格式
    return {
        "code": 200,
        "data": {
            "id": wf.id,
            "name": wf.name,
            "nodes": nodes_json,  # 这里返回的是对象，前端可以直接用
            "updated_at": wf.updated_at
        }
    }

@router.get("/delete/{workflow_id}")
def delete_workflow(workflow_id: int, db: Session = Depends(get_db)):
    """
    删除指定 ID 的工作流
    """
    # 1. 查找是否存在
    wf = db.query(Workflow).filter(Workflow.id == workflow_id).first()

    # 2. 如果不存在，报错
    if not wf:
        raise HTTPException(status_code=404, detail="工作流不存在或已被删除")

    # 3. 执行删除
    db.delete(wf)
    db.commit()

    return {"code": 200, "msg": "删除成功"}


@router.get("/{workflow_id}/run")
def run_workflow(workflow_id: str, db: Session = Depends(get_db)):
    # 1. 生成本次运行的唯一 ID
    run_id = str(uuid.uuid4())

    # 2. 准备参数
    # 假设你的 main_task_entry 需要一些参数，可以在这里准备

    # 1. 查询数据库
    wf = db.query(Workflow).filter(Workflow.id == workflow_id).first()

    # 2. 如果找不到，抛出 404 错误
    if not wf:
        raise HTTPException(status_code=404, detail="工作流不存在")

    try:
        nodes_json = json.loads(wf.nodes) if wf.nodes else {}
    except json.JSONDecodeError:
        nodes_json = {}  # 防止数据库里存了脏数据导致报错
    data = {
        "id": wf.id,
        "name": wf.name,
        "nodes": nodes_json,  #
        "updated_at": wf.updated_at
    }
    # 3. 创建子进程
    # target 指向包装器，args 把真正的脚本函数传进去
    p = Process(
        target=process_runner_wrapper,
        args=(data, run_id, workflow_id)
    )

    # 4. 启动进程 (非阻塞，瞬间完成)
    p.start()

    # 5. 立刻返回，前端不需要等待脚本跑完
    return {
        "code": 200,
        "message": "Task started",
        "run_id": run_id,
        "pid": p.pid
    }



@router.get("/dom")
def get_dom():
    # 1. 生成本次运行的唯一 ID
    run_id = str(uuid.uuid4())

    # 2. 如果找不到，抛出 404 错误

    data = {
        "id": run_id,
        "name": "获取DOM树结构",
        "nodes": {
            "public-trigger-1765888423180": {
                "id": "public-trigger-1765888423180",
                "nodeType": 200,
                "nodeCode": "public/trigger",
                "displayName": "开始节点",
                "lastCodes": [],
                "nextCodes": [
                    "mobile-dump_dom-1765888423180"
                ],
                "data": {
                    "label": "开始"
                },
                "platform": "mobile"
            },
            "mobile-dump_dom-1765888423180": {
                "id": "mobile-dump_dom-1765888423180",
                "nodeType": 200,
                "nodeCode": "mobile/dump_dom",
                "displayName": "获取DOM",
                "lastCodes": ["public-trigger-1765888423180"],
                "nextCodes": ["api-request-1765888423180"],
                "data": {
                    "label": "获取DOM"
                },
                "platform": "mobile"
            },
            "api-request-1765888423180": {
                "id": "api-request-1765888423180",
                "nodeType": 200,
                "nodeCode": "api/request",
                "displayName": "发送请求",
                "lastCodes": ["mobile-dump_dom-1765888423180"],
                "nextCodes": [],
                "data": {
                    "label": "发送请求",
                    'server_url': 'http://127.0.0.1:8000/api/request',
                    'http_method': 'POST',
                    'request_body_type': 'json',
                    'request_body_json': '{"dom": "{{mobile-dump_dom-1765888423180.dom}}"}',
                    'request_headers': {
                        'Accept': 'application/json',
                    }
                },
                "platform": "api"
            }
        },
    }
    # 3. 创建子进程
    # target 指向包装器，args 把真正的脚本函数传进去
    p = Process(
        target=process_runner_wrapper,
        args=(data, run_id, "dump_dom")
    )

    # 4. 启动进程 (非阻塞，瞬间完成)
    p.start()

    # 5. 立刻返回，前端不需要等待脚本跑完
    return {
        "code": 200,
        "message": "Task started",
        "run_id": run_id,
        "pid": p.pid
    }