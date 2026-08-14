# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Bonjour / mDNS 发现局域网 iOS 设备。

浏览：
  - _apple-mobdev2._tcp  Xcode/Finder 无线调试（真机）
  - _apple-mobdev._tcp   旧版 mobdev

不浏览 _remotepairing._tcp：那是模拟器 / CoreDevice 配对隧道，实例名是标准 UUID，
不是手机 UDID（例如 2766782A-77B2-…），不能当执行设备。
"""
from __future__ import annotations

import re
import threading
from typing import Dict

from script.log import SLog

from server.services.runtime.ios_ids import is_executable_ios_sn, is_rfc4122_uuid

TAG = "IosBonjour"

SERVICE_TYPES = (
    "_apple-mobdev2._tcp.local.",
    "_apple-mobdev._tcp.local.",
)

_UDID_RE = re.compile(
    r"([0-9A-Fa-f]{40}|[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16})",
)
_MAC_RE = re.compile(r"([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}")

_lock = threading.Lock()
_wifi: Dict[str, dict] = {}
_zc = None
_browsers = []


def _sn_from_name(name: str) -> str:
    raw = str(name or "").split(".")[0]
    m = _UDID_RE.search(raw.replace("\\032", " "))
    if m:
        return m.group(1)
    mac = _MAC_RE.search(raw)
    if mac:
        return "ios-wifi-" + re.sub(r"[^0-9A-Fa-f]", "", mac.group(0)).lower()
    cleaned = re.sub(r"[^0-9A-Za-z._-]", "", raw)[:40]
    if not cleaned or is_rfc4122_uuid(cleaned):
        return ""
    return f"ios-wifi-{cleaned}"


def _parse_info(info, type_: str, name: str) -> dict | None:
    sn = _sn_from_name(name)
    if not sn:
        return None
    ip = ""
    try:
        addrs = info.parsed_addresses() if info else []
        ip = next((a for a in addrs if a and not a.startswith("127.") and ":" not in a), "") or (
            addrs[0] if addrs else ""
        )
    except Exception:
        ip = ""
    txt = {}
    try:
        if info and info.properties:
            txt = {
                (k.decode("utf-8", "ignore") if isinstance(k, bytes) else str(k)): (
                    v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v or "")
                )
                for k, v in info.properties.items()
            }
    except Exception:
        txt = {}
    for key in ("udid", "serial", "id"):
        val = str(txt.get(key) or "").strip()
        if _UDID_RE.fullmatch(val):
            sn = val
            break
    display = str(txt.get("name") or txt.get("n") or name.split(".")[0] or "iOS")
    return {
        "udid": sn,
        "name": display,
        "ip": ip,
        "transport": "wifi",
        "service": type_,
    }


class _Listener:
    def add_service(self, zc, type_, name):
        try:
            info = zc.get_service_info(type_, name, timeout=2500)
        except Exception:
            info = None
        rec = _parse_info(info, type_, name)
        if not rec or not is_executable_ios_sn(rec.get("udid") or ""):
            return
        with _lock:
            _wifi[rec["udid"]] = rec
        SLog.d(TAG, f"bonjour + {rec['udid']} {rec.get('ip')}")

    def remove_service(self, zc, type_, name):
        sn = _sn_from_name(name)
        if not sn:
            return
        with _lock:
            _wifi.pop(sn, None)
        SLog.d(TAG, f"bonjour - {sn}")

    def update_service(self, zc, type_, name):
        self.add_service(zc, type_, name)


def start_bonjour_browser() -> None:
    global _zc, _browsers
    if _zc is not None:
        return
    try:
        from zeroconf import IPVersion, ServiceBrowser, Zeroconf
    except Exception as e:
        SLog.w(TAG, f"zeroconf unavailable, skip iOS Bonjour: {e}")
        return
    try:
        _zc = Zeroconf(ip_version=IPVersion.V4Only)
        listener = _Listener()
        _browsers = [ServiceBrowser(_zc, list(SERVICE_TYPES), listener)]
        SLog.i(TAG, "Bonjour iOS browser started")
    except Exception as e:
        SLog.w(TAG, f"start Bonjour browser failed: {e}")
        _zc = None
        _browsers = []


def stop_bonjour_browser() -> None:
    global _zc, _browsers
    for b in _browsers:
        try:
            b.cancel()
        except Exception:
            pass
    _browsers = []
    if _zc is not None:
        try:
            _zc.close()
        except Exception:
            pass
        _zc = None


def snapshot_wifi_devices() -> Dict[str, dict]:
    with _lock:
        return dict(_wifi)
