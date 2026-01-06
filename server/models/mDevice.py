# !/usr/bin/env python
# -*-coding:utf-8 -*-

from sqlalchemy import Column, String, Integer, DateTime, Text
from sqlalchemy.sql import func
from server.core.database import Base

class MDevice(Base):
    """
    设备表: 存储所有接入设备的信息
    """
    __tablename__ = "m_device"

    sn = Column(String, primary_key=True, index=True, comment="设备唯一标识SN")
    device_type = Column(String, nullable=False, comment="设备类型: android, ios, pc, mac")
    model = Column(String, nullable=True, comment="设备型号")
    
    # 网络信息
    ip_address = Column(String, nullable=True, comment="IP地址")
    mac_address = Column(String, nullable=True, comment="物理MAC地址")
    
    # 系统信息
    os_version = Column(String, nullable=True, comment="系统版本")
    resolution = Column(String, nullable=True, comment="屏幕分辨率")
    
    # 状态管理
    status = Column(String, default="offline", comment="状态: online, offline, busy, error")
    owner = Column(String, nullable=True, comment="当前占用者/任务ID")
    
    # 时间戳
    last_online_time = Column(DateTime(timezone=True), onupdate=func.now(), comment="最后在线时间")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class MDeviceLog(Base):
    """
    设备指令日志: 记录服务端与设备的交互历史
    """
    __tablename__ = "m_device_log"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    sn = Column(String, index=True, comment="关联设备SN")
    direction = Column(String, comment="方向: send(服务端发), receive(设备回)")
    type = Column(String, comment="消息类型: command, heartbeat, register, response")
    content = Column(Text, comment="消息内容JSON")
    created_at = Column(DateTime(timezone=True), server_default=func.now())