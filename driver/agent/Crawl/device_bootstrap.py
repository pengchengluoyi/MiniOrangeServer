# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""跑图专用：初始化移动端引擎（不依赖工作流 Checklist）。"""
from __future__ import annotations

import builtins
from typing import Optional, Tuple

from script.log import SLog
from driver.agent.Common.task_details import TaskDetails
from driver.agent.Memory import memory_manager
from driver.tentacle.manager import Manager

TAG = "CrawlDeviceBootstrap"


def list_connected_adb_serials() -> list:
    """本机 adb devices 中 state=device 的序列号。"""
    try:
        import adbutils

        client = adbutils.AdbClient()
        serials = []
        for info in client.list():
            if getattr(info, "state", None) == "device" and info.serial:
                serials.append(str(info.serial))
        return serials
    except Exception as e:
        SLog.w(TAG, f"adbutils list devices failed: {e}")
        return []


def resolve_mobile_serial(node_sn: str, platform: str = "android") -> str:
    """
    解析用于 ADB/WDA 的手机序列号。
    - 优先 adb devices 里在线的真机；
    - PC 节点（Driver 客户端 SN）不能当作 adb -s 使用。
    """
    adb_serials = list_connected_adb_serials()
    if node_sn and node_sn in adb_serials:
        return str(node_sn)

    try:
        from server.services.device_service import DeviceService

        dev = DeviceService.get_by_sn(node_sn) if node_sn else None
        dt = (dev.device_type or "").lower() if dev else ""

        if dt == "pc" or (node_sn and node_sn not in adb_serials):
            if adb_serials:
                chosen = adb_serials[0]
                SLog.i(TAG, f"Driver node {node_sn} -> adb device {chosen}")
                return chosen

        if dt in ("android", "ios") and node_sn in adb_serials:
            return str(node_sn)

        pick_type = platform if platform in ("ios", "android") else "android"
        picked = DeviceService.pick_sn(device_type=pick_type)
        if picked and picked in adb_serials:
            return str(picked)
    except Exception as e:
        SLog.w(TAG, f"resolve_mobile_serial db fallback: {e}")

    if adb_serials:
        return adb_serials[0]

    return str(node_sn or "")


def bootstrap_mobile_engine(
    node_sn: str,
    platform: str = "android",
) -> Tuple[object, Tuple[int, int]]:
    """
    初始化 Manager + MobileEngine，返回 (engine, (width, height))。
    """
    mobile_sn = resolve_mobile_serial(node_sn, platform)
    builtins.TARGET_DEVICE_SN = mobile_sn
    memory_manager.short_term.set_global("platform", platform)
    memory_manager.short_term.set_global("run_device_sn", mobile_sn)

    info = TaskDetails({
        "id": "sys_crawl_bootstrap",
        "nodeCode": "tools/screenshot",
        "nodeType": 200,
        "displayName": "跑图设备初始化",
        "platform": platform,
        "data": {"platform": platform},
        "lastCodes": [],
        "nextCodes": [],
    })

    mgr = Manager()
    mgr.online(info)
    engine = mgr.MobileEngine
    if engine is None:
        raise RuntimeError(
            f"无法初始化移动端引擎（node={node_sn}, mobile_sn={mobile_sn}）。"
            "请确认手机已 USB 连接且 adb devices 可见。"
        )

    if hasattr(engine, "screen_on"):
        engine.screen_on()

    if platform == "android" and hasattr(engine, "ensure_input_ready"):
        engine.ensure_input_ready()
        mode = getattr(engine, "_input_mode", "unknown")
        SLog.i(TAG, f"Android input mode={mode}")

    if hasattr(engine, "screen_size"):
        w, h = engine.screen_size()
    elif hasattr(engine, "_display_size"):
        w, h = engine._display_size()
    else:
        w, h = 1080, 1920

    SLog.i(TAG, f"Mobile engine ready sn={mobile_sn} screen={w}x{h}")
    return engine, (w, h)
