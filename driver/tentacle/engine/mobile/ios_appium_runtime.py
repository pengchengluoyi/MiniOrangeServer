# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
Appium (XCUITest) 运行时：为 IOSAppiumEngine 管理 appium server 进程与 capabilities。

**纯自管**：WDA 一律由 Appium 自己 build + install + launch（走 xcodebuild CLI，不需要
Xcode GUI）。不探测已在跑的 WDA、不复用设备上已装的 runner、不依赖 ios_runtime.py
（那是 wda 后端的地盘）—— 本模块与 wda 后端零耦合，唯一的交集是本地端口避让。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from typing import Any, Optional

from driver.tentacle.engine.mobile.ios_config import (
    IOSDeviceInfo,
    detect_xcode_team_id,
)
from script.log import SLog

TAG = "IOSAppiumRuntime"

APPIUM_PID_FILE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "miniorange-appium.pid")
APPIUM_LOG = os.path.join(os.environ.get("TMPDIR", "/tmp"), "miniorange-appium.log")
DEFAULT_APPIUM_PORT = 4723
DEFAULT_APPIUM_HOST = "127.0.0.1"
DEFAULT_WDA_LOCAL_PORT = 8100

_NPM_GLOBAL_HINTS = (
    "/opt/homebrew/bin/appium",
    "/usr/local/bin/appium",
    os.path.expanduser("~/.nvm/versions/node/*/bin/appium"),
    os.path.expanduser("~/.npm-global/bin/appium"),
)

_INSTALL_HINT = (
    "未找到 appium CLI。请先安装（只需一次）：\n"
    "  npm i -g appium\n"
    "  appium driver install xcuitest\n"
    "或用 IOS_APPIUM_BIN 指向已有的 appium 可执行文件；"
    "若 appium server 由外部维护，设 IOS_APPIUM_AUTOSTART=0 并保证 "
    f"{DEFAULT_APPIUM_HOST}:{DEFAULT_APPIUM_PORT} 可用。"
)


# --------------------------------------------------------------------------- #
# appium CLI / server 发现
# --------------------------------------------------------------------------- #
def appium_bin() -> Optional[str]:
    """定位 appium 可执行文件；找不到返回 None（由调用方决定是否致命）。"""
    explicit = os.environ.get("IOS_APPIUM_BIN")
    if explicit:
        return explicit if os.path.isfile(explicit) else shutil.which(explicit)

    found = shutil.which("appium")
    if found:
        return found

    import glob

    for hint in _NPM_GLOBAL_HINTS:
        for path in sorted(glob.glob(hint), reverse=True):
            if os.path.isfile(path):
                return path
    return None


def appium_port() -> int:
    return int(os.environ.get("IOS_APPIUM_PORT") or DEFAULT_APPIUM_PORT)


def appium_server_url() -> str:
    url = os.environ.get("IOS_APPIUM_URL")
    if url:
        return url.rstrip("/")
    host = os.environ.get("IOS_APPIUM_HOST") or DEFAULT_APPIUM_HOST
    return f"http://{host}:{appium_port()}"


def verify_appium(url: str, timeout: float = 3.0, *, quiet: bool = False) -> bool:
    """GET /status；Appium 2+ 的 base path 是 /，老版本可能挂在 /wd/hub。"""
    for suffix in ("/status", "/wd/hub/status"):
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}{suffix}", timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                if resp.status == 200 and "value" in body:
                    return True
        except Exception as e:
            if not quiet:
                SLog.d(TAG, f"appium verify failed {url}{suffix}: {e}")
    return False


def xcuitest_driver_installed(bin_path: Optional[str] = None) -> Optional[bool]:
    """True/False/None(判断不了)。只用于给出更准确的报错，不阻断启动。"""
    bin_path = bin_path or appium_bin()
    if not bin_path:
        return None
    try:
        r = subprocess.run(
            [bin_path, "driver", "list", "--installed"],
            capture_output=True,
            text=True,
            timeout=60,
            errors="ignore",
        )
        out = f"{r.stdout or ''}{r.stderr or ''}".lower()
        if not out.strip():
            return None
        return "xcuitest" in out
    except Exception as e:
        SLog.d(TAG, f"appium driver list failed: {e}")
        return None


