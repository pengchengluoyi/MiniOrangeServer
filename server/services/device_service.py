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


def _normalize_model(model: Optional[str]) -> str:
    """提取型号标识，用于合并 USB hub 与 ClawNode 重复条目。"""
    if not model:
        return ""
    import re

    text = re.sub(r"[^A-Z0-9]+", " ", str(model).strip().upper())
    tokens = [t for t in text.split() if t]
    if not tokens:
        return ""

    series: set[str] = set()
    for tok in tokens:
        if re.fullmatch(r"[A-Z]{1,4}\d{2,}[A-Z0-9]*", tok):
            series.add(tok)
        elif re.fullmatch(r"\d{5,}[A-Z0-9]*", tok):
            series.add(tok)
    if series:
        return "|".join(sorted(series))

    brands = {
        "MOTOROLA", "MOTO", "XIAOMI", "REDMI", "SAMSUNG", "HUAWEI", "HONOR",
        "GOOGLE", "ONEPLUS", "OPPO", "VIVO", "REALME", "ANDROID", "IPHONE", "APPLE",
    }
    rest = [t for t in tokens if t not in brands and len(t) >= 2]
    return " ".join(rest) if rest else " ".join(tokens)


def _is_claw_direct(device: MDevice) -> bool:
    sn = str(device.sn or "")
    return sn.startswith("claw-") or str(device.device_type or "") == "android_direct"


def dedupe_devices(devices: List[MDevice]) -> List[MDevice]:
    """
    同一台手机可能同时以 USB hub（adb serial）和 ClawNode WS（claw-*）注册。
    展示时保留 ClawNode 直连，隐藏重复的 hub 条目。
    """
    claws_by_model: dict[str, MDevice] = {}
    claws_by_ip: dict[str, MDevice] = {}
    for device in devices:
        if not _is_claw_direct(device):
            continue
        model_key = _normalize_model(device.model)
        if model_key:
            claws_by_model[model_key] = device
        ip = str(device.ip_address or "").strip()
        if ip and ip.upper() != "USB":
            claws_by_ip[ip] = device

    skip_sns: set[str] = set()
    for device in devices:
        if str(device.device_type or "") != "android" or str(device.role or "") != "hub":
            continue
        model_key = _normalize_model(device.model)
        ip = str(device.ip_address or "").strip()
        if model_key and model_key in claws_by_model:
            skip_sns.add(str(device.sn))
        elif ip and ip in claws_by_ip:
            skip_sns.add(str(device.sn))

    return [device for device in devices if str(device.sn) not in skip_sns]


def remove_duplicate_hubs_for_claw(
    claw_sn: str,
    model: Optional[str] = "",
    ip: Optional[str] = "",
    db=None,
) -> List[str]:
    """ClawNode 添加/上线时删除同机型的 USB hub 重复行。"""
    if not claw_sn or not str(claw_sn).startswith("claw-"):
        return []
    model_key = _normalize_model(model)
    device_ip = str(ip or "").strip()
    close = db is None
    if close:
        db = SessionLocal()
    removed: List[str] = []
    try:
        for device in db.query(MDevice).all():
            if str(device.sn) == claw_sn:
                continue
            if str(device.device_type or "") != "android" or str(device.role or "") != "hub":
                continue
            hub_model_key = _normalize_model(device.model)
            hub_ip = str(device.ip_address or "").strip()
            if model_key and hub_model_key == model_key:
                removed.append(str(device.sn))
                db.delete(device)
            elif device_ip and hub_ip and device_ip == hub_ip:
                removed.append(str(device.sn))
                db.delete(device)
        if removed:
            db.commit()
    except Exception:
        if close:
            db.rollback()
        raise
    finally:
        if close:
            db.close()
    return removed


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
    def list_all(db=None, *, dedupe: bool = True) -> List[MDevice]:
        close = db is None
        if close:
            db = SessionLocal()
        try:
            devices = [d for d in db.query(MDevice).all() if is_valid_sn(d.sn)]
            return dedupe_devices(devices) if dedupe else devices
        finally:
            if close:
                db.close()
