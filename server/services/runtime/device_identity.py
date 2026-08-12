# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""设备指纹与跨连接身份工具 (v3)。

一个物理设备可能同时以两种方式接入：
  - ClawNode App（WS 推送，连接句柄 claw-xxx）
  - adb（USB / TCP，连接句柄 adb serial 或 ip:port）

两种连接各自能拿到设备的真实唯一标识 hw_uid（安卓=ro.serialno，iOS=DID/UDID），
由 hw_uid 派生稳定的 fingerprint_id 作为逻辑设备身份，同 hw_uid 即同一台设备。

本模块只做纯计算，不触库、不依赖 adb / WS。
"""
from __future__ import annotations

import hashlib


def normalize_platform(platform: str | None) -> str:
    plat = (platform or "android").strip().lower()
    if plat in ("android", "android_direct"):
        return "android"
    if plat in ("ios", "iphone", "ipad"):
        return "ios"
    return plat or "android"


def compute_fingerprint(platform: str | None, hw_uid: str | None) -> str:
    """由平台唯一标识派生指纹。

    安卓用 SN、iOS 用 DID —— 具体取哪个由调用方决定，这里只负责把 hw_uid
    按平台加前缀后做 sha256，保证：同 hw_uid → 同指纹；跨平台不碰撞；定长脱敏。

    hw_uid 为空时返回空串（无法建立稳定身份，调用方回退旧启发式）。
    """
    uid = (hw_uid or "").strip()
    if not uid:
        return ""
    plat = normalize_platform(platform)
    digest = hashlib.sha256(f"{plat}:{uid}".encode("utf-8")).hexdigest()[:16]
    return f"fp-{plat}-{digest}"


def is_clawnode_sn(sn: str | None) -> bool:
    """ClawNode 直连连接句柄判定（唯一权威处）。

    统一散落各处的 `sn.startswith("claw-")` 硬编码：ClawNode 的 sn 形如 claw-<androidId>，
    adb 连接句柄是真实 serial / ip:port —— 两者天然区分引擎（RemoteEngine vs MAdbEngine）。
    """
    return bool(sn) and str(sn).startswith("claw-")
