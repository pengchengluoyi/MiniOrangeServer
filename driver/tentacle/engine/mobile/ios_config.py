# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
iOS 运行时发现与 WDA 连接：设备身份来自 server.services.device_service（m_device），
本模块只负责 USB/simulator 探测与 WDA URL/签名等运行时常量。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Optional

from script.log import SLog

TAG = "IOSConfig"
DEFAULT_WDA_URL = "http://127.0.0.1:8100"


@dataclass
class IOSDeviceInfo:
    udid: str
    name: str
    platform_version: str = ""
    is_simulator: bool = False


def _tidevice_bin() -> str:
    candidates = []
    if sys.prefix:
        candidates.append(os.path.join(sys.prefix, "bin", "tidevice"))
    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        candidates.append(os.path.join(venv, "bin", "tidevice"))
    candidates.append(shutil.which("tidevice") or "tidevice")
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return candidates[-1]


def _run_cmd(cmd: list[str], timeout: int = 8) -> str:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, errors="ignore"
        )
        if result.returncode == 0:
            return (result.stdout or "").strip()
    except Exception as e:
        SLog.d(TAG, f"Command failed {cmd}: {e}")
    return ""


def list_simulators() -> list[IOSDeviceInfo]:
    devices: list[IOSDeviceInfo] = []
    raw = _run_cmd(["xcrun", "simctl", "list", "devices", "booted", "-j"])
    if not raw:
        return devices
    try:
        payload = json.loads(raw)
        for runtime, entries in (payload.get("devices") or {}).items():
            if "iOS" not in runtime and "iphoneos" not in runtime.lower():
                continue
            for entry in entries or []:
                if entry.get("state") != "Booted":
                    continue
                udid = entry.get("udid") or ""
                if udid:
                    devices.append(
                        IOSDeviceInfo(
                            udid=udid,
                            name=entry.get("name") or "Simulator",
                            is_simulator=True,
                        )
                    )
    except json.JSONDecodeError as e:
        SLog.w(TAG, f"simctl JSON parse error: {e}")
    return devices


def list_usb_devices() -> list[IOSDeviceInfo]:
    devices: list[IOSDeviceInfo] = []
    raw = _run_cmd([_tidevice_bin(), "list"])
    if not raw:
        return devices
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("udid"):
            continue
        parts = line.split()
        if not parts:
            continue
        udid = parts[0]
        name = parts[1] if len(parts) > 1 else "iOS Device"
        devices.append(IOSDeviceInfo(udid=udid, name=name, is_simulator=False))
    return devices


def list_ios_devices() -> list[IOSDeviceInfo]:
    sims = list_simulators()
    if sims:
        return sims
    return list_usb_devices()


def verify_wda_url(url: str, timeout: float = 3.0, *, quiet: bool = False) -> bool:
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/status", timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="ignore")
            return resp.status == 200 and (
                "value" in body or "ready" in body or "sessionId" in body
            )
    except Exception as e:
        if not quiet:
            SLog.d(TAG, f"WDA verify failed {url}: {e}")
        return False


def probe_wda_url(ports: Optional[list[int]] = None) -> Optional[str]:
    ports = ports or [8100, 8101, 8200]
    for port in ports:
        url = f"http://127.0.0.1:{port}"
        if verify_wda_url(url):
            SLog.i(TAG, f"Found running WDA at {url}")
            return url
    return None


def detect_xcode_team_id() -> Optional[str]:
    raw = _run_cmd(["security", "find-identity", "-v", "-p", "codesigning"])
    if not raw:
        return None
    for line in raw.splitlines():
        if "Apple Development" not in line:
            continue
        # security 的输出形如：1) <hash> "Apple Development: name (474L99R7GT)"
        # 行尾带一个双引号，早期正则用 \)\s*$ 锚定，永远匹配不到 Team ID。
        m = re.search(r"\(([A-Z0-9]{10})\)\"?\s*$", line)
        if m:
            return m.group(1)
    return None


