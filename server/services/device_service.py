# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
设备数据访问：统一走 m_device 表（与平台无关的 sn / device_type / password）。
供 HTTP、WebSocket、子进程 in_process、Driver 共用。
"""
from __future__ import annotations

from typing import List, Optional

from server.core.database import SessionLocal
from server.models.mDevice import MDevice

# hub 占位 / tidevice 表头行，非真实设备
_INVALID_SNS = frozenset({"UDID", "NAME", "SERIALNUMBER", "DEVICE"})


def is_valid_sn(sn: Optional[str]) -> bool:
    """过滤 tidevice 表头、占位符等伪设备 sn。"""
    if not sn:
        return False
    key = str(sn).strip().upper()
    if key in _INVALID_SNS:
        return False
    if len(key) < 8:
        return False
    return True


class DeviceService:
    @staticmethod
    def get_by_sn(sn: str, db=None) -> Optional[MDevice]:
        if not sn:
            return None
        close = db is None
        if close:
            db = SessionLocal()
        try:
            return db.query(MDevice).filter(MDevice.sn == sn).first()
        finally:
            if close:
                db.close()

    @staticmethod
    def get_password(sn: str) -> Optional[str]:
        device = DeviceService.get_by_sn(sn)
        if not device or not device.password:
            return None
        return str(device.password).strip() or None

    @staticmethod
    def set_password(sn: str, password: str, db=None) -> bool:
        close = db is None
        if close:
            db = SessionLocal()
        try:
            device = db.query(MDevice).filter(MDevice.sn == sn).first()
            if not device:
                return False
            device.password = password
            db.commit()
            return True
        except Exception:
            if close:
                db.rollback()
            raise
        finally:
            if close:
                db.close()

    @staticmethod
    def list_by_type(device_type: Optional[str] = None, db=None) -> List[MDevice]:
        """按 m_device.device_type 过滤（ios / android / pc …）；不传则返回全部有效设备。"""
        close = db is None
        if close:
            db = SessionLocal()
        try:
            q = db.query(MDevice)
            if device_type:
                q = q.filter(MDevice.device_type == device_type)
            return [d for d in q.all() if is_valid_sn(d.sn)]
        finally:
            if close:
                db.close()

    @staticmethod
    def pick_sn(
        preferred_sn: Optional[str] = None,
        device_type: Optional[str] = None,
    ) -> Optional[str]:
        """
        解析本次运行使用的设备 sn。
        优先级：显式 preferred_sn > 库内同类型设备（online → busy → offline）> 任意一台同类型。
        """
        if preferred_sn and is_valid_sn(preferred_sn):
            return str(preferred_sn)
        devices = DeviceService.list_by_type(device_type)
        for status in ("online", "busy", "offline"):
            for dev in devices:
                if dev.status == status:
                    return dev.sn
        return devices[0].sn if devices else None

    @staticmethod
    def list_all(db=None) -> List[MDevice]:
        close = db is None
        if close:
            db = SessionLocal()
        try:
            return [d for d in db.query(MDevice).all() if is_valid_sn(d.sn)]
        finally:
            if close:
                db.close()
