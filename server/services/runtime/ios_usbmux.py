# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""通过 usbmuxd 列出 USB 连接的 iOS 设备（不依赖 tidevice）。

协议：usbmuxd v1 plist（macOS/Linux: /var/run/usbmuxd；Windows: 127.0.0.1:27015）。
"""
from __future__ import annotations

import os
import plistlib
import socket
import struct
import subprocess
from typing import List

from script.log import SLog

TAG = "IosUsbmux"

_USBMUXD_SOCK = "/var/run/usbmuxd"
_USBMUXD_TCP = ("127.0.0.1", 27015)
_HEADER_FMT = "<IIII"  # length, version, type, tag
_PLIST_TYPE = 8
_PROTO_VERSION = 1


def _connect() -> socket.socket | None:
    if getattr(socket, "AF_UNIX", None) and os.path.exists(_USBMUXD_SOCK):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(2.5)
            sock.connect(_USBMUXD_SOCK)
            return sock
        except OSError as e:
            SLog.d(TAG, f"usbmuxd unix connect failed: {e}")
            try:
                sock.close()
            except OSError:
                pass
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        tcp.settimeout(2.5)
        tcp.connect(_USBMUXD_TCP)
        return tcp
    except OSError:
        try:
            tcp.close()
        except OSError:
            pass
        return None


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("usbmuxd closed")
        buf += chunk
    return buf


def _plist_rpc(payload: dict) -> dict:
    body = plistlib.dumps(payload, fmt=plistlib.FMT_XML)
    header = struct.pack(_HEADER_FMT, 16 + len(body), _PROTO_VERSION, _PLIST_TYPE, 1)
    sock = _connect()
    if sock is None:
        return {}
    try:
        sock.sendall(header + body)
        raw_hdr = _recv_exact(sock, 16)
        length, _, _, _ = struct.unpack(_HEADER_FMT, raw_hdr)
        rest = _recv_exact(sock, max(0, length - 16))
        if not rest:
            return {}
        parsed = plistlib.loads(rest)
        return parsed if isinstance(parsed, dict) else {}
    finally:
        try:
            sock.close()
        except OSError:
            pass


def list_usbmux_devices() -> List[dict]:
    """返回 [{udid, name, product_id}]，仅 USB。"""
    try:
        resp = _plist_rpc({
            "MessageType": "ListDevices",
            "ClientVersionString": "MiniOrange",
            "ProgName": "MiniOrange",
        })
    except Exception as e:
        SLog.d(TAG, f"ListDevices failed: {e}")
        return []
    out: List[dict] = []
    for item in resp.get("DeviceList") or []:
        if not isinstance(item, dict):
            continue
        props = item.get("Properties") or item
        if not isinstance(props, dict):
            continue
        udid = str(props.get("SerialNumber") or "").strip()
        if not udid:
            continue
        conn = str(props.get("ConnectionType") or "USB").upper()
        if conn and conn not in ("USB", "NETWORK"):
            continue
        if conn == "NETWORK":
            continue
        out.append({
            "udid": udid,
            "name": str(props.get("DeviceName") or props.get("ProductType") or "").strip(),
            "product_id": props.get("ProductID"),
            "transport": "usb",
        })
    return out


def _tidevice_list() -> List[dict]:
    try:
        r = subprocess.run(
            ["tidevice", "list"], capture_output=True, text=True, timeout=5, errors="ignore"
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if r.returncode != 0:
        return []
    items: List[dict] = []
    for line in (r.stdout or "").splitlines():
        line = line.strip()
        if not line or line.upper().startswith("UDID"):
            continue
        parts = line.split()
        udid = parts[0]
        if udid.upper() in ("UDID", "NAME", "SERIALNUMBER", "DEVICE"):
            continue
        name = parts[1] if len(parts) > 1 else ""
        if name.upper() in ("NAME", "SERIALNUMBER", "UDID", "IOS"):
            name = ""
        items.append({"udid": udid, "name": name, "transport": "usb"})
    return items


_name_cache: dict[str, str] = {}


def _lookup_device_name(udid: str) -> str:
    """usbmux ListDevices 通常没有 DeviceName，补一次 ideviceinfo / tidevice。"""
    if not udid:
        return ""
    if udid in _name_cache:
        return _name_cache[udid]
    name = ""
    for cmd in (
        ["ideviceinfo", "-u", udid, "-k", "DeviceName"],
        ["tidevice", "-u", udid, "info", "--key", "DeviceName"],
    ):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3, errors="ignore")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if r.returncode != 0:
            continue
        lines = (r.stdout or "").strip().splitlines()
        cand = lines[0].strip() if lines else ""
        if cand and cand.lower() not in ("ios", "iphone", "ipad", "device"):
            name = cand
            break
    _name_cache[udid] = name
    return name


def list_usb_ios_devices() -> List[dict]:
    found = list_usbmux_devices() or _tidevice_list()
    for item in found:
        name = str(item.get("name") or "").strip()
        if not name or name.lower() in ("ios", "iphone", "ipad", "device"):
            looked = _lookup_device_name(item.get("udid") or "")
            if looked:
                item["name"] = looked
    return found
