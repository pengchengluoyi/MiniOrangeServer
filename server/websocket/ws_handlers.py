# !/usr/bin/env python
# -*-coding:utf-8 -*-

import uuid
import json
from server.websocket.device_manager import DeviceManager, SessionLocal
from server.models.mDevice import MDevice
from server.models.workflow import Workflow
from script.log import SLog

async def handle_get_device_list(websocket, data: dict):
    """
    获取设备列表
    """
    session = SessionLocal()
    try:
        devices = session.query(MDevice).all()
        result = []
        for d in devices:
            result.append({
                "sn": d.sn,
                "type": d.device_type,
                "model": d.model,
                "ip": d.ip_address,
                "status": d.status,
                "last_online": str(d.last_online_time) if d.last_online_time else None
            })
        return {"code": 200, "data": result}
    except Exception as e:
        SLog.e("wsHandlers", f"Get device list error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()

async def handle_run_workflow(websocket, data: dict):
    """
    运行工作流 (下发指令给设备)
    """
    SLog.i("handle_run_workflow", data)
    sn = data.get("sn")
    flow_id = data.get("flow_id")

    if not sn:
        return {"code": 400, "msg": "Missing SN"}
    if not flow_id:
        return {"code": 400, "msg": "Missing flow_id"}

    # 查库获取 run_data
    session = SessionLocal()
    try:
        wf = session.query(Workflow).filter(Workflow.id == flow_id).first()
        if not wf:
            return {"code": 404, "msg": "Workflow not found"}
        
        try:
            nodes_json = json.loads(wf.nodes) if wf.nodes else {}
        except json.JSONDecodeError:
            nodes_json = {}
            
        run_data = {
            "id": wf.id,
            "name": wf.name,
            "nodes": nodes_json,
            "updated_at": str(wf.updated_at) if wf.updated_at else None
        }
    except Exception as e:
        SLog.e("wsHandlers", f"Query workflow error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()

    # 构造下发给设备的参数
    params = {
        "run_id": data.get("run_id") or str(uuid.uuid4()),
        "flow_id": flow_id,
        "run_data": run_data
    }

    SLog.i("handle_run_workflow", params)
    # 通过 DeviceManager 发送指令
    success = await DeviceManager().send_command(sn, "run_task", params)

    if success:
        return {"code": 200, "msg": "Command sent", "run_id": params["run_id"]}
    else:
        return {"code": 500, "msg": "Failed to send command (Device offline?)"}