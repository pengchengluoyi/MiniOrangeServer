# !/usr/bin/env python
# -*-coding:utf-8 -*-

from typing import List, Optional, Dict, Any
from fastapi import APIRouter
from pydantic import BaseModel
from server.websocket.device_manager import DeviceManager
from server.services.device_service import DeviceService
from script.log import SLog

router = APIRouter(prefix="/device", tags=["Device"])

class DeviceInfo(BaseModel):
    sn: str
    type: str
    model: Optional[str] = None
    ip: Optional[str] = None
    status: str
    role: Optional[str] = "node"
    password: Optional[str] = None
    app_version: Optional[str] = None
    last_online: Optional[str] = None
    # v3: 通道/指纹信息（与 WS device_list_update 广播对齐）
    channels: Optional[Dict[str, Any]] = None
    control_channel: Optional[str] = None
    fingerprint_id: Optional[str] = None
    clawnode_id: Optional[str] = None
    adb_sn: Optional[str] = None

class CommandReq(BaseModel):
    sn: str
    command: str
    params: Dict[str, Any] = {}
    wait_result: bool = True
    wait_timeout: float = 90.0

class SetPasswordReq(BaseModel):
    sn: str
    password: str

@router.get("/list", response_model=List[DeviceInfo])
def get_device_list():
    """获取设备列表"""
    try:
        result = []
        dm = DeviceManager()
        from server.services.runtime.channels import read_channels, channels_to_brief, resolve_control_channel
        for d in DeviceService.list_all():
            meta = dm.device_meta.get(d.sn, {})
            result.append({
                "sn": d.sn,
                "type": d.device_type,
                "model": d.model,
                "ip": d.ip_address,
                "status": d.status,
                "role": d.role,
                "password": d.password,
                "app_version": meta.get("app_version"),
                "last_online": str(d.last_online_time) if d.last_online_time else None,
                "channels": channels_to_brief(read_channels(d)),
                "control_channel": resolve_control_channel(d).get("channel"),
                "fingerprint_id": getattr(d, "fingerprint_id", None),
                "clawnode_id": getattr(d, "clawnode_id", None),
                "adb_sn": getattr(d, "adb_sn", None),
            })
        return result
    except Exception as e:
        SLog.e("rDevice", f"Get list error: {e}")
        return []

@router.post("/command")
async def send_command(req: CommandReq):
    """下发指令"""
    manager = DeviceManager()
    
    if req.sn not in manager.active_connections:
        return {"code": 400, "msg": "Device is offline"}
        
    timeout = req.wait_timeout if req.wait_result else None
    out = await manager.send_command(
        req.sn, req.command, req.params, wait_timeout=timeout,
    )
    if isinstance(out, dict):
        if not out.get("sent"):
            return {"code": 500, "msg": out.get("error") or "Failed to send", **out}
        device = out.get("device") or {}
        status = str(device.get("status") or "").lower()
        ok = status == "success" or (status == "" and not device.get("stderr"))
        msg = device.get("message") or device.get("stdout") or out.get("error") or "Command sent"
        if out.get("timeout"):
            return {
                "code": 202,
                "msg": "sent but device response timeout",
                "trace_id": out.get("trace_id"),
                "timeout": True,
            }
        return {
            "code": 200 if ok else 500,
            "msg": msg,
            "ok": ok,
            "trace_id": out.get("trace_id"),
            "device": device,
        }
    if out:
        return {"code": 200, "msg": "Command sent"}
    return {"code": 500, "msg": "Failed to send"}

@router.post("/set_password")
def set_device_password(req: SetPasswordReq):
    """设置设备解锁密码"""
    try:
        if not DeviceService.set_password(req.sn, req.password):
            return {"code": 404, "msg": "Device not found"}
        return {"code": 200, "msg": "Password updated"}
    except Exception as e:
        SLog.e("rDevice", f"Set password error: {e}")
        return {"code": 500, "msg": f"Error: {e}"}