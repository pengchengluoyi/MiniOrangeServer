# server/websocket/ws_handlers.py
# !/usr/bin/env python
# -*-coding:utf-8 -*-

import uuid
import json
import os
import base64
from sqlalchemy.orm import joinedload
from sqlalchemy import func, desc
from server.websocket.device_manager import DeviceManager, SessionLocal
from server.models.mDevice import MDevice
from server.models.workflow import Workflow
from server.models.timeline import TaskTimeline
from server.models.AppGraph.app_structure import AppGraph, AppNode, AppSOP
from server.models.AppGraph.app_component import AppComponent
from script.mPath import get_final_path
from script.log import SLog
from server.core.local_brain import LocalBrain


async def handle_get_device_list(websocket, data: dict):
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


async def handle_get_timeline_list(websocket, data: dict):
    """
    获取时间线列表 (分页 + 聚合)
    """
    page = int(data.get("page", 1))
    page_size = int(data.get("page_size", 20))

    session = SessionLocal()
    try:
        # 聚合查询: 按 run_id 分组，获取开始/结束时间和事件数
        # 注意: run_id 已建立索引，有助于分组性能
        query = session.query(
            TaskTimeline.run_id,
            func.min(TaskTimeline.timestamp).label('start_time'),
            func.max(TaskTimeline.timestamp).label('end_time'),
            func.count(TaskTimeline.id).label('event_count')
        ).group_by(TaskTimeline.run_id)

        # 获取总数
        total = query.count()

        # 排序: 按最后更新时间倒序
        query = query.order_by(desc('end_time'))

        # 分页
        results = query.offset((page - 1) * page_size).limit(page_size).all()

        data_list = []
        for row in results:
            data_list.append({
                "run_id": row.run_id,
                "start_time": row.start_time,
                "end_time": row.end_time,
                "event_count": row.event_count
            })

        response = {
            "code": 200,
            "data": {
                "list": data_list,
                "total": total,
                "page": page,
                "page_size": page_size
            },
        }
        return response
    except Exception as e:
        SLog.e("wsHandlers", f"Get timeline list error: {e}")
        response = {"code": 500, "msg": str(e)}
        return response
    finally:
        session.close()


