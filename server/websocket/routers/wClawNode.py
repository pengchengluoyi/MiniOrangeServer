# server/websocket/routers/wClawNode.py
# -*-coding:utf-8 -*-
"""
ClawNode 直连节点适配器。

ClawNode (1.7.23+) 与 server 使用完全相同的命令格式：
  下发：{ "type": "command", "command": "TAP", "params": {...}, "trace_id": "..." }
  回传：{ "trace_id": "...", "type": "ACTION_RESULT", "data": {...} }

EXEC_SCRIPT（1.8.0+）：params { script, language, timeout_ms } 或 script_id + script_vars

本模块负责：
  - ClawNode 注册
  - 结果回传广播
  - 少数控制/流/日志指令的适配（仍输出统一 command+params 格式）
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


async def handle_clawnode_capabilities(websocket: WebSocket, data: dict):
    """
    接收 ClawNode 主动上报或 GET_CAPABILITIES 应答的 CAPABILITIES 帧。
    data 已拍平，含 version_name / version_code / capabilities 等。
    """
    sn = DeviceManager()._get_sn_by_ws(websocket)
    if not sn:
        return None
    trace_id = data.get("trace_id")
    manifest = DeviceManager().ingest_capability_payload(sn, data)
    if trace_id:
        DeviceManager().resolve_command_waiter(trace_id, data)
    SLog.i(
        TAG,
        f"capabilities sn={sn} version={manifest.get('version_name') if manifest else '?'} "
        f"count={len((manifest or {}).get('capabilities') or [])}",
    )
    return None


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
        DeviceManager().resolve_command_waiter(trace_id, data)

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
    把控制类指令翻译成与 server send_command 完全一致的格式：
      { "type": "command", "command": "TAP", "params": {...}, "trace_id": "..." }

    这样 ClawNode 和其他节点收到的命令对象结构完全相同。
    """
    action = (params.get("action") or "").lower()
    trace_id = params.get("req_id") or f"srv-{uuid.uuid4().hex[:12]}"

    def cmd(name, payload):
        return {"type": "command", "command": name, "params": payload, "trace_id": trace_id}

    if action in ("click", "tap"):
        return cmd("TAP", {
            "x": params.get("x"),
            "y": params.get("y"),
            "duration_ms": params.get("duration", 80),
        })

    if action == "swipe":
        x1 = params.get("x1", params.get("x"))
        y1 = params.get("y1", params.get("y"))
        return cmd("SWIPE", {
            "x": x1,
            "y": y1,
            "x2": params.get("x2"),
            "y2": params.get("y2"),
            "duration_ms": params.get("duration", 300),
        })

    if action in ("wake", "wake_up"):
        return cmd("WAKE_UP", {})

    if action in ("key", "keyevent", "key_event", "press_key"):
        kev = params.get("keyevent") or params.get("key") or params.get("event")
        return cmd("KEY_EVENT", {"keyevent": kev})

    if action in ("stop_app", "force_stop", "close_app"):
        return cmd("CLOSE_APP", {"package": params.get("package") or params.get("pkg") or ""})

    if action in ("kill_app", "force_kill"):
        return cmd("KILL_APP", {"package": params.get("package") or params.get("pkg") or ""})

    if action in ("clear_app_cache", "clear_cache"):
        return cmd("CLEAR_APP_CACHE", {"package": params.get("package") or params.get("pkg") or ""})

    if action in ("run_shell", "shell"):
        return cmd("RUN_SHELL", {"command": params.get("command") or params.get("cmd") or ""})

    if action in ("exec_script", "run_script", "exec_code"):
        from server.services.shared.clawnode_script import flatten_capability_params, build_exec_script_command_params
        flat = flatten_capability_params(params)
        try:
            built = build_exec_script_command_params(
                script=str(flat.get("script") or ""),
                script_id=str(flat.get("script_id") or ""),
                language=str(flat.get("language") or ""),
                timeout_ms=int(flat.get("timeout_ms") or 60_000),
                script_vars=flat.get("script_vars") if isinstance(flat.get("script_vars"), dict) else None,
            )
        except ValueError:
            built = {
                "script": flat.get("script") or "",
                "language": flat.get("language") or "dsl",
            }
            if flat.get("timeout_ms") is not None:
                built["timeout_ms"] = flat.get("timeout_ms")
        return cmd("EXEC_SCRIPT", built)

    if action in ("export_logs", "upload_logs"):
        minutes = params.get("minutes")
        p = {}
        if minutes is not None:
            try:
                p["minutes"] = int(minutes)
            except (TypeError, ValueError):
                pass
        return {"type": "command", "command": "EXPORT_LOGS", "params": p, "trace_id": params.get("trace_id") or trace_id}

    if action in ("open_app", "start_app", "launch_app"):
        return cmd("OPEN_APP", {
            "package": params.get("package") or params.get("pkg") or "",
            "activity": params.get("activity"),
        })

    if action in ("set_clipboard", "clipboard"):
        return cmd("SET_CLIPBOARD", {"text": params.get("text") or ""})

    if action in ("input_text", "type_text", "type"):
        payload = {"text": params.get("text") or ""}
        if params.get("x") is not None:
            payload["x"] = params.get("x")
        if params.get("y") is not None:
            payload["y"] = params.get("y")
        return cmd("INPUT_TEXT", payload)

    if action in ("install_apk", "install"):
        p = {"url": params.get("url") or ""}
        file_name = params.get("fileName") or params.get("file_name")
        if file_name:
            p["file_name"] = file_name
        return cmd("INSTALL_APK", p)

    return None


def translate_screenshot_to_clawnode(params: dict) -> dict:
    """单次截图指令翻译。使用与 send_command 相同的格式。"""
    trace_id = params.get("req_id") or f"srv-{uuid.uuid4().hex[:12]}"
    return {
        "type": "command",
        "command": "GET_SCREENSHOT",
        "params": {"quality": params.get("quality", 80)},
        "trace_id": trace_id,
    }


def translate_stream_to_clawnode(start: bool, params: dict) -> dict:
    """推流开关指令翻译。使用与 send_command 相同的格式。"""
    trace_id = params.get("req_id") or f"srv-{uuid.uuid4().hex[:12]}"
    if start:
        return {
            "type": "command",
            "command": "START_STREAM",
            "params": {"fps": params.get("fps", 15)},
            "trace_id": trace_id,
        }
    return {
        "type": "command",
        "command": "STOP_STREAM",
        "params": {},
        "trace_id": trace_id,
    }
