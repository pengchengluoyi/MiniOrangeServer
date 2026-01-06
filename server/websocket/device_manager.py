# !/usr/bin/env python
# -*-coding:utf-8 -*-

import json
from typing import Dict
from fastapi import WebSocket
from sqlalchemy.orm import sessionmaker
from datetime import datetime

from server.core.database import engine
from server.models.mDevice import MDevice, MDeviceLog
from script.log import SLog

# 创建会话工厂
SessionLocal = sessionmaker(bind=engine)

class DeviceManager:
    _instance = None
    
    # 内存中维护活跃连接: { "device_sn": WebSocket }
    active_connections: Dict[str, WebSocket] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DeviceManager, cls).__new__(cls)
        return cls._instance

    async def register(self, websocket: WebSocket, data: dict):
        """处理设备注册 (对应 wsMap 中的 register)"""
        sn = data.get("sn")
        if not sn:
            return {"code": 400, "msg": "Missing SN"}
        
        # 1. 建立内存映射
        self.active_connections[sn] = websocket
        
        # 2. 数据库注册/更新
        self._register_device(sn, data)
        
        # 3. 记录日志
        self._save_log(sn, "receive", "register", json.dumps(data))
        
        return {"code": 200, "msg": "Registered successfully"}

    async def heartbeat(self, websocket: WebSocket, data: dict):
        """处理心跳 (对应 wsMap 中的 heartbeat)"""
        sn = data.get("sn")
        if sn:
            self._update_device_status(sn, "online")
        return None  # 心跳通常不需要回复内容，或者回复简单的 ack

    async def disconnect(self, websocket: WebSocket, data: dict):
        """处理断开连接 (对应 wsMap 中的 disconnect)"""
        # 因为 disconnect 事件传来的 data 通常为空，我们需要反查 websocket 对应的 SN
        target_sn = None
        for sn, ws in self.active_connections.items():
            if ws == websocket:
                target_sn = sn
                break
        
        if target_sn:
            del self.active_connections[target_sn]
            self._update_device_status(target_sn, "offline")
            SLog.i("DeviceManager", f"Device disconnected: {target_sn}")

    async def send_command(self, sn: str, command: str, params: dict = None):
        """给设备发送指令"""
        if sn not in self.active_connections:
            SLog.w("DeviceManager", f"Device {sn} is offline")
            return False
        
        msg = {
            "type": "command",
            "command": command,
            "params": params or {},
            "timestamp": datetime.now().isoformat()
        }

        msg_str = json.dumps(msg)

        SLog.i("DeviceManager", f"msg_str {msg_str}")
        try:
            await self.active_connections[sn].send_text(msg_str)
            self._save_log(sn, "send", "command", msg_str)
            return True
        except Exception as e:
            SLog.e("DeviceManager", f"Send command failed: {e}")
            return False

    # --- 数据库操作 ---

    def _update_device_status(self, sn: str, status: str):
        try:
            with SessionLocal() as db:
                device = db.query(MDevice).filter(MDevice.sn == sn).first()
                if device:
                    device.status = status
                    if status == "online":
                        device.last_online_time = datetime.now()
                    db.commit()
        except Exception as e:
            SLog.e("DeviceManager", f"DB Error update status: {e}")

    def _register_device(self, sn: str, info: dict):
        try:
            with SessionLocal() as db:
                device = db.query(MDevice).filter(MDevice.sn == sn).first()
                if not device:
                    device = MDevice(sn=sn)
                    db.add(device)
                
                # 更新字段
                device.device_type = info.get("type", "unknown")
                device.model = info.get("model")
                device.ip_address = info.get("ip")
                device.mac_address = info.get("mac")
                device.os_version = info.get("os_version")
                device.resolution = info.get("resolution")
                device.status = "online"
                device.last_online_time = datetime.now()
                db.commit()
                SLog.i("DeviceManager", f"Device registered/updated: {sn}")
        except Exception as e:
            SLog.e("DeviceManager", f"DB Error register: {e}")

    def _save_log(self, sn: str, direction: str, msg_type: str, content: str):
        try:
            with SessionLocal() as db:
                log = MDeviceLog(sn=sn, direction=direction, type=msg_type, content=content)
                db.add(log)
                db.commit()
        except Exception as e:
            SLog.e("DeviceManager", f"DB Error save log: {e}")
