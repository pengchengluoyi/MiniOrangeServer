# !/usr/bin/env python
# -*-coding:utf-8 -*-

from sqlalchemy import Column, String, Integer, DateTime, Text, JSON
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
    
    # 🚀 新增字段
    role = Column(String, default="node", comment="角色: node, hub, client")
    password = Column(String, nullable=True, comment="设备解锁密码")
    
    # 状态管理
    status = Column(String, default="offline", comment="主状态: online, offline, busy, error")
    owner = Column(String, nullable=True, comment="当前占用者/任务ID")

    # 子通道状态 (Step 2)：以 JSON 持久化，供 Capability Router / 设备列表 UI 使用。
    # 结构见 server/services/runtime/channels.py DEFAULT_CHANNELS。
    #   {
    #     "remote": { "state": connected|disconnected|auth_failed|unpaired,
    #                 "last_heartbeat_at": iso, "auth_state": str, "details": str },
    #     "adb":    { "state": connected|disconnected|unauthorized|not_applicable,
    #                 "last_probe_at": iso, "transport": usb|tcp, "serial": str }
    #   }
    channels = Column(JSON, default=dict, comment="子通道状态: { remote: {...}, adb: {...} }")

    # 设备指纹绑定 (v3)：跨连接方式(ClawNode / adb)标识同一物理设备。
    #   - hw_uid       设备真实唯一标识：安卓=SN(ro.serialno)、iOS=DID(UDID)，是合并键
    #   - fingerprint_id 由 hw_uid 按平台派生的稳定指纹，逻辑设备身份锚点
    #   - clawnode_id  绑定的 ClawNode 连接句柄 (claw-xxx)
    #   - adb_sn       绑定的 adb 连接句柄 (adb serial 或 ip:port)
    fingerprint_id = Column(String, nullable=True, index=True, comment="物理设备指纹(跨连接合并锚点)")
    hw_uid = Column(String, nullable=True, index=True, comment="设备真实唯一标识: 安卓SN / iOS DID")
    clawnode_id = Column(String, nullable=True, comment="绑定的ClawNode连接句柄 claw-xxx")
    adb_sn = Column(String, nullable=True, comment="绑定的adb连接句柄 serial/ip:port")

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