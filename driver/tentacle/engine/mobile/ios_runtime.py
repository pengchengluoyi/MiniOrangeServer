# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""iOS WDA lifecycle (Xcode Test / iproxy) for IOSEngine."""
from __future__ import annotations

import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from driver.tentacle.engine.mobile.ios_config import (
    detect_xcode_team_id,
    probe_wda_url,
    resolve_device,
    verify_wda_url,
)
from script.log import SLog

TAG = "IOSRuntime"
WDA_PID_FILE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "miniorange-wda-xcodebuild.pid")
IPROXY_PID_FILE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "miniorange-wda-iproxy.pid")
WDA_LOG = os.path.join(os.environ.get("TMPDIR", "/tmp"), "miniorange-wda-xcodebuild.log")
DEFAULT_WDA_URL = "http://127.0.0.1:8100"
DEFAULT_WDA_PORT = 8100
def _wda_project_dir() -> str:
    return os.environ.get(
        "WDA_PROJECT_DIR",
        os.path.expanduser("~/code/WebDriverAgent"),
    )


def _wda_project_path() -> str:
    return os.path.join(_wda_project_dir(), "WebDriverAgent.xcodeproj")


def _venv_bin(name: str) -> str:
    prefix = os.environ.get("VIRTUAL_ENV") or os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "..", ".venv"
    )
    path = os.path.join(prefix, "bin", name)
    if os.path.isfile(path):
        return path
    found = shutil.which(name)
    return found or name


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


def wda_settings() -> dict:
    import builtins

    from server.services.device_service import DeviceService

    preferred = getattr(builtins, "TARGET_DEVICE_SN", None) or DeviceService.pick_sn(device_type="ios")
    device = resolve_device(test_subject=preferred)
    bundle = (
        os.environ.get("IOS_WDA_BUNDLE_ID")
        or "com.facebook.WebDriverAgentRunner.zaohaowu"
    )
    bundle = bundle.removesuffix(".xctrunner")
    team = (
        os.environ.get("IOS_XCODE_ORG_ID")
        or detect_xcode_team_id()
        or ""
    )
    port = int(os.environ.get("IOS_WDA_PORT") or DEFAULT_WDA_PORT)
    url = os.environ.get("IOS_WDA_URL") or f"http://127.0.0.1:{port}"
    return {
        "udid": device.udid,
        "bundle": bundle,
        "team_id": team,
        "port": port,
        "url": url.rstrip("/"),
        "project": _wda_project_path(),
        "project_dir": _wda_project_dir(),
    }


def mount_developer_image(udid: str) -> bool:
    if os.environ.get("IOS_SKIP_DEVELOPER_MOUNT", "0") == "1":
        SLog.d(TAG, "IOS_SKIP_DEVELOPER_MOUNT=1, skip pymobiledevice3 auto-mount")
        return False
    pymd3 = _venv_bin("pymobiledevice3")
    try:
        r = subprocess.run(
            [pymd3, "mounter", "auto-mount", "--udid", udid],
            capture_output=True,
            text=True,
            timeout=120,
            errors="ignore",
        )
        if r.returncode == 0:
            SLog.i(TAG, "Developer image mounted")
            return True
        err = (r.stderr or r.stdout or "").strip()
        short = err if len(err) <= 400 else err[:400] + "…"
        if "rate limit" in err.lower() or "GithubRateLimit" in err:
            SLog.w(
                TAG,
                "auto-mount skipped (GitHub API rate limit on DeveloperDiskImage). "
                "Use GITHUB_TOKEN for developer_disk_image, or set IOS_SKIP_DEVELOPER_MOUNT=1. "
                f"Detail: {short}",
            )
        else:
            SLog.w(TAG, f"auto-mount failed: {short}")
    except Exception as e:
        SLog.w(TAG, f"auto-mount skipped: {e}")
    return False


