# !/usr/bin/env python
# -*-coding:utf-8 -*-

from typing import List, Optional, Dict, Any
from fastapi import APIRouter
from pydantic import BaseModel
from server.websocket.device_manager import DeviceManager, SessionLocal
from server.models.mDevice import MDevice
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
    last_online: Optional[str] = None

class CommandReq(BaseModel):
    sn: str
    command: str
    params: Dict[str, Any] = {}

class SetPasswordReq(BaseModel):
    sn: str
    password: str

@router.get("/list", response_model=List[DeviceInfo])
def get_device_list():
    """获取设备列表"""
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
                "role": d.role,
                "password": d.password,
                "last_online": str(d.last_online_time) if d.last_online_time else None
            })
        return result
    except Exception as e:
        SLog.e("rDevice", f"Get list error: {e}")
        return []
    finally:
        session.close()

@router.post("/command")
async def send_command(req: CommandReq):
    """下发指令"""
    manager = DeviceManager()
    
    if req.sn not in manager.active_connections:
        return {"code": 400, "msg": "Device is offline"}
        
    success = await manager.send_command(req.sn, req.command, req.params)
    if success:
        return {"code": 200, "msg": "Command sent"}
    else:
        return {"code": 500, "msg": "Failed to send"}

@router.post("/set_password")
def set_device_password(req: SetPasswordReq):
    """设置设备解锁密码"""
    session = SessionLocal()
    try:
        device = session.query(MDevice).filter(MDevice.sn == req.sn).first()
        if not device:
            return {"code": 404, "msg": "Device not found"}
        
        device.password = req.password
        session.commit()
        return {"code": 200, "msg": "Password updated"}
    except Exception as e:
        SLog.e("rDevice", f"Set password error: {e}")
        return {"code": 500, "msg": f"Error: {e}"}
    finally:
        session.close()