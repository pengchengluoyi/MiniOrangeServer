# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""ADB 设备发现器 (v3, P0)。

后台轮询本机 adb，把 USB / TCP 直连设备纳入统一设备列表：
  - `adb devices` 拿到全部连接及状态（device / unauthorized / offline）
  - 对配置的 TCP 端点尝试 `adb connect`（D6：与 ClawNode 一样的"连接后取 SN"思路）
  - 已授权设备：`getprop ro.serialno` 取真实 SN 作 hw_uid → 派生指纹 → 注册为 online
  - unauthorized：进列表但标记待授权（前端提示允许 USB 调试）
  - 消失的设备：置 adb 通道 disconnected

与 ClawNode 的心跳监控并列、互不干扰；ClawNode(claw-*) 走 WS，不在本发现器管辖内。
"""
from __future__ import annotations

import asyncio
import subprocess
from typing import Dict, List, Tuple

from script.log import SLog

TAG = "AdbDiscovery"

DISCOVERY_INTERVAL_SEC = 5.0
_ADB_TIMEOUT = 6.0

# 上一轮观察到的 { serial: state }，用于检测新增/消失/状态变化
_last_seen: Dict[str, str] = {}


def _adb(args: List[str], *, timeout: float = _ADB_TIMEOUT) -> Tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["adb", *args], capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except FileNotFoundError:
        return 127, "", "adb binary not in PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"adb {' '.join(args)} timeout"
    except Exception as e:  # pragma: no cover
        return 1, "", f"adb invoke failed: {e}"


def list_adb_states() -> Dict[str, str]:
    """`adb devices` → { serial: state }，state ∈ device|unauthorized|offline|... 。"""
    rc, out, _ = _adb(["devices"])
    states: Dict[str, str] = {}
    if rc != 0:
        return states
    for line in out.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            states[parts[0]] = parts[1]
    return states


def _tcp_endpoints() -> List[str]:
    """从配置读取要 adb connect 的 TCP 端点（ip:port 列表）。未配置则空。"""
    try:
        from server.core.security import SecurityManager

        raw = (SecurityManager._config or {}).get("adb_tcp_endpoints")
    except Exception:
        raw = None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _try_connect_tcp(endpoints: List[str]) -> None:
    for ep in endpoints:
        rc, out, err = _adb(["connect", ep])
        if rc == 0 and "connected" in (out + err).lower():
            SLog.d(TAG, f"adb connect {ep}: {out or err}")


def _clawnode_tcp_candidates() -> List[str]:
    """动态IP+TCP发现：从 ClawNode 设备当前 ip_address 派生 ip:5555 端点，
    使换了网段的 ClawNode 也能经 adb-tcp 接入并按指纹与其 claw 连接合并。

    默认关闭；配置 adb_tcp_follow_clawnode=true 开启（避免对每台 claw 机每 tick 发起
    connect 造成额外开销 / 触发设备端授权弹窗）。
    """
    try:
        from server.core.security import SecurityManager

        if not (SecurityManager._config or {}).get("adb_tcp_follow_clawnode"):
            return []
        port = int((SecurityManager._config or {}).get("adb_tcp_port") or 5555)
    except Exception:
        return []

    eps: List[str] = []
    try:
        from server.core.database import SessionLocal
        from server.models.mDevice import MDevice

        with SessionLocal() as db:
            rows = (
                db.query(MDevice)
                .filter(MDevice.device_type == "android_direct")
                .all()
            )
            for d in rows:
                ip = str(getattr(d, "ip_address", "") or "").strip()
                if ip and ip.upper() != "USB" and ":" not in ip:
                    eps.append(f"{ip}:{port}")
    except Exception as e:
        SLog.w(TAG, f"clawnode tcp candidates failed: {e}")
    return eps


def _prop(serial: str, name: str) -> str:
    rc, out, _ = _adb(["-s", serial, "shell", "getprop", name])
    return out.strip() if rc == 0 else ""


def _collect_device_meta(serial: str) -> dict:
    """已授权设备：取 hw_uid(真实SN) + 型号 + 系统版本。"""
    hw_uid = _prop(serial, "ro.serialno")
    # tcp 场景 serial 是 ip:port，ro.serialno 才是真实 SN；usb 场景两者常一致
    model = _prop(serial, "ro.product.model")
    os_version = _prop(serial, "ro.build.version.release")
    return {"hw_uid": hw_uid, "model": model, "os_version": os_version}


def _run_tick() -> bool:
    """同步执行一轮发现（含 DB 写）。返回是否有变化（需广播设备列表）。"""
    from server.websocket.device_manager import DeviceManager

    manager = DeviceManager()
    endpoints = _tcp_endpoints() + _clawnode_tcp_candidates()
    if endpoints:
        _try_connect_tcp(endpoints)

    states = list_adb_states()
    changed = False

    for serial, state in states.items():
        transport = "tcp" if ":" in serial else "usb"
        prev = _last_seen.get(serial)
        if state == "device":
            meta = _collect_device_meta(serial)
            ok = manager.register_adb_device(
                serial,
                transport=transport,
                state="connected",
                hw_uid=meta.get("hw_uid", ""),
                model=meta.get("model", ""),
                os_version=meta.get("os_version", ""),
                ip_address=(serial.split(":")[0] if transport == "tcp" else ""),
            )
            if ok and prev != "device":
                SLog.i(TAG, f"adb device online: {serial} ({transport}) hw_uid={meta.get('hw_uid')}")
                changed = True
        elif state == "unauthorized":
            manager.register_adb_device(
                serial,
                transport=transport,
                state="unauthorized",
                reason="等待设备授权：请在设备上勾选『允许 USB 调试』并信任此电脑",
            )
            if prev != "unauthorized":
                SLog.w(TAG, f"adb device unauthorized: {serial}")
                changed = True
        else:
            # offline 等异常态：置 disconnected
            manager.mark_adb_offline(serial)
            if prev not in (None, "offline"):
                changed = True

    # 检测消失的设备
    for serial in list(_last_seen.keys()):
        if serial not in states:
            manager.mark_adb_offline(serial)
            SLog.i(TAG, f"adb device gone: {serial}")
            changed = True

    _last_seen.clear()
    _last_seen.update(states)
    return changed


async def run_adb_discovery(interval_sec: float = DISCOVERY_INTERVAL_SEC) -> None:
    """后台协程：定时发现 adb 设备。在 main.py lifespan 中 create_task 启动。"""
    from server.websocket.device_manager import DeviceManager

    SLog.i(TAG, "Starting ADB discovery loop...")
    while True:
        try:
            changed = await asyncio.to_thread(_run_tick)
            if changed:
                await DeviceManager().notify_device_list_changed("adb_discovery")
        except Exception as e:
            SLog.e(TAG, f"discovery tick error: {e}")
        await asyncio.sleep(interval_sec)