# --------------------------------------------------------------------------- #
# pid 文件（与 ios_runtime.py 同风格）
# --------------------------------------------------------------------------- #
def _read_pid(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def _write_pid(path: str, pid: int) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(pid))


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_appium_errors(max_lines: int = 30) -> str:
    try:
        with open(APPIUM_LOG, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return ""
    hits = [ln for ln in lines if "[error]" in ln.lower() or "error:" in ln.lower()]
    return "".join((hits or lines)[-max_lines:])


# --------------------------------------------------------------------------- #
# server 生命周期
# --------------------------------------------------------------------------- #
def start_appium_server(*, url: Optional[str] = None, timeout: float = 90.0) -> str:
    """拉起 appium server（后台进程），返回可用的 base URL。"""
    target = (url or appium_server_url()).rstrip("/")
    if verify_appium(target, quiet=True):
        SLog.i(TAG, f"appium already running at {target}")
        return target

    bin_path = appium_bin()
    if not bin_path:
        raise RuntimeError(_INSTALL_HINT)

    if xcuitest_driver_installed(bin_path) is False:
        raise RuntimeError(
            "appium 已安装但缺少 xcuitest driver。请执行：\n"
            "  appium driver install xcuitest"
        )

    old = _read_pid(APPIUM_PID_FILE)
    if _pid_alive(old):
        SLog.i(TAG, f"appium server already started here (pid {old}), waiting ...")
    else:
        port = appium_port()
        cmd = [
            bin_path,
            "server",
            "--address",
            os.environ.get("IOS_APPIUM_HOST") or DEFAULT_APPIUM_HOST,
            "--port",
            str(port),
            "--log-timestamp",
            "--local-timezone",
        ]
        extra = (os.environ.get("IOS_APPIUM_ARGS") or "").split()
        if extra:
            cmd.extend(extra)

        logf = open(APPIUM_LOG, "ab")
        logf.write(f"\n\n=== start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
        logf.flush()
        proc = subprocess.Popen(
            cmd,
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _write_pid(APPIUM_PID_FILE, proc.pid)
        SLog.i(TAG, f"appium server starting (pid {proc.pid}, port {port}), log {APPIUM_LOG}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if verify_appium(target, quiet=True):
            SLog.i(TAG, f"appium ready at {target}")
            return target
        pid = _read_pid(APPIUM_PID_FILE)
        if pid and not _pid_alive(pid):
            raise RuntimeError(
                f"appium server 启动后立即退出，见 {APPIUM_LOG}\n{_read_appium_errors()}"
            )
        time.sleep(1.0)

    raise RuntimeError(
        f"appium server 在 {timeout}s 内未就绪 ({target})\n"
        f"日志: {APPIUM_LOG}\n{_read_appium_errors()}"
    )


def stop_appium_server() -> None:
    """只停本模块拉起的 appium；外部启动的（无 pid 文件）不动。"""
    pid = _read_pid(APPIUM_PID_FILE)
    if _pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), 15)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        SLog.i(TAG, f"appium server stopped (pid {pid})")
    try:
        os.remove(APPIUM_PID_FILE)
    except OSError:
        pass


def ensure_appium_server(url: Optional[str] = None, *, auto_start: Optional[bool] = None) -> str:
    """返回可用的 appium base URL；auto_start 默认看 IOS_APPIUM_AUTOSTART（默认开）。"""
    target = (url or appium_server_url()).rstrip("/")
    if verify_appium(target, quiet=True):
        return target
    if auto_start is None:
        auto_start = os.environ.get("IOS_APPIUM_AUTOSTART", "1") != "0"
    if not auto_start:
        raise ConnectionError(
            f"appium server 未就绪 ({target})，且 IOS_APPIUM_AUTOSTART=0。"
            f"请手动执行: appium server --port {appium_port()}"
        )
    return start_appium_server(url=target)


# --------------------------------------------------------------------------- #
# WDA 获取策略
# --------------------------------------------------------------------------- #
def appium_wda_settings() -> dict:
    """
    本模块自管的 WDA 相关配置，**不依赖 ios_runtime**（那是 wda 后端的地盘）。

    沿用既有的 IOS_WDA_BUNDLE_ID / IOS_XCODE_ORG_ID 等变量名，这样原有配置不用改。
    """
    bundle = (
        os.environ.get("IOS_APPIUM_WDA_BUNDLE_ID")
        or os.environ.get("IOS_WDA_BUNDLE_ID")
        or "com.facebook.WebDriverAgentRunner.zaohaowu"
    )
    # updatedWDABundleId 要的是不带 .xctrunner 的 id，后缀由 driver 自己补。
    bundle = bundle.removesuffix(".xctrunner")
    team = (
        os.environ.get("IOS_XCODE_ORG_ID")
        or detect_xcode_team_id()
        or ""
    )
    return {
        "bundle": bundle,
        "team_id": team,
        "signing_id": os.environ.get("IOS_XCODE_SIGNING_ID") or "Apple Development",
        "port": int(os.environ.get("IOS_APPIUM_WDA_PORT") or DEFAULT_WDA_LOCAL_PORT),
    }


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #
def build_options(
    device: IOSDeviceInfo,
    *,
    bundle_id: Optional[str] = None,
) -> Any:
    """
    组装 XCUITestOptions —— 纯自管：WDA 一律由 Appium 自己 build + install + launch。

    不探测已在跑的 8100，也不使用设备上已装的 runner。相应地也**不设** wdaRemotePort：
    WDA 会以 USE_PORT=wdaLocalPort 重新编译，两端自动一致（见 appium-webdriveragent
    webdriveragent.js: wdaRemotePort ?? wdaLocalPort ?? 8100）。
    """
    from appium.options.ios import XCUITestOptions

    settings = appium_wda_settings()

    options = XCUITestOptions()
    options.udid = device.udid
    options.device_name = device.name or "iOS"
    if device.platform_version:
        options.platform_version = device.platform_version
    options.new_command_timeout = int(os.environ.get("IOS_APPIUM_COMMAND_TIMEOUT") or 300)
    options.no_reset = True
    options.set_capability("appium:skipLogCapture", True)
    options.wda_launch_timeout = int(os.environ.get("IOS_APPIUM_WDA_LAUNCH_TIMEOUT") or 120_000)
    options.wda_connection_timeout = int(
        os.environ.get("IOS_APPIUM_WDA_CONNECTION_TIMEOUT") or 240_000
    )
    if os.environ.get("IOS_APPIUM_SHOW_XCODE_LOG", "0") == "1":
        options.show_xcode_log = True
    if bundle_id:
        options.bundle_id = bundle_id

    options.updated_wda_bundle_id = settings["bundle"]
    options.wda_local_port = pick_wda_local_port(settings["port"])
    if settings["team_id"]:
        options.xcode_org_id = settings["team_id"]
        options.xcode_signing_id = settings["signing_id"]
    else:
        SLog.w(
            TAG,
            "未检测到签名 Team ID，Appium 编译 WDA 会失败。"
            "请在 Xcode 登录 Apple ID，或显式设置 IOS_XCODE_ORG_ID。",
        )

    raw = os.environ.get("IOS_APPIUM_CAPS")
    if raw:
        try:
            overrides = json.loads(raw) or {}
            for key, value in overrides.items():
                options.set_capability(key if ":" in key else f"appium:{key}", value)
            SLog.i(TAG, f"已合并 IOS_APPIUM_CAPS 覆盖项: {sorted(overrides)}")
        except Exception as e:
            SLog.w(TAG, f"IOS_APPIUM_CAPS 解析失败，已忽略: {e}")

    SLog.i(TAG, f"XCUITest caps (Appium 自管 WDA): {options.to_capabilities()}")
    return options


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, int(port))) == 0


