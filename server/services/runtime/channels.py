# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""MDevice.channels JSON 字段的读写帮手。

字段结构：
    {
      "remote": {
        "state": "connected" | "disconnected" | "auth_failed" | "unpaired",
        "last_heartbeat_at": "2026-06-24T19:00:00",
        "auth_state": str,    # ClawNode 端的细粒度 ConnectionState 名（Authenticated / Reconnecting / ...）
        "details": str
      },
      "adb": {
        "state": "connected" | "disconnected" | "unauthorized" | "not_applicable",
        "last_probe_at": "2026-06-24T19:00:00",
        "transport": "usb" | "tcp" | None,
        "serial": str,        # 真实 adb serial（claw-* 设备解析后的）
        "reason": str
      }
    }
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any

from server.models.mDevice import MDevice

REMOTE_STATES = frozenset({"connected", "disconnected", "auth_failed", "unpaired"})
ADB_STATES = frozenset({"connected", "disconnected", "unauthorized", "not_applicable"})

DEFAULT_CHANNELS: dict[str, dict[str, Any]] = {
    "remote": {
        "state": "disconnected",
        "last_heartbeat_at": None,
        "auth_state": None,
        "details": "",
    },
    "adb": {
        "state": "not_applicable",
        "last_probe_at": None,
        "transport": None,
        "serial": "",
        "reason": "",
    },
}


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def read_channels(device: MDevice | None) -> dict[str, dict[str, Any]]:
    """从 MDevice 读取 channels 并合并默认值。device=None 返回纯默认结构。"""
    base = copy.deepcopy(DEFAULT_CHANNELS)
    if device is None:
        return base
    raw = getattr(device, "channels", None)
    if not isinstance(raw, dict):
        return base
    for k, default in DEFAULT_CHANNELS.items():
        v = raw.get(k)
        if isinstance(v, dict):
            merged = dict(default)
            merged.update({kk: vv for kk, vv in v.items() if vv is not None or kk in default})
            base[k] = merged
    return base


def set_remote_channel(
    device: MDevice,
    *,
    state: str,
    auth_state: str | None = None,
    details: str | None = None,
) -> dict[str, dict[str, Any]]:
    """更新 channels.remote。返回更新后的完整 channels（已写入 device.channels）。"""
    if state not in REMOTE_STATES:
        raise ValueError(f"invalid remote state: {state}")
    channels = read_channels(device)
    channels["remote"]["state"] = state
    channels["remote"]["last_heartbeat_at"] = _now_iso()
    if auth_state is not None:
        channels["remote"]["auth_state"] = auth_state
    if details is not None:
        channels["remote"]["details"] = details
    device.channels = channels
    return channels


def set_adb_channel(
    device: MDevice,
    *,
    state: str,
    serial: str | None = None,
    transport: str | None = None,
    reason: str | None = None,
) -> dict[str, dict[str, Any]]:
    """更新 channels.adb。"""
    if state not in ADB_STATES:
        raise ValueError(f"invalid adb state: {state}")
    channels = read_channels(device)
    channels["adb"]["state"] = state
    channels["adb"]["last_probe_at"] = _now_iso()
    if serial is not None:
        channels["adb"]["serial"] = serial
    if transport is not None:
        channels["adb"]["transport"] = transport
    if reason is not None:
        channels["adb"]["reason"] = reason
    device.channels = channels
    return channels


def channels_to_brief(channels: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """把 channels 摘要成给前端列表 / Prompt 的精简形式。"""
    remote = channels.get("remote") or {}
    adb = channels.get("adb") or {}
    return {
        "remote_state": remote.get("state") or "disconnected",
        "adb_state": adb.get("state") or "not_applicable",
        "remote_last_heartbeat_at": remote.get("last_heartbeat_at"),
        "adb_last_probe_at": adb.get("last_probe_at"),
        "adb_serial": adb.get("serial") or "",
        "adb_transport": adb.get("transport"),
    }


def derive_main_status(
    channels: dict[str, dict[str, Any]],
    *,
    has_active_ws: bool = False,
    is_busy: bool = False,
) -> str:
    """根据 channels 推导主 status (online/offline/busy/error)。

    规则：
      - busy 由调用方传入（被某个 run 占用）
      - remote=connected 或 adb=connected → online
      - 否则 → offline
    """
    if is_busy:
        return "busy"
    remote = (channels.get("remote") or {}).get("state")
    adb = (channels.get("adb") or {}).get("state")
    if remote == "connected" or adb == "connected" or has_active_ws:
        return "online"
    if remote == "auth_failed":
        return "error"
    return "offline"


def resolve_control_channel(device: MDevice | None) -> dict[str, Any]:
    """判定一台设备当前应走哪条控制渠道。

    统一替换散落各处的 `sn.startswith("claw-")` 硬编码。返回：
        { "channel": "remote" | "adb" | "none", "adb_serial": str }

    判定顺序（v3，P0）：
      1. ClawNode 设备（claw-* / android_direct / 已绑定 clawnode_id）→ remote
         —— 保持现状语义，绝不让 claw 设备误走 adb，ClawNode 零回归。
      2. adb 通道 connected 且有 serial → adb
      3. remote 通道 connected → remote
      4. 否则 → none
    双通道并存时的"优先 adb / 交给大模型"策略见 PRD §6，属 P2，本函数只给安全默认值。
    """
    if device is None:
        return {"channel": "none", "adb_serial": ""}
    channels = read_channels(device)
    sn = str(getattr(device, "sn", "") or "")
    adb = channels.get("adb") or {}
    adb_serial = str(adb.get("serial") or getattr(device, "adb_sn", "") or "").strip()

    is_claw = (
        sn.startswith("claw-")
        or str(getattr(device, "device_type", "") or "") == "android_direct"
        or bool(getattr(device, "clawnode_id", ""))
    )
    if is_claw:
        return {"channel": "remote", "adb_serial": adb_serial}

    if adb.get("state") == "connected" and adb_serial:
        return {"channel": "adb", "adb_serial": adb_serial}

    if (channels.get("remote") or {}).get("state") == "connected":
        return {"channel": "remote", "adb_serial": adb_serial}

    return {"channel": "none", "adb_serial": adb_serial}