def _start_iproxy(udid: str, port: int) -> Optional[int]:
    if not shutil.which("iproxy"):
        SLog.w(TAG, "iproxy not found (brew install libimobiledevice)")
        return None
    old = _read_pid(IPROXY_PID_FILE)
    if _pid_alive(old):
        return old
    subprocess.run(["pkill", "-f", f"iproxy.*{port}"], check=False)
    log_path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "miniorange-wda-iproxy.log")
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(
            ["iproxy", str(port), str(port), "-u", udid],
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    _write_pid(IPROXY_PID_FILE, proc.pid)
    SLog.i(TAG, f"iproxy {port} started (pid {proc.pid})")
    return proc.pid


def _stop_iproxy() -> None:
    pid = _read_pid(IPROXY_PID_FILE)
    if _pid_alive(pid):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    try:
        os.remove(IPROXY_PID_FILE)
    except OSError:
        pass


def _read_xcodebuild_errors(max_lines: int = 30) -> str:
    try:
        with open(WDA_LOG, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return ""
    hits = [ln for ln in lines if "error:" in ln.lower() or "TEST FAILED" in ln]
    if hits:
        return "".join(hits[-max_lines:])
    return "".join(lines[-max_lines:])


def _xcodebuild_failed_signing() -> bool:
    text = _read_xcodebuild_errors(80).lower()
    keys = (
        "no account for team",
        "conflicting provisioning",
        "no profiles for",
        "signing certificate",
        "test failed",
    )
    return any(k in text for k in keys)


def _run_applescript(script: str) -> bool:
    r = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0 and (r.stderr or "").strip():
        SLog.d(TAG, f"osascript: {(r.stderr or r.stdout or '').strip()}")
    return r.returncode == 0


def _trigger_xcode_test() -> None:
    """通过 Xcode GUI 执行 Test (⌘U)，与手动点击效果一致。"""
    _run_applescript(
        """
tell application "Xcode" to activate
delay 2
tell application "System Events"
    keystroke "u" using command down
end tell
"""
    )
    SLog.i(
        TAG,
        "已发送 Xcode Test (⌘U)。若未启动，请在 系统设置 -> 隐私 -> 辅助功能 中允许终端/Cursor。",
    )


def _stop_xcode_test() -> None:
    """停止 Xcode 里正在跑的 Test，并结束本机 WDA 相关进程。"""
    if os.environ.get("IOS_WDA_STOP_XCODE", "1") == "0":
        return
    if not shutil.which("osascript"):
        return
    stopped = _run_applescript(
        """
tell application "Xcode" to activate
delay 0.5
tell application "System Events"
    tell process "Xcode"
        try
            click menu item "Stop" of menu "Product" of menu bar 1
        on error
            try
                click menu item "停止" of menu "产品" of menu bar 1
            on error
                keystroke "." using command down
            end try
        end try
    end tell
end tell
"""
    )
    if stopped:
        SLog.i(TAG, "已发送 Xcode Stop Test（产品 -> 停止 / ⌘.）")
    subprocess.run(["pkill", "-f", "WebDriverAgentRunner"], check=False)
    subprocess.run(["pkill", "-f", "xctest.*WebDriverAgent"], check=False)


def _quit_xcode() -> None:
    """退出 Xcode：先 AppleScript，仍存活则 SIGTERM；可选 SIGKILL。"""
    if os.environ.get("IOS_WDA_QUIT_XCODE", "1") == "0":
        return
    if not shutil.which("osascript"):
        return
    _run_applescript(
        """
tell application "Xcode" to activate
delay 0.3
tell application "Xcode"
    if it is running then
        quit saving no
    end if
end tell
"""
    )
    time.sleep(2)
    if subprocess.run(["pgrep", "-x", "Xcode"], capture_output=True).returncode == 0:
        SLog.w(TAG, "Xcode 仍在运行，发送 SIGTERM (killall -TERM Xcode)")
        subprocess.run(["killall", "-TERM", "Xcode"], check=False)
        time.sleep(1)
    if (
        subprocess.run(["pgrep", "-x", "Xcode"], capture_output=True).returncode == 0
        and os.environ.get("IOS_WDA_KILLALL_XCODE", "0") == "1"
    ):
        SLog.w(TAG, "IOS_WDA_KILLALL_XCODE=1，发送 SIGKILL")
        subprocess.run(["killall", "-9", "Xcode"], check=False)
        time.sleep(0.5)
    if subprocess.run(["pgrep", "-x", "Xcode"], capture_output=True).returncode != 0:
        SLog.i(TAG, "Xcode 已退出")
    else:
        SLog.w(TAG, "Xcode 仍未退出，请检查未保存对话框或手动退出")


def _open_xcode_project(settings: dict) -> None:
    project = settings["project"]
    if not os.path.isdir(project):
        raise FileNotFoundError(
            f"WebDriverAgent not found: {project}\n"
            "git clone https://github.com/appium/WebDriverAgent.git ~/code/WebDriverAgent"
        )
    subprocess.run(["open", project], check=False)
    SLog.i(TAG, f"Opening {project} in Xcode ...")
    time.sleep(8)


def _start_xcode_gui(settings: dict) -> None:
    _open_xcode_project(settings)
    _trigger_xcode_test()


def _start_xcodebuild(settings: dict) -> int:
    project = settings["project"]
    if not os.path.isdir(project):
        raise FileNotFoundError(
            f"WebDriverAgent not found: {project}\n"
            "git clone https://github.com/appium/WebDriverAgent.git ~/code/WebDriverAgent"
        )

    old = _read_pid(WDA_PID_FILE)
    if _pid_alive(old):
        SLog.i(TAG, f"xcodebuild already running (pid {old})")
        return old

    env = os.environ.copy()
    # 使用工程内已保存的签名（Xcode 里配好的 Team），不要覆盖 DEVELOPMENT_TEAM
    cmd = [
        "xcodebuild",
        "test",
        "-project",
        os.path.join(settings["project_dir"], "WebDriverAgent.xcodeproj"),
        "-scheme",
        "WebDriverAgentRunner",
        "-destination",
        f"id={settings['udid']}",
        "-allowProvisioningUpdates",
        f"WDA_PRODUCT_BUNDLE_IDENTIFIER={settings['bundle']}",
        "ONLY_ACTIVE_ARCH=YES",
    ]
    logf = open(WDA_LOG, "ab")
    logf.write(f"\n\n=== start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
    logf.flush()
    proc = subprocess.Popen(
        cmd,
        cwd=settings["project_dir"],
        stdout=logf,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    _write_pid(WDA_PID_FILE, proc.pid)
    SLog.i(TAG, f"xcodebuild test started (pid {proc.pid}), log {WDA_LOG}")
    return proc.pid


def wait_wda_ready(
    url: str,
    timeout: float = 240.0,
    poll: float = 2.0,
) -> bool:
    deadline = time.time() + timeout
    last_log = 0.0
    while time.time() < deadline:
        if verify_wda_url(url, quiet=True):
            return True
        now = time.time()
        if now - last_log >= 30.0:
            remain = int(deadline - now)
            SLog.i(TAG, f"Waiting for WDA at {url} ... ({remain}s left)")
            last_log = now
        pid = _read_pid(WDA_PID_FILE)
        if pid and not _pid_alive(pid):
            SLog.e(TAG, f"xcodebuild exited early, see {WDA_LOG}")
            raise RuntimeError(
                "xcodebuild failed before WDA was ready.\n"
                f"{_read_xcodebuild_errors()}\n"
                f"Full log: {WDA_LOG}"
            )
        time.sleep(poll)
    return False


def _wda_mode() -> str:
    """gui=自动按 Xcode Test；build=仅 xcodebuild；auto=先 build 失败再 gui"""
    return (os.environ.get("IOS_WDA_MODE") or "gui").strip().lower()


def _should_iproxy(mode: str, use_iproxy: bool) -> bool:
    if use_iproxy:
        return True
    env = os.environ.get("IOS_WDA_IPROXY")
    if env is not None:
        return env != "0"
    # 自动化启动时 CLI 往往不会转发 8100，默认开 iproxy 更稳
    return mode in ("gui", "build", "auto")


def start_wda(
    *,
    url: Optional[str] = None,
    timeout: float = 240.0,
    use_iproxy: bool = False,
    mode: Optional[str] = None,
) -> str:
    """启动 WDA，返回可用的 base URL。"""
    settings = wda_settings()
    target = (url or settings["url"]).rstrip("/")
    mode = mode or _wda_mode()

    if verify_wda_url(target):
        SLog.i(TAG, f"WDA already ready at {target}")
        return target

    mount_developer_image(settings["udid"])
    if _should_iproxy(mode, use_iproxy):
        _start_iproxy(settings["udid"], settings["port"])

    try:
        os.remove(WDA_PID_FILE)
    except OSError:
        pass

    build_tried = False
    if mode in ("auto", "build"):
        build_tried = True
        SLog.i(TAG, f"Starting WDA via xcodebuild (mode={mode}) ...")
        _start_xcodebuild(settings)
        try:
            if wait_wda_ready(target, timeout=min(timeout, 180.0)):
                SLog.i(TAG, f"WDA ready at {target}")
                return target
        except RuntimeError as e:
            if mode == "build":
                raise
            SLog.w(TAG, f"xcodebuild path failed: {e}")

    if mode in ("auto", "gui"):
        SLog.i(TAG, "Starting WDA via Xcode Test (⌘U) ...")
        _start_xcode_gui(settings)
        # 首次编译安装常需 1–3 分钟
        time.sleep(15)
        if wait_wda_ready(target, timeout=timeout):
            SLog.i(TAG, f"WDA ready at {target}")
            return target

    hint = (
        "1. Xcode -> Settings -> Accounts 已登录 Apple ID (Team 474L99R7GT)\n"
        "2. WebDriverAgentRunner 签名无误后重试\n"
        "3. 或: IOS_WDA_MODE=gui Manager().execute_interface(...) 或 IOSEngine.start()\n"
    )
    if build_tried and _xcodebuild_failed_signing():
        hint = (
            "xcodebuild 无法使用 Xcode 账号签名（需在 Xcode 里登录 Team）。\n"
            "已默认改用 IOS_WDA_MODE=gui；请确认辅助功能权限并看到 Xcode 在跑 Test。\n"
        ) + hint

    raise RuntimeError(
        f"WDA not ready at {target} after {timeout}s.\n"
        f"xcodebuild log: {WDA_LOG}\n"
        f"{_read_xcodebuild_errors()}\n"
        f"{hint}"
    )


def stop_wda(*, quit_xcode: bool = True) -> None:
    _stop_xcode_test()
    if quit_xcode:
        _quit_xcode()
    pid = _read_pid(WDA_PID_FILE)
    if _pid_alive(pid):
        try:
            os.killpg(pid, 15)
        except (OSError, ProcessLookupError):
            try:
                os.kill(pid, 15)
            except OSError:
                pass
        SLog.i(TAG, f"Stopped xcodebuild pid {pid}")
    try:
        os.remove(WDA_PID_FILE)
    except OSError:
        pass
    subprocess.run(["pkill", "-f", "xcodebuild.*WebDriverAgent"], check=False)
    _stop_iproxy()
    port = int(os.environ.get("IOS_WDA_PORT") or DEFAULT_WDA_PORT)
    subprocess.run(["pkill", "-f", f"iproxy.*{port}"], check=False)
    SLog.i(TAG, "WDA / iproxy / xcodebuild stopped")


def ensure_wda(url: Optional[str] = None, auto_start: bool = True) -> str:
    target = (url or probe_wda_url() or DEFAULT_WDA_URL).rstrip("/")
    if verify_wda_url(target):
        return target
    if not auto_start:
        raise ConnectionError(f"WDA not ready at {target}")
    return start_wda(url=target)



def start_environment(
    test_subject: Optional[str] = None,
    *,
    start_wda_flag: bool = True,
) -> str:
    if test_subject:
        os.environ["IOS_UDID"] = test_subject
    wda_url = ""
    if start_wda_flag:
        wda_url = start_wda()
        SLog.i(TAG, f"WDA ready: {wda_url}")
    return wda_url


def stop_environment(*, quit_xcode: bool = True) -> None:
    stop_wda(quit_xcode=quit_xcode)
    SLog.i(TAG, "iOS environment stopped")