async def handle_run_workflow(websocket, data: dict):
    SLog.i("handle_run_workflow", data)
    sn = data.get("sn")
    flow_id = data.get("flow_id")

    if not sn: return {"code": 400, "msg": "Missing SN"}
    if not flow_id: return {"code": 400, "msg": "Missing flow_id"}

    session = SessionLocal()
    try:
        wf = session.query(Workflow).filter(Workflow.id == flow_id).first()
        if not wf:
            return {"code": 404, "msg": "Workflow not found"}

    except Exception as e:
        SLog.e("wsHandlers", f"Query workflow error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()

    params = {
        "run_id": data.get("run_id") or str(uuid.uuid4()),
        "flow_id": flow_id,
        "target_sn": sn
    }

    success = await DeviceManager().send_command(sn, "run_task", params)

    if success:
        return {"code": 200, "msg": "Command sent", "run_id": params["run_id"]}
    else:
        return {"code": 500, "msg": "Failed to send command"}


async def handle_get_workflow_detail(websocket, data: dict):
    flow_id = data.get("flow_id")

    if not flow_id: return {"code": 400, "msg": "Missing flow_id"}

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

    return {"code": 200, "msg": "Success", "data": run_data}


async def handle_get_app_graph(websocket, data: dict):
    flow_id = data.get("flow_id")

    if not flow_id: return {"code": 400, "msg": "Missing flow_id"}

    session = SessionLocal()
    try:
        # 修正：根据 Workflow -> SOP -> AppGraph 反查
        # AppNode.workflow_id 已废弃，现在 Workflow 挂载在 SOP 下
        graph = session.query(AppGraph).join(AppSOP).join(Workflow).filter(Workflow.id == flow_id).options(
            joinedload(AppGraph.nodes).joinedload(AppNode.components),
            joinedload(AppGraph.edges)
        ).first()

        if not graph:
            return {"code": 200, "data": {"nodes": [], "edges": []}}

        # 手动序列化以避免 SQLAlchemy 对象的循环引用 (Graph -> Node -> Component -> Graph)
        nodes_data = []
        for node in graph.nodes:
            # 1. 序列化组件 (切断反向引用)
            if node.type != "case":
                comps_list = []
                for comp in node.components:
                    comps_list.append({
                        "uid": comp.uid,
                        "label": comp.label,
                        "category": comp.category,
                        "sub_type": comp.sub_type,
                        "rules": comp.rules,
                        "x": comp.x, "y": comp.y, "width": comp.width, "height": comp.height
                    })

                node_payload = {
                    "id": node.node_id,
                    "label": node.label,
                    "type": node.type,
                    "screenshot": node.screenshot,
                    "components": comps_list,
                    # 兼容字段
                    "anchors": [{"uid": c["uid"], "type": c["sub_type"], "value": c["label"], "rect": [c["x"], c["y"], c["width"], c["height"]]} for c in comps_list if c["category"] == "anchor"],
                    "mask_areas": [{"rect": [c["x"], c["y"], c["width"], c["height"]]} for c in comps_list if c["category"] == "mask"]
                }
                nodes_data.append(node_payload)

        return {
            "code": 200,
            "data": {
                "nodes": nodes_data,
                "edges": [{"source": e.source, "target": e.target, "trigger": e.trigger} for e in graph.edges]
            }
        }
    except Exception as e:
        SLog.e("WsHandlers", f"Get Graph Error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_get_world_model(websocket, data: dict):
    return {
        "code": 200,
        "data": {
            "system_reflexes": [
                {
                    "id": "SYS_UNLOCK",
                    "priority": 100,
                    "trigger": {
                        "logic": "OR",
                        "conditions": [
                            {"type": "text", "value": "滑动来解锁"},
                            {"type": "visual", "value": "ICON_LOCK_CLOSED"}
                        ]
                    },
                    "action": {
                        "component": "public/gesture",
                        "params": {"sub_type": "drag", "start_ref": "bottom", "end_offset": [0, -0.4]}
                    }
                }
            ],
            "category_knowledge": {}
        }
    }


async def handle_ask_local_ai(websocket, data: dict):
    screenshot_b64 = data.get("screenshot")
    if not screenshot_b64: return {"code": 400, "msg": "No screenshot"}

    result = LocalBrain().analyze_ui(screenshot_b64)
    if result:
        return {"code": 200, "data": result}
    else:
        return {"code": 500, "msg": "AI inference failed"}


async def handle_get_component(websocket, data: dict):
    uid = data.get("uid")
    if not uid: return {"code": 400, "msg": "Missing uid"}

    session = SessionLocal()
    try:
        comp = session.query(AppComponent).filter(AppComponent.uid == uid).first()
        if not comp: return {"code": 404, "msg": "Component not found"}

        info = {
            "uid": comp.uid,
            "label": comp.label,
            "x": comp.x, "y": comp.y, "width": comp.width, "height": comp.height,
            "screenshot_b64": None
        }
        if comp.node and comp.node.screenshot:
            path = get_final_path(comp.node.screenshot)
            if os.path.exists(path):
                with open(path, "rb") as f:
                    info["screenshot_b64"] = base64.b64encode(f.read()).decode('utf-8')
        return {"code": 200, "data": info}
    except Exception as e:
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_sync_timeline(websocket, data: dict):
    """
    保存任务执行的时间线数据
    客户端需先调用 upload 接口上传图片，将 PIL 对象替换为 URL 后再调用此接口
    """
    run_id = data.get("run_id")
    timeline = data.get("timeline", {})  # 预期格式: {'timestamp': {'type':..., 'data':...}}

    if not run_id or not timeline:
        return {"code": 400, "msg": "Missing run_id or timeline data"}

    session = SessionLocal()
    try:
        # 遍历字典，timeline 是以时间戳为 Key 的字典
        for ts, item in timeline.items():
            record = TaskTimeline(
                run_id=str(run_id),
                timestamp=int(ts),
                event_type=item.get("type"),
                event_data=str(item.get("data"))  # 存 URL 字符串或坐标字符串
            )
            session.add(record)
        
        session.commit()
        return {"code": 200, "msg": "Timeline synced"}
    except Exception as e:
        session.rollback()
        SLog.e("wsHandlers", f"Sync timeline error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_get_device_password(websocket, data: dict):
    sn = data.get("sn")
    if not sn: return {"code": 400, "msg": "Missing SN"}

    session = SessionLocal()
    try:
        device = session.query(MDevice).filter(MDevice.sn == sn).first()
        if not device: return {"code": 404, "msg": "Device not found"}
        return {"code": 200, "data": {"password": device.password}}
    except Exception as e:
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_set_device_password(websocket, data: dict):
    """设置设备解锁密码"""
    sn = data.get("sn")
    password = data.get("password")
    if not sn: return {"code": 400, "msg": "Missing SN"}
    session = SessionLocal()
    try:
        device = session.query(MDevice).filter(MDevice.sn == sn).first()
        if not device:
            return {"code": 404, "msg": "Device not found"}

        device.password = password
        session.commit()
        return {"code": 200, "msg": "Password updated"}
    except Exception as e:
        SLog.e("rDevice", f"Set password error: {e}")
        return {"code": 500, "msg": f"Error: {e}"}
    finally:
        session.close()


async def handle_get_timeline(websocket, data: dict):
    run_id = data.get("run_id")

    if not run_id:
        return {"code": 400, "msg": "Missing run_id"}

    session = SessionLocal()
    try:
        records = session.query(TaskTimeline).filter(
            TaskTimeline.run_id == str(run_id)
        ).order_by(TaskTimeline.timestamp).all()

        result = []
        for r in records:
            result.append({
                "timestamp": r.timestamp,
                "type": r.event_type,
                "data": r.event_data
            })
        SLog.i("wsHandlers", f"Timeline fetched. Count: {len(result)}")

        return {"code": 200, "data": result}
    except Exception as e:
        SLog.e("wsHandlers", f"Get timeline error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()