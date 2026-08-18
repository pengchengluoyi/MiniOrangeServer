# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""iOS 设备发现器：USB(usbmuxd) + 局域网(Bonjour) + 已启动模拟器(simctl)。

不从 USB/Bonjour 收录 CoreDevice `_remotepairing` UUID；模拟器只走 simctl。
"""
from __future__ import annotations

import asyncio
from typing import Dict

from script.log import SLog

TAG = "IosDiscovery"
DISCOVERY_INTERVAL_SEC = 6.0

_last_seen: Dict[str, str] = {}  # udid -> usb|wifi|simulator


def _run_tick() -> bool:
    from server.services.runtime.ios_ids import (
        is_executable_ios_sn,
        is_physical_ios_udid,
        is_rfc4122_uuid,
    )
    from server.services.runtime.ios_bonjour import snapshot_wifi_devices
    from server.services.runtime.ios_simctl import list_booted_simulators
    from server.services.runtime.ios_usbmux import list_usb_ios_devices
    from server.websocket.device_manager import DeviceManager

    manager = DeviceManager()
    present: Dict[str, dict] = {}

    for item in list_usb_ios_devices():
        udid = str(item.get("udid") or "").strip()
        if not udid or is_rfc4122_uuid(udid) or not is_physical_ios_udid(udid):
            continue
        present[udid] = {
            "transport": "usb",
            "model": item.get("name") or "",
            "os": "",
            "ip": "",
        }

    for udid, item in snapshot_wifi_devices().items():
        if is_rfc4122_uuid(udid) or not is_executable_ios_sn(udid):
            continue
        if udid in present:
            if item.get("ip") and not present[udid].get("ip"):
                present[udid]["ip"] = item["ip"]
            continue
        name = str(item.get("name") or "")
        if is_rfc4122_uuid(name):
            name = ""
        present[udid] = {
            "transport": "wifi",
            "model": name,
            "os": "",
            "ip": item.get("ip") or "",
        }

    for item in list_booted_simulators():
        udid = str(item.get("udid") or "").strip()
        if not udid or not is_rfc4122_uuid(udid) or udid in present:
            continue
        present[udid] = {
            "transport": "simulator",
            "model": item.get("name") or "Simulator",
            "os": item.get("os") or "",
            "ip": "",
        }

    changed = False
    for udid, meta in present.items():
        prev = _last_seen.get(udid)
        ok = manager.register_ios_device(
            udid,
            transport=meta["transport"],
            state="connected",
            model=meta.get("model") or "",
            os_version=meta.get("os") or "",
            ip_address=meta.get("ip") or "",
        )
        if ok and prev != meta["transport"]:
            SLog.i(TAG, f"ios device online: {udid} ({meta['transport']})")
            changed = True

    for udid in list(_last_seen.keys()):
        if udid not in present:
            manager.mark_ios_offline(udid)
            SLog.i(TAG, f"ios device gone: {udid}")
            changed = True

    _last_seen.clear()
    _last_seen.update({k: v["transport"] for k, v in present.items()})
    return changed


async def run_ios_discovery(interval_sec: float = DISCOVERY_INTERVAL_SEC) -> None:
    from server.services.runtime.ios_bonjour import start_bonjour_browser, stop_bonjour_browser
    from server.websocket.device_manager import DeviceManager

    SLog.i(TAG, "Starting iOS discovery (usbmuxd + Bonjour + simctl)...")
    start_bonjour_browser()
    try:
        while True:
            try:
                changed = await asyncio.to_thread(_run_tick)
                if changed:
                    await DeviceManager().notify_device_list_changed("ios_discovery")
            except Exception as e:
                SLog.e(TAG, f"discovery tick error: {e}")
            await asyncio.sleep(interval_sec)
    finally:
        stop_bonjour_browser()