def resolve_device(test_subject: Optional[str] = None) -> IOSDeviceInfo:
    """
    test_subject：运行级 sn（TARGET_DEVICE_SN 或 DeviceService.pick_sn(device_type=...)）。
    否则环境变量 IOS_UDID，再否则 USB/模拟器自动发现。
    """
    from server.services.device_service import DeviceService
    from server.services.runtime.ios_ids import is_simulator_udid

    udid = (
        test_subject
        or os.environ.get("IOS_UDID")
        or DeviceService.pick_sn(device_type="ios")
    )
    if udid:
        row = DeviceService.get_by_sn(udid)
        for dev in list_ios_devices():
            if dev.udid == udid:
                return dev
        if row:
            return IOSDeviceInfo(
                udid=udid,
                name=row.model or "iOS",
                platform_version=row.os_version or "",
                is_simulator=is_simulator_udid(udid),
            )
        return IOSDeviceInfo(
            udid=udid,
            name="iOS",
            is_simulator=is_simulator_udid(udid),
        )

    prefer = (os.environ.get("IOS_PREFER") or "device").lower()
    sims = list_simulators()
    usbs = list_usb_devices()
    if prefer == "device" and usbs:
        return usbs[0]
    if sims:
        return sims[0]
    if usbs:
        return usbs[0]
    raise ConnectionError(
        "No iOS device found. Connect USB device, register hub in m_device, or set IOS_UDID."
    )


def resolve_wda_url() -> str:
    return (
        os.environ.get("IOS_WDA_URL")
        or probe_wda_url()
        or DEFAULT_WDA_URL
    ).rstrip("/")


DDI_HINT = (
    "设备未挂载 Developer Disk Image（iOS 17+ 启动 XCTest/WebDriverAgent 的前提），"
    "所有 iOS 自动化能力都无法使用，与 wda / appium 后端的选择无关。\n"
    "若错误含 kAMAuthInstallErrorHTTPUnauthorized / 3501：Apple 拒绝签发个性化 DDI。\n"
    "排查顺序：\n"
    "  1. Xcode → Window → Devices and Simulators，等 Preparing device 完成\n"
    "     或：.venv/bin/python driver/tentacle/engine/mobile/appium_ios.py prepare\n"
    "  2. 一次性安装 Xcode 的 MobileDevice*.pkg / XcodeSystemResources.pkg（需 sudo）\n"
    "  3. 宿主 macOS 大.小版本不要低于设备 iOS（如 26.3 vs 26.4）\n"
    "  4. 设备重启后保持解锁并插稳 USB\n"
    "确认命令：xcrun devicectl device info details --device <udid> | grep ddiServicesAvailable\n"
    "如需跳过本检查：IOS_SKIP_DDI_CHECK=1"
)


def ddi_services_available(udid: str, *, timeout: int = 60) -> Optional[bool]:
    """
    设备上的 Developer Disk Image 服务是否可用。True / False / None(判断不了)。

    用 Xcode 自带的 devicectl 读取（--device 可直接接受 UDID）。只做只读探测，
    不触发挂载 —— 挂载由 xcuitest driver / CoreDevice 自动完成，且挂上后持续到设备重启。
    """
    if not udid:
        return None
    if not shutil.which("xcrun"):
        return None
    out_path = os.path.join(
        tempfile.gettempdir(), f"miniorange-ddi-{udid[:12]}.json"
    )
    try:
        subprocess.run(
            [
                "xcrun", "devicectl", "device", "info", "details",
                "--device", udid, "--quiet", "--json-output", out_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="ignore",
        )
        with open(out_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        value = (payload.get("result") or {}).get("deviceProperties", {}).get(
            "ddiServicesAvailable"
        )
        return bool(value) if value is not None else None
    except Exception as e:
        SLog.d(TAG, f"ddiServicesAvailable 探测失败 {udid[:12]}: {e}")
        return None
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


def assert_ddi_ready(device: IOSDeviceInfo, *, tag: str = TAG) -> None:
    """DDI 明确不可用时快速失败，避免让调用方去啃 xcodebuild code 70 的堆栈。"""
    if os.environ.get("IOS_SKIP_DDI_CHECK") == "1":
        return
    if device.is_simulator:
        return
    available = ddi_services_available(device.udid)
    if available is None:
        SLog.d(tag, "无法确认 DDI 状态（devicectl 不可用），继续尝试")
        return
    if available:
        SLog.i(tag, f"DDI 已就绪 udid={device.udid[:12]}…")
        return
    raise RuntimeError(
        f"设备 {device.name} ({device.udid}) 的 DDI 未就绪。\n{DDI_HINT}"
    )
