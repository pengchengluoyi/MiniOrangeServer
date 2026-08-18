# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Run 启动时的通道连通性探测（adb / remote / vlm / hitl）。

设计原则：
  - 每个 probe 独立、可单独调用，便于调试 / 复用
  - 不直接修改 MDevice.channels；那是 device_manager 与 RunContext 的事
  - 探测一次只产出 (state, meta)，不缓存（缓存交给 RunContext / device_manager）
"""
from __future__ import annotations

import subprocess
from typing import Any, Tuple

from script.log import SLog

TAG = "ConnectivityProbe"

# 类型别名（仅用于注释，避免对运行时引入 Literal 兼容）
RemoteState = str  # connected | disconnected | auth_failed | unpaired
AdbState = str  # connected | disconnected | unauthorized | not_applicable
VlmState = str  # available | not_configured | error
HitlState = str  # available | disabled

ProbeResult = Tuple[str, dict]


# ====== Remote (ClawNode WebSocket) ======


def probe_remote(sn: str) -> ProbeResult:
    """从 DeviceManager 读取当前 ws 状态。

    ClawNode 端的细粒度 9 态在 device_manager 注册/心跳时已汇总：
      - active_connections 含 sn AND sn 在 direct_nodes 内 → 视为 Authenticated → connected
      - active_connections 含 sn 但不在 direct_nodes → 未鉴权 → disconnected
      - 不在 active_connections → disconnected
    """
    if not sn:
        return "disconnected", {"reason": "no sn"}
    try:
        # 这里晚导入是为了避免 plugins/loader 等无设备依赖的模块被牵连
        from server.websocket.device_manager import DeviceManager

        manager = DeviceManager()  # 单例
    except Exception as e:
        SLog.w(TAG, f"DeviceManager not available: {e}")
        return "disconnected", {"reason": f"device_manager import failed: {e}"}

    try:
        ws = manager.active_connections.get(sn)
    except Exception as e:
        return "disconnected", {"reason": f"active_connections read failed: {e}"}

    if ws and sn in getattr(manager, "direct_nodes", set()):
        # 进一步取最近心跳时间作为新鲜度证据
        last_hb = None
        try:
            last_hb = manager._last_app_heartbeat.get(sn)  # noqa: SLF001
        except Exception:
            pass
        return "connected", {"auth_state": "Authenticated", "last_heartbeat_ts": last_hb}
    if ws:
        return "disconnected", {"reason": "ws active but not authenticated direct node"}
    return "disconnected", {"reason": "no active ws"}


# ====== iOS (usbmuxd / WDA) ======


def probe_ios(sn: str) -> ProbeResult:
    """iOS 是否可作为执行目标：USB 在 usbmuxd 上，或发现器已标记 ios_nodes。

    不要求 WDA 此刻已起来（首次动作时再拉起），避免把插着的真机判成 offline。
    """
    if not sn:
        return "disconnected", {"reason": "no sn"}
    try:
        from server.websocket.device_manager import DeviceManager

        manager = DeviceManager()
        if sn in getattr(manager, "ios_nodes", set()):
            return "connected", {"source": "ios_nodes"}
    except Exception as e:
        SLog.d(TAG, f"ios_nodes read failed: {e}")
    try:
        from server.services.runtime.ios_usbmux import list_usb_ios_devices

        for item in list_usb_ios_devices():
            if str(item.get("udid") or "") == str(sn):
                return "connected", {"source": "usbmuxd", "transport": "usb"}
    except Exception as e:
        SLog.d(TAG, f"usbmux probe failed: {e}")
    try:
        from server.core.database import SessionLocal
        from server.models.mDevice import MDevice
        from server.services.runtime.channels import read_channels

        with SessionLocal() as db:
            dev = db.query(MDevice).filter(MDevice.sn == sn).first()
            if dev:
                ios = (read_channels(dev).get("ios") or {})
                if ios.get("state") == "connected":
                    return "connected", {
                        "source": "m_device",
                        "transport": ios.get("transport") or "",
                    }
    except Exception as e:
        SLog.d(TAG, f"m_device ios channel probe failed: {e}")
    try:
        from server.services.runtime.ios_simctl import list_booted_simulators

        for item in list_booted_simulators():
            if str(item.get("udid") or "") == str(sn):
                return "connected", {"source": "simctl", "transport": "simulator"}
    except Exception as e:
        SLog.d(TAG, f"simctl probe failed: {e}")
    return "disconnected", {"reason": "not present on usbmuxd / ios discovery"}


# ====== ADB ======


def probe_adb(sn: str, *, timeout_sec: float = 8.0) -> ProbeResult:
    """通过 `adb -s <sn> shell echo ok` 测试 adb 通路。

    入参 sn 必须是真实 adb serial（不是 claw-*）。调用方 RunContext 负责把
    ClawNode 直连设备的 claw-xxx 解析为对应的 adb serial（若有）。

    返回 state：
      - connected：echo ok 成功
      - unauthorized：adb 提示 unauthorized
      - disconnected：adb 不在 PATH / 超时 / device not found / 其它失败
      - not_applicable：sn 为空或仍是 claw-*（无法 adb 化）
    """
    if not sn:
        return "not_applicable", {"reason": "no sn"}
    if str(sn).startswith("claw-"):
        return "not_applicable", {"reason": "clawnode sn cannot be queried via adb directly"}

    try:
        proc = subprocess.run(
            ["adb", "-s", str(sn), "shell", "echo", "ok"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError:
        return "disconnected", {"reason": "adb binary not in PATH"}
    except subprocess.TimeoutExpired:
        return "disconnected", {"reason": f"adb shell timeout ({timeout_sec}s)"}
    except Exception as e:
        return "disconnected", {"reason": f"adb invoke failed: {e}"}

    stdout = (proc.stdout or "").strip()
    stderr_lower = (proc.stderr or "").strip().lower()
    if "unauthorized" in stderr_lower:
        return "unauthorized", {"stderr": stderr_lower[:240]}
    if "device not found" in stderr_lower or "no devices/emulators found" in stderr_lower:
        return "disconnected", {"stderr": stderr_lower[:240]}
    if proc.returncode == 0 and "ok" in stdout.lower():
        transport = "tcp" if ":" in str(sn) else "usb"
        return "connected", {"transport": transport, "stdout": stdout[:120]}
    return "disconnected", {
        "stderr": stderr_lower[:240],
        "stdout": stdout[:120],
        "returncode": proc.returncode,
    }


def list_adb_serials(*, timeout_sec: float = 6.0) -> list[str]:
    """查询当前所有 adb device 的 serial 列表（去掉 offline/unauthorized）。"""
    try:
        proc = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, Exception):
        return []
    serials: list[str] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


# ====== VLM Provider ======


def probe_vlm(
    provider_id: str = "",
    model: str = "",
    *,
    require_explicit: bool = False,
) -> ProbeResult:
    """检查 VLM provider 配置是否齐全。

    简化版（Step 2）：
      - 显式给 provider_id + model 即 available
      - 否则查 system_settings 看是否有任何已配置的 AI provider；有就视为 available
      - 都没有 → not_configured
      - require_explicit=True 时必须给 provider_id+model 才算 available
    """
    pid = (provider_id or "").strip()
    mid = (model or "").strip()
    if pid and mid:
        return "available", {"provider": pid, "model": mid, "source": "explicit"}

    if require_explicit:
        return "not_configured", {"reason": "provider_id+model not provided"}

    # 尝试从系统设置读取默认 provider
    try:
        from server.services import system_settings_service as ss

        # 大多数 ss API 暴露 should_use_ai_planning 接口
        if hasattr(ss, "should_use_ai_planning"):
            res = ss.should_use_ai_planning("plan")
            if isinstance(res, dict) and res.get("enabled"):
                return "available", {
                    "provider": res.get("provider_id") or "default",
                    "model": res.get("model") or "",
                    "source": "system_settings",
                }
    except Exception as e:
        SLog.w(TAG, f"system_settings probe failed: {e}")

    return "not_configured", {"reason": "no AI provider configured"}


# ====== HITL ======


def probe_hitl(*, observer_count: int | None = None) -> ProbeResult:
    """HITL 通道是否可用。

    简化版：只要 server 在跑就视为可用（前端连不连等到弹框时再判断超时）。
    后续可改为检查 observer/前端 WS 是否在线。
    """
    if observer_count is not None and observer_count <= 0:
        return "disabled", {"reason": "no frontend observer connected"}
    return "available", {}