def pick_wda_local_port(preferred: int) -> int:
    """
    选一个空闲的**本地**转发端口（设备侧 WDA 仍是 8100，只是本地端口不同）。

    wda 后端会用 iproxy 长期占住 8100，且进程残留时端口还在但 WDA 已死 —— 直接抢会让
    Appium 报 "port #8100 is occupied"。这里主动让路，避免两个后端互相踩。
    """
    env = os.environ.get("IOS_APPIUM_WDA_LOCAL_PORT")
    if env:
        return int(env)
    preferred = int(preferred)
    if not _port_in_use(preferred):
        return preferred
    for port in range(preferred + 1, preferred + 21):
        if not _port_in_use(port):
            SLog.w(
                TAG,
                f"本地端口 {preferred} 被占用（多为 wda 后端遗留的 iproxy），"
                f"Appium 改用 wdaLocalPort={port}",
            )
            return port
    raise RuntimeError(
        f"{preferred}~{preferred + 20} 全部被占用，无法为 Appium 分配本地转发端口。"
        "请清理残留的 iproxy，或用 IOS_APPIUM_WDA_LOCAL_PORT 指定。"
    )


def start_environment(test_subject: Optional[str] = None) -> str:
    """对齐 ios_runtime.start_environment：返回 appium server base URL。"""
    if test_subject:
        os.environ["IOS_UDID"] = test_subject
    url = ensure_appium_server()
    SLog.i(TAG, f"appium ready: {url}")
    return url


def stop_environment() -> None:
    stop_appium_server()
    SLog.i(TAG, "appium environment stopped")
