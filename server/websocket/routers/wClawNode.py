# server/websocket/routers/wClawNode.py
# -*-coding:utf-8 -*-
"""
ClawNode 直连节点适配器。

ClawNode 是 Android 端的 headless agent，直接连到服务端 /ws（不经过 PC + adb），
说的是自己的协议方言：下行 {trace_id, action_type, payload}，上行
{trace_id, type, data}。

本模块是 PC Node 协议与 ClawNode 协议之间的“翻译层”，完全独立，
不触碰 driver/client.py 的任何逻辑：
  - handle_clawnode_register: 处理 ClawNode 注册，让它进设备列表；
  - handle_clawnode_result:   接收 ClawNode 回传的截图/动作结果，广播给前端；
  - translate_control_to_clawnode: 把服务端的 control/截图 指令翻译成 ClawNode 方言。
"""

import json
import time
import uuid

from fastapi import WebSocket

from server.websocket.device_manager import DeviceManager
from script.log import SLog

TAG = "wClawNode"


async def handle_clawnode_register(websocket: WebSocket, data: dict):
    """
    ClawNode 注册。data: { sn, type, model, resolution, role, os_version, ... }

    复用 DeviceManager 的注册落库逻辑，同时把该 SN 标记为“直连节点”，
    使后续 control/stream 指令走 ClawNode 协议分支。注册后设备自动出现在
    get_device_list 中，前端无需任何改动。
    """
    sn = data.get("sn")
    if not sn:
        return {"code": 400, "msg": "Missing SN"}

    DeviceManager().register_clawnode(websocket, data)
    SLog.i(TAG, f"ClawNode registered: {sn}")
    return {"code": 200, "msg": "clawnode registered"}


async def handle_clawnode_result(websocket: WebSocket, data: dict):
    """
    接收 ClawNode 回传的 SCREENSHOT_RESULT / ACTION_RESULT / STREAM_* 等。

    注意：rWebsocket.process_message 已把外层 {trace_id,type,data:{...}} 拍平，
    传入的 data 形如 {format, base64_image, trace_id, type, ...}。
    即发即回模式下，这里只把结果广播给前端观察者。
    """
    sn = DeviceManager()._get_sn_by_ws(websocket)
    trace_id = data.get("trace_id")

    # 先喂给等待该 trace_id 的 RemoteEngine（service 层往返），幂等非阻塞
    if trace_id:
        from driver.tentacle.engine.mobile.mRemote import RemoteEngine
        eng = RemoteEngine._by_sn.get(sn)
        if eng:
            eng._resolve(trace_id, data)

    # 再广播给前端观察者（stream/截图预览继续工作）
    payload = {
        "type": data.get("type", "clawnode_result"),
        "data": {
            "sn": sn,
            **{k: v for k, v in data.items() if k != "type"},
        },
    }
    await DeviceManager().broadcast_to_observers(payload, exclude=websocket)
    return None


def translate_control_to_clawnode(params: dict) -> dict:
    """
    把服务端/前端的 control 指令翻译成 ClawNode 方言 {trace_id, action_type, payload}。

    服务端 control 字段：{ action: "click"/"tap"/"swipe", x, y, x1, y1, x2, y2, duration }
    ClawNode 协议见各阶段定义：TAP 用 x/y；SWIPE 用 x/y(起点)+x2/y2(终点)+duration_ms。

    返回 None 表示该动作 ClawNode 不支持。
    """
    action = (params.get("action") or "").lower()
    trace_id = params.get("req_id") or f"srv-{uuid.uuid4().hex[:12]}"

    if action in ("click", "tap"):
        return {
            "trace_id": trace_id,
            "action_type": "TAP",
            "payload": {
                "x": params.get("x"),
                "y": params.get("y"),
                "duration_ms": params.get("duration", 80),
            },
        }

    if action == "swipe":
        # 兼容两种来源字段：x1/y1/x2/y2 或 x/y/x2/y2
        x1 = params.get("x1", params.get("x"))
        y1 = params.get("y1", params.get("y"))
        return {
            "trace_id": trace_id,
            "action_type": "SWIPE",
            "payload": {
                "x": x1,
                "y": y1,
                "x2": params.get("x2"),
                "y2": params.get("y2"),
                "duration_ms": params.get("duration", 300),
            },
        }

    if action in ("wake", "wake_up"):
        return {"trace_id": trace_id, "action_type": "WAKE_UP", "payload": {}}

    if action in ("key", "keyevent", "key_event", "press_key"):
        # 返回键/Home 等：keyevent 可为 back/home 或 Android keycode
        kev = params.get("keyevent") or params.get("key") or params.get("event")
        return {"trace_id": trace_id, "action_type": "KEY_EVENT", "payload": {"keyevent": kev}}

    if action in ("stop_app", "force_stop"):
        return {
            "trace_id": trace_id,
            "action_type": "STOP_APP",
            "payload": {"package": params.get("package") or params.get("pkg") or ""},
        }

    return None


def translate_screenshot_to_clawnode(params: dict) -> dict:
    """单次截图指令翻译。params 可含 quality。"""
    trace_id = params.get("req_id") or f"srv-{uuid.uuid4().hex[:12]}"
    return {
        "trace_id": trace_id,
        "action_type": "GET_SCREENSHOT",
        "payload": {"quality": params.get("quality", 80)},
    }


def translate_stream_to_clawnode(start: bool, params: dict) -> dict:
    """推流开关指令翻译。"""
    trace_id = params.get("req_id") or f"srv-{uuid.uuid4().hex[:12]}"
    if start:
        return {
            "trace_id": trace_id,
            "action_type": "START_STREAM",
            "payload": {"fps": params.get("fps", 15)},
        }
    return {"trace_id": trace_id, "action_type": "STOP_STREAM", "payload": {}}
