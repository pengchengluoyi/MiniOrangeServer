#!/usr/bin/env python3
# -*-coding:utf-8 -*-
"""
独立的 iOS × Appium 脚本 —— 只用 Appium / Apple 官方能力，可直接运行。

刻意做成自包含：不 import 本项目任何模块（Manager / SLog / engine 体系一概不碰），
所以它不受项目接线影响，也能单独拷出去用。依赖只有官方的 Appium-Python-Client、
官方 appium CLI（含 xcuitest driver 自带脚本）与 Apple 的 xcrun devicectl。

────────────────────────────────────────────────────────────────────────────
先看环境体检（不需要会话，任何时候都能跑）
    python driver/tentacle/engine/mobile/appium_ios.py doctor

把设备准备成可跑 XCTest（打开 Xcode 设备窗口并轮询 DDI）
    python .../appium_ios.py prepare

把 WebDriverAgent 装到手机上（官方 xcuitest 脚本，不开 Xcode GUI）
    python .../appium_ios.py setup-wda

设备与应用（需要会话）
    python .../appium_ios.py info
    python .../appium_ios.py apps
    python .../appium_ios.py install /path/to/App.ipa
    python .../appium_ios.py uninstall com.example.app
    python .../appium_ios.py launch com.apple.Preferences
    python .../appium_ios.py terminate com.apple.Preferences
    python .../appium_ios.py state com.apple.Preferences

界面操作（需要会话）
    python .../appium_ios.py shot out.png
    python .../appium_ios.py source out.xml
    python .../appium_ios.py tap 200 400
    python .../appium_ios.py swipe up
    python .../appium_ios.py press home
    python .../appium_ios.py type "hello"
    python .../appium_ios.py click --text "设置"
    python .../appium_ios.py click --predicate "name == 'Cancel'"
    python .../appium_ios.py unlock

一次跑通全部只读动作，用于验收
    python .../appium_ios.py smoke

通用参数
    --udid <udid>     指定设备（默认取第一台 USB 真机）
    --bundle <id>     指定被测应用，会话建立时直接拉起
    --server <url>    appium server 地址（默认 http://127.0.0.1:4723）
    --no-autostart    不自动拉起 appium server
    --skip-ddi        跳过 DDI 前置检查
    --verbose         打印 xcodebuild 日志（showXcodeLog）
────────────────────────────────────────────────────────────────────────────
环境变量（都可选，命令行参数优先）
    IOS_UDID, IOS_APPIUM_URL, IOS_APPIUM_BIN,
    IOS_XCODE_ORG_ID, IOS_XCODE_SIGNING_ID,
    IOS_WDA_BUNDLE_ID, IOS_APPIUM_WDA_LOCAL_PORT,
    IOS_APPIUM_CAPS   （JSON，追加/覆盖任意 capability）
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Any, NoReturn, Optional
from urllib.parse import urlparse

DEFAULT_SERVER = "http://127.0.0.1:4723"
DEFAULT_WDA_BUNDLE = "com.facebook.WebDriverAgentRunner"
DEFAULT_WDA_LOCAL_PORT = 8100
APPIUM_PID_FILE = os.path.join(tempfile.gettempdir(), "appium-ios-script.pid")
APPIUM_LOG = os.path.join(tempfile.gettempdir(), "appium-ios-script.log")

OK, BAD, WARN, DOT = "✔", "✖", "!", "·"


# ─────────────────────────────── 小工具 ─────────────────────────────── #
def log(msg: str = "") -> None:
    print(msg, flush=True)


def die(msg: str, code: int = 1) -> NoReturn:
    print(f"\n{BAD} {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, errors="ignore"
    )


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, int(port))) == 0


def http_ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200 and "value" in r.read().decode("utf-8", "ignore")
    except Exception:
        return False


# ────────────────────────── appium CLI / server ────────────────────────── #
def appium_bin() -> Optional[str]:
    explicit = os.environ.get("IOS_APPIUM_BIN")
    if explicit:
        return explicit if os.path.isfile(explicit) else shutil.which(explicit)
    found = shutil.which("appium")
    if found:
        return found
    for pattern in (
        "/opt/homebrew/bin/appium",
        "/usr/local/bin/appium",
        os.path.expanduser("~/.nvm/versions/node/*/bin/appium"),
    ):
        for path in sorted(glob.glob(pattern), reverse=True):
            if os.path.isfile(path):
                return path
    return None


def appium_version(bin_path: str) -> str:
    r = run([bin_path, "--version"], timeout=60)
    return (r.stdout or "").strip() or "?"


def xcuitest_version(bin_path: str) -> Optional[str]:
    r = run([bin_path, "driver", "list", "--installed"], timeout=90)
    text = f"{r.stdout or ''}{r.stderr or ''}"
    m = re.search(r"xcuitest@([\w.\-]+)", text)
    return m.group(1) if m else None


def server_running(url: str) -> bool:
    return http_ok(f"{url.rstrip('/')}/status")


def start_server(url: str, timeout: float = 90.0) -> str:
    """拉起 appium server（官方 `appium server`），返回可用的 base URL。"""
    url = url.rstrip("/")
    if server_running(url):
        log(f"{DOT} appium server 已在运行 {url}")
        return url

    bin_path = appium_bin()
    if not bin_path:
        die(
            "找不到 appium CLI。安装一次即可：\n"
            "    npm i -g appium\n"
            "    appium driver install xcuitest"
        )
    if xcuitest_version(bin_path) is None:
        die("appium 已安装但缺少 xcuitest driver：\n    appium driver install xcuitest")

    port = str(urlparse(url).port or 4723)
    host = urlparse(url).hostname or "127.0.0.1"

    logf = open(APPIUM_LOG, "ab")
    logf.write(f"\n=== start {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
    logf.flush()
    proc = subprocess.Popen(
        [bin_path, "server", "--address", host, "--port", port, "--log-timestamp"],
        stdout=logf,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    with open(APPIUM_PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))
    log(f"{DOT} 已拉起 appium server pid={proc.pid} 日志 {APPIUM_LOG}")

    deadline = time.time() + timeout
    while time.time() < deadline:
        if server_running(url):
            log(f"{OK} appium server 就绪 {url}")
            return url
        if proc.poll() is not None:
            die(f"appium server 启动后立即退出，见 {APPIUM_LOG}")
        time.sleep(1.0)
    die(f"appium server {timeout}s 内未就绪，见 {APPIUM_LOG}")


# ──────────────────────────── 设备 / DDI ──────────────────────────── #
def list_devices(connected_only: bool = False) -> list[dict]:
    """
    用 Apple 官方 devicectl 列出设备。

    注意 devicectl 会把**所有配过对的**设备都列出来，包括当前没连接的（transport 为空）。
    对没连接的设备谈 DDI 毫无意义，所以默认带上 transport / tunnel 供调用方判断。
    """
    if not shutil.which("xcrun"):
        return []
    out_path = os.path.join(tempfile.gettempdir(), "appium-ios-devices.json")
    run(
        ["xcrun", "devicectl", "list", "devices", "--quiet", "--json-output", out_path],
        timeout=90,
    )
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return []
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass

    devices = []
    for d in (payload.get("result") or {}).get("devices", []) or []:
        props = d.get("deviceProperties") or {}
        hw = d.get("hardwareProperties") or {}
        conn = d.get("connectionProperties") or {}
        udid = hw.get("udid") or ""
        if not udid:
            continue
        transport = conn.get("transportType")
        entry = {
            "udid": udid,
            "name": props.get("name") or "iOS",
            "model": hw.get("marketingName") or hw.get("productType") or "",
            "os": props.get("osVersionNumber") or "",
            "transport": transport or "",
            "tunnel": conn.get("tunnelState") or "",
            "paired": conn.get("pairingState") or "",
            "connected": bool(transport),
            "wired": transport == "wired",
        }
        if connected_only and not entry["connected"]:
            continue
        devices.append(entry)
    return devices


def is_simulator_udid(udid: str) -> bool:
    """模拟器 UDID 是标准 UUID；真机是 0000xxxx-xxxxxxxxxxxxxxxx。"""
    return bool(
        re.fullmatch(
            r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
            udid or "",
        )
    )


def list_simulators(booted_only: bool = False) -> list[dict]:
    r = run(["xcrun", "simctl", "list", "devices", "available", "-j"], timeout=60)
    if r.returncode != 0 or not (r.stdout or "").strip():
        return []
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    out = []
    for runtime, entries in (payload.get("devices") or {}).items():
        os_ver = runtime.rsplit(".", 1)[-1].replace("iOS-", "").replace("-", ".")
        for entry in entries or []:
            if not entry.get("isAvailable"):
                continue
            state = entry.get("state") or ""
            if booted_only and state != "Booted":
                continue
            udid = entry.get("udid") or ""
            if not udid:
                continue
            out.append(
                {
                    "udid": udid,
                    "name": entry.get("name") or "Simulator",
                    "model": "Simulator",
                    "os": os_ver,
                    "transport": "simulator",
                    "tunnel": state,
                    "paired": "",
                    "connected": state == "Booted",
                    "wired": False,
                    "simulator": True,
                }
            )
    return out


def _simulator_already_booted(udid: str) -> Optional[dict]:
    for s in list_simulators():
        if s["udid"] == udid and s["tunnel"] == "Booted":
            return s
    return None


def boot_simulator(udid: str, timeout: float = 120.0) -> None:
    already = _simulator_already_booted(udid)
    if already:
        log(f"{OK} 模拟器已在运行 {already['name']} / iOS {already['os']}")
        run(["open", "-a", "Simulator"], timeout=30)
        return

    r = run(["xcrun", "simctl", "boot", udid], timeout=60)
    err = (r.stderr or r.stdout or "").strip()
    # 146 / "already booted" / Xcode 26 的 405 "current state: Booted" 都表示已经开着
    booted_ok = (
        r.returncode in (0, 146)
        or "already booted" in err.lower()
        or "current state: booted" in err.lower()
    )
    if not booted_ok:
        log(f"{WARN} simctl boot: {err or r.returncode}")
    run(["open", "-a", "Simulator"], timeout=30)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in list_simulators():
            if s["udid"] == udid and s["tunnel"] == "Booted":
                log(f"{OK} 模拟器已启动 {s['name']} / iOS {s['os']}")
                return
        time.sleep(2.0)
    die(f"模拟器 {udid} 在 {timeout}s 内未进入 Booted")


def rogue_tunnel_pids() -> list[str]:
    """
    找出 appium-ios-remotexpc 的 tunnel-creation 进程。

    它会以 root 身份独占设备的 RemoteXPC 隧道，Apple 的 CoreDevice 就建不起自己的隧道
    （devicectl 里表现为 tunnelState=disconnected），继而挂不上 DDI。
    """
    r = run(["pgrep", "-f", "tunnel-creation"], timeout=30)
    return [p for p in (r.stdout or "").split() if p.strip()]


def _parse_version(text: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums[:3]) if nums else ()


def ddi_available(udid: str) -> Optional[bool]:
    """DDI 服务是否可用。True / False / None(判断不了)。只读，不触发挂载。"""
    if not (udid and shutil.which("xcrun")):
        return None
    out_path = os.path.join(tempfile.gettempdir(), f"appium-ios-ddi-{udid[:12]}.json")
    try:
        run(
            [
                "xcrun", "devicectl", "device", "info", "details",
                "--device", udid, "--quiet", "--json-output", out_path,
            ],
            timeout=90,
        )
        with open(out_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        value = (payload.get("result") or {}).get("deviceProperties", {}).get(
            "ddiServicesAvailable"
        )
        return None if value is None else bool(value)
    except Exception:
        return None
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


def ddi_mount_error(udid: str) -> str:
    """触发一次开发者服务查询，把 CoreDevice 的真实挂载错误抠出来。"""
    if not (udid and shutil.which("xcrun")):
        return ""
    r = run(
        ["xcrun", "devicectl", "device", "info", "processes", "--device", udid],
        timeout=90,
    )
    text = f"{r.stdout or ''}{r.stderr or ''}"
    if "ddiServicesAvailable" in text.lower() and "error" not in text.lower():
        return ""
    keys = (
        "kAMAuthInstallErrorHTTPUnauthorized",
        "kAMDMobileImageMounterNetworkUnauthorizedError",
        "kAMDMobileImageMounterPersonalizedBundleMissingVariantError",
        "kAMDMobileImageMounterTATSUDeclinedAuthorization",
        "developer disk image could not be mounted",
    )
    hits: list[str] = []
    for line in text.splitlines():
        low = line.lower()
        if any(k.lower() in low for k in keys) or "3501" in line:
            hits.append(line.strip())
    if not hits:
        return ""
    for line in hits:
        if "3501" in line or "unauthorized" in line.lower() or "tatsu" in line.lower():
            return line
    return hits[0]


DDI_HELP = """DDI（Developer Disk Image）未挂载。iOS 17+ 启动 XCTest/WebDriverAgent 必须先有它，
这一层由 Apple 的 CoreDevice 负责，Appium 无法绕过。

若错误里有 kAMAuthInstallErrorHTTPUnauthorized / 3501，或 Xcode
Devices and Simulators 已经显示同样红字：Appium / prepare / 打开窗口都绕不过。
这一层是 Apple CoreDevice 向 gs.apple.com 要个性化签名，被拒绝了。

必须在本机 Terminal.app 里做（Cursor 里 sudo 读不到密码）：
  1. 升级 macOS，使其大.小版本不低于手机 iOS（当前常见坑：macOS 26.3 vs iOS 26.4）
  2. sudo kill <tunnel-creation 的 pid>     # doctor 会打印
  3. sudo installer -pkg /Applications/Xcode.app/Contents/Resources/Packages/MobileDevice.pkg -target /
     sudo installer -pkg /Applications/Xcode.app/Contents/Resources/Packages/MobileDeviceDevelopment.pkg -target /
     sudo installer -pkg /Applications/Xcode.app/Contents/Resources/Packages/XcodeSystemResources.pkg -target /
  4. 重启手机，解锁，插稳 USB，再跑 doctor

真机 DDI 未就绪时，可用模拟器先把 Appium 跑通（模拟器不需要 DDI）：
  python .../appium_ios.py smoke --simulator

通用排查：
  1. 设置 → 隐私与安全性 → 开发者模式 打开
  2. xcrun devicectl manage ddis update
自查：xcrun devicectl device info details --device <udid> | grep ddiServicesAvailable
要强行继续：加 --skip-ddi（仍会在 xcodebuild code 70 失败，只是跳过前置检查）"""


def detect_team_id() -> Optional[str]:
    r = run(["security", "find-identity", "-v", "-p", "codesigning"], timeout=60)
    for line in (r.stdout or "").splitlines():
        if "Apple Development" not in line:
            continue
        # 行尾形如：... (474L99R7GT)"  —— 注意带结尾引号
        m = re.search(r"\(([A-Z0-9]{10})\)\"?\s*$", line)
        if m:
            return m.group(1)
    return None


def pick_local_port(preferred: int = DEFAULT_WDA_LOCAL_PORT) -> int:
    env = os.environ.get("IOS_APPIUM_WDA_LOCAL_PORT")
    if env:
        return int(env)
    if not port_in_use(preferred):
        return preferred
    for p in range(preferred + 1, preferred + 21):
        if not port_in_use(p):
            log(f"{WARN} 本地端口 {preferred} 被占用，改用 {p}")
            return p
    die(f"{preferred}~{preferred+20} 全被占用，用 IOS_APPIUM_WDA_LOCAL_PORT 指定一个")


# ──────────────────────────── doctor ──────────────────────────── #
def cmd_doctor(args: argparse.Namespace) -> int:
    log("═══ Appium / iOS 环境体检 ═══\n")
    fatal = 0

    bin_path = appium_bin()
    if bin_path:
        log(f"{OK} appium CLI      {bin_path}  (v{appium_version(bin_path)})")
        xc = xcuitest_version(bin_path)
        if xc:
            log(f"{OK} xcuitest driver v{xc}")
        else:
            log(f"{BAD} xcuitest driver 未安装 → appium driver install xcuitest")
            fatal += 1
    else:
        log(f"{BAD} appium CLI      未找到 → npm i -g appium && appium driver install xcuitest")
        fatal += 1

    url = args.server
    log(f"{OK if server_running(url) else DOT} appium server   {url} "
        f"{'运行中' if server_running(url) else '未运行（会话时自动拉起）'}")

    xcode = run(["xcodebuild", "-version"], timeout=60).stdout or ""
    log(f"{OK if xcode else BAD} Xcode           {xcode.splitlines()[0] if xcode else '未找到'}")
    mac = run(["sw_vers", "-productVersion"], timeout=30).stdout.strip()
    log(f"{DOT} macOS           {mac}")

    team = os.environ.get("IOS_XCODE_ORG_ID") or detect_team_id()
    if team:
        log(f"{OK} 签名 Team       {team}")
    else:
        log(f"{BAD} 签名 Team       未检测到 → 在 Xcode 登录 Apple ID，或设 IOS_XCODE_ORG_ID")
        fatal += 1

    log("\n─── 设备 ───")
    every = list_devices()
    connected = [d for d in every if d["connected"]]
    offline = [d for d in every if not d["connected"]]

    mac_ver = _parse_version(mac)
    if not connected:
        log(f"{BAD} 没有已连接的设备。插上数据线、解锁手机、信任本电脑。")
        fatal += 1
    for d in connected:
        ddi = ddi_available(d["udid"])
        mark = OK if ddi else (WARN if ddi is None else BAD)
        ddi_txt = {True: "DDI 就绪", False: "DDI 未挂载", None: "DDI 状态未知"}[ddi]
        log(f"{mark} {d['name']} / {d['model']} / iOS {d['os']}")
        log(f"    udid={d['udid']}")
        log(f"    transport={d['transport']}  tunnel={d['tunnel']}  {d['paired']}  {ddi_txt}")
        if not d["wired"]:
            log(f"    {WARN} 非有线连接（{d['transport']}）—— Appium 真机需要 USB 有线")
        ios_ver = _parse_version(d["os"])
        if mac_ver and ios_ver and mac_ver[:2] < ios_ver[:2]:
            log(
                f"    {WARN} 宿主 macOS {mac} < 设备 iOS {d['os']}，"
                "CoreDevice 个性化 DDI 经常因此返回 HTTP 401"
            )
        if ddi is False:
            fatal += 1
            if d["wired"]:
                err = ddi_mount_error(d["udid"])
                if err:
                    log(f"    {BAD} 挂载失败：{err}")
    if offline:
        log(f"\n{DOT} 另有 {len(offline)} 台配过对但当前未连接的设备（对它们谈 DDI 无意义）：")
        for d in offline:
            log(f"    {d['name']} / iOS {d['os']}  ({d['tunnel']})")

    rogue = rogue_tunnel_pids()
    if rogue:
        log(f"\n{WARN} 检测到 appium-ios-remotexpc 的 tunnel-creation 进程：pid {', '.join(rogue)}")
        log("    属主若是 root，Cursor 里 sudo 读不到密码，请到 Terminal.app 执行：")
        log(f"    sudo kill {' '.join(rogue)}")

    sims = list_simulators()
    if sims:
        log("\n─── 模拟器（不需要 DDI）───")
        for s in sims[:6]:
            log(f"    {OK if s['connected'] else DOT} {s['name']} / iOS {s['os']}  {s['udid']}")
        log("    真机 DDI 未就绪时：python driver/tentacle/engine/mobile/appium_ios.py smoke --simulator")

    log("")
    if fatal:
        log(f"{BAD} 体检未通过：{fatal} 项阻塞。")
        if any(ddi_available(d["udid"]) is False for d in connected):
            log("\n" + DDI_HELP)
        return 1
    log(f"{OK} 体检通过，可以建立会话。")
    return 0


# ──────────────────────── 官方方式安装 WDA ──────────────────────── #
def cmd_prepare(args: argparse.Namespace) -> int:
    """打开 Xcode 设备窗口，触发官方 DDI 准备，并轮询直到可用。"""
    udid = resolve_udid(args)
    if ddi_available(udid):
        log(f"{OK} DDI 已就绪，可以直接 smoke / setup-wda")
        return 0

    log(f"{DOT} 打开 Xcode Devices 窗口并请求 enableForDevelopment")
    run(["open", "xcdevice://showDevicesWindow"], timeout=30)
    run(["open", f"xcdevice://enableForDevelopment?identifier={udid}"], timeout=30)
    log(f"{DOT} 请在 Xcode 里盯着这台 iPhone，等 Preparing device 结束（保持解锁）")

    deadline = time.time() + 180
    last_log = 0.0
    while time.time() < deadline:
        if ddi_available(udid):
            log(f"{OK} DDI 已挂上，可以跑 smoke")
            return 0
        now = time.time()
        if now - last_log >= 20:
            log(f"{DOT} 仍未挂上，还等 {int(deadline - now)}s …")
            last_log = now
        time.sleep(5.0)

    err = ddi_mount_error(udid)
    log(f"{BAD} 180s 内 DDI 仍未就绪" + (f"：{err}" if err else ""))
    log("\n" + DDI_HELP)
    return 1


def cmd_setup_wda(args: argparse.Namespace) -> int:
    """
    把 WebDriverAgent 装到设备上。

    真机没有官方的独立安装脚本 —— xcuitest 自带的 build-wda 只支持模拟器，open-wda 是把
    工程丢进 Xcode GUI（正好是我们要避开的）。真机的官方途径就是**建立一次会话**：
    Appium 会自己调 xcodebuild 编译、安装并拉起 WDA。所以这里就是跑一次会话再退出，
    跑完 WDA 就留在设备上了。
    """
    udid = resolve_udid(args)
    log(f"{DOT} 真机 WDA 安装 = 由 Appium 在建立会话时完成（官方途径，不开 Xcode GUI）")
    log(f"{DOT} 首次编译安装通常 1~3 分钟，请保持设备解锁并连线\n")
    with IOSSession(args) as d:
        size = d.get_window_size()
        log(f"{OK} WDA 已在设备上编译安装并运行，会话可用（屏幕 {size['width']}x{size['height']}）")
    log(f"{OK} 会话已退出；WDA 仍留在设备上，后续会话会更快")
    return 0


# ──────────────────────────── 会话 ──────────────────────────── #
class IOSSession:
    """官方 Appium XCUITest 会话的薄封装。用 with 语句保证 quit()。"""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.udid = resolve_udid(args)
        self.driver = None

    # ---------- capabilities：全部官方 ---------- #
    def build_options(self):
        from appium.options.ios import XCUITestOptions

        o = XCUITestOptions()
        o.udid = self.udid
        o.new_command_timeout = 300
        o.no_reset = True
        o.set_capability("appium:skipLogCapture", True)
        o.wda_launch_timeout = 120_000
        o.wda_connection_timeout = 240_000
        o.wda_local_port = pick_local_port()
        if is_simulator_udid(self.udid):
            # 模拟器不走真机签名 / 个性化 DDI
            pass
        else:
            o.updated_wda_bundle_id = (
                os.environ.get("IOS_WDA_BUNDLE_ID") or DEFAULT_WDA_BUNDLE
            ).removesuffix(".xctrunner")
            team = os.environ.get("IOS_XCODE_ORG_ID") or detect_team_id()
            if team:
                o.xcode_org_id = team
                o.xcode_signing_id = (
                    os.environ.get("IOS_XCODE_SIGNING_ID") or "Apple Development"
                )
        if self.args.verbose:
            o.show_xcode_log = True
        if getattr(self.args, "bundle", None):
            o.bundle_id = self.args.bundle

        raw = os.environ.get("IOS_APPIUM_CAPS")
        if raw:
            try:
                for k, v in (json.loads(raw) or {}).items():
                    o.set_capability(k if ":" in k else f"appium:{k}", v)
                log(f"{DOT} 已合并 IOS_APPIUM_CAPS")
            except Exception as e:
                log(f"{WARN} IOS_APPIUM_CAPS 解析失败，忽略：{e}")
        return o

    def __enter__(self):
        skip_ddi = self.args.skip_ddi or is_simulator_udid(self.udid)
        if not skip_ddi:
            ddi = ddi_available(self.udid)
            if ddi is False:
                die(f"设备 {self.udid} 的 " + DDI_HELP)
            if ddi is None:
                log(f"{WARN} 无法确认 DDI 状态，继续尝试")

        url = self.args.server
        if not server_running(url):
            if self.args.no_autostart:
                die(f"appium server 未运行（{url}），去掉 --no-autostart 或手动 appium server")
            url = start_server(url)

        from appium import webdriver

        opts = self.build_options()
        log(f"{DOT} 建立会话 udid={self.udid[:12]}… caps={opts.to_capabilities()}")
        t0 = time.time()
        try:
            self.driver = webdriver.Remote(url, options=opts)
        except Exception as e:
            msg = str(e).split("Stacktrace")[0].strip()
            hint = ""
            low = msg.lower()
            if (
                "developer disk image" in low
                or "code 70" in low
                or "httpunauthorized" in low
            ):
                extra = ddi_mount_error(self.udid)
                hint = "\n\n" + DDI_HELP
                if extra:
                    hint = f"\n\n挂载失败：{extra}" + hint
            elif "xcodebuild failed" in low:
                hint = f"\n\n看 appium 日志定位编译错误：{APPIUM_LOG}（或加 --verbose）"
            die(f"会话建立失败：{msg}{hint}")
        log(f"{OK} 会话就绪（{time.time()-t0:.1f}s）\n")
        return self.driver

    def __exit__(self, *exc):
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass
        return False


def resolve_udid(args: argparse.Namespace) -> str:
    if getattr(args, "simulator", False) and not getattr(args, "udid", None):
        booted = list_simulators(booted_only=True)
        if booted:
            return booted[0]["udid"]
        sims = list_simulators()
        if not sims:
            die("没有可用的 iOS 模拟器。先在 Xcode 里装 iOS Simulator runtime。")
        pick = next((s for s in sims if s["name"] == "iPhone 17"), sims[0])
        log(f"{DOT} --simulator 选用 {pick['name']} / iOS {pick['os']}  {pick['udid']}")
        boot_simulator(pick["udid"])
        return pick["udid"]
    if getattr(args, "udid", None):
        if is_simulator_udid(args.udid):
            boot_simulator(args.udid)
        return args.udid
    env = os.environ.get("IOS_UDID")
    if env:
        return env
    devices = list_devices(connected_only=True)
    if not devices:
        die("没有已连接的 iOS 设备。先跑 doctor 看环境，或用 --udid 指定。")
    wired = [d for d in devices if d["wired"]]
    pool = wired or devices
    if len(pool) > 1:
        log(f"{WARN} 发现 {len(pool)} 台已连接设备，默认用第一台（--udid 可指定）：")
        for d in pool:
            log(f"    {d['udid']}  {d['name']} / iOS {d['os']}  ({d['transport']})")
    if not wired:
        log(f"{WARN} 没有有线连接的设备，改用 {pool[0]['transport']}；Appium 真机通常需要 USB")
    return pool[0]["udid"]


def script(driver, name: str, args: Optional[dict] = None):
    """执行官方 `mobile:` 扩展命令。"""
    return driver.execute_script(name, args or {})


# ──────────────────────── 会话类子命令 ──────────────────────── #
def cmd_info(args):
    with IOSSession(args) as d:
        size = d.get_window_size()
        log(f"window_size : {size['width']} x {size['height']}")
        try:
            log(f"screen_info : {script(d, 'mobile: deviceScreenInfo')}")
        except Exception as e:
            log(f"screen_info : (不可用) {e}")
        try:
            log(f"viewport    : {script(d, 'mobile: viewportRect')}")
        except Exception as e:
            log(f"viewport    : (不可用) {e}")
        try:
            log(f"locked      : {d.is_locked()}")
        except Exception as e:
            log(f"locked      : (不可用) {e}")
        try:
            log(f"active app  : {script(d, 'mobile: activeAppInfo')}")
        except Exception:
            pass
    return 0


def cmd_apps(args):
    """列出设备上的应用（官方 mobile: listApps，部分 iOS 版本可能不支持）。"""
    with IOSSession(args) as d:
        try:
            apps = script(d, "mobile: listApps", {"applicationType": "User"})
        except Exception as e:
            log(f"{WARN} mobile: listApps 不可用：{str(e).splitlines()[0]}")
            log("    可改用 state <bundleId> 查询单个应用是否安装。")
            return 1
        if isinstance(apps, list):
            log(f"共 {len(apps)} 个用户应用：")
            for a in apps:
                if isinstance(a, dict):
                    log(f"  {a.get('CFBundleIdentifier','?'):50} {a.get('CFBundleName','')}")
                else:
                    log(f"  {a}")
        else:
            log(json.dumps(apps, ensure_ascii=False, indent=2))
    return 0


def cmd_install(args):
    path = os.path.abspath(args.path)
    if not os.path.exists(path):
        die(f"文件不存在：{path}")
    with IOSSession(args) as d:
        log(f"{DOT} 安装 {path}")
        d.install_app(path)
        log(f"{OK} 安装完成")
    return 0


def cmd_uninstall(args):
    with IOSSession(args) as d:
        if not d.is_app_installed(args.bundle_id):
            log(f"{WARN} {args.bundle_id} 未安装")
            return 0
        d.remove_app(args.bundle_id)
        log(f"{OK} 已卸载 {args.bundle_id}")
    return 0


_STATE = {0: "未安装", 1: "未运行", 2: "后台挂起", 3: "后台运行", 4: "前台"}


def cmd_state(args):
    with IOSSession(args) as d:
        st = int(d.query_app_state(args.bundle_id))
        log(f"{args.bundle_id} → {st} ({_STATE.get(st, '未知')})")
        log(f"is_app_installed → {d.is_app_installed(args.bundle_id)}")
    return 0


def cmd_launch(args):
    with IOSSession(args) as d:
        script(d, "mobile: launchApp", {"bundleId": args.bundle_id})
        log(f"{OK} 已拉起 {args.bundle_id}")
    return 0


def cmd_terminate(args):
    with IOSSession(args) as d:
        script(d, "mobile: terminateApp", {"bundleId": args.bundle_id})
        log(f"{OK} 已结束 {args.bundle_id}")
    return 0


def cmd_shot(args):
    with IOSSession(args) as d:
        out = os.path.abspath(args.out)
        d.get_screenshot_as_file(out)
        log(f"{OK} 截图已保存 {out}  ({os.path.getsize(out)} bytes)")
    return 0


def cmd_source(args):
    with IOSSession(args) as d:
        xml = d.page_source
        if args.out:
            out = os.path.abspath(args.out)
            with open(out, "w", encoding="utf-8") as f:
                f.write(xml)
            log(f"{OK} 页面结构已保存 {out}  ({len(xml)} 字符)")
        else:
            log(xml[:4000] + ("\n… (截断)" if len(xml) > 4000 else ""))
    return 0


def cmd_tap(args):
    with IOSSession(args) as d:
        script(d, "mobile: tap", {"x": args.x, "y": args.y})
        log(f"{OK} 点击 ({args.x}, {args.y})")
    return 0


def cmd_swipe(args):
    with IOSSession(args) as d:
        w, h = (lambda s: (s["width"], s["height"]))(d.get_window_size())
        margin = (1.0 - args.scale) / 2.0
        pts = {
            "up":    (0.5, 1 - margin, 0.5, margin),
            "down":  (0.5, margin, 0.5, 1 - margin),
            "left":  (1 - margin, 0.5, margin, 0.5),
            "right": (margin, 0.5, 1 - margin, 0.5),
        }[args.direction]
        script(d, "mobile: dragFromToForDuration", {
            # duration 是起点按住时长，官方约束区间 [0.5, 60] 秒
            "duration": 0.5,
            "fromX": int(w * pts[0]), "fromY": int(h * pts[1]),
            "toX": int(w * pts[2]), "toY": int(h * pts[3]),
        })
        log(f"{OK} 滑动 {args.direction}")
    return 0


def cmd_press(args):
    with IOSSession(args) as d:
        script(d, "mobile: pressButton", {"name": args.name})
        log(f"{OK} 按下 {args.name}")
    return 0


def cmd_type(args):
    with IOSSession(args) as d:
        try:
            script(d, "mobile: keys", {"keys": list(args.text)})
        except Exception:
            d.switch_to.active_element.send_keys(args.text)
        log(f"{OK} 已输入 {args.text!r}")
    return 0


def cmd_unlock(args):
    with IOSSession(args) as d:
        if not d.is_locked():
            log(f"{DOT} 未锁屏")
            return 0
        d.unlock()
        log(f"{OK if not d.is_locked() else WARN} unlock 后 locked={d.is_locked()}")
    return 0


def cmd_click(args):
    from appium.webdriver.common.appiumby import AppiumBy

    if args.predicate:
        by, value = AppiumBy.IOS_PREDICATE, args.predicate
    elif args.class_chain:
        by, value = AppiumBy.IOS_CLASS_CHAIN, args.class_chain
    elif args.xpath:
        by, value = AppiumBy.XPATH, args.xpath
    elif args.text:
        by, value = AppiumBy.IOS_PREDICATE, (
            f"label == '{args.text}' OR name == '{args.text}' "
            f"OR value == '{args.text}'"
        )
    else:
        die("需要 --text / --predicate / --class-chain / --xpath 之一")

    with IOSSession(args) as d:
        d.implicitly_wait(args.timeout)
        try:
            el = d.find_element(by=by, value=value)
        except Exception:
            die(f"找不到元素：{by} = {value}")
        el.click()
        log(f"{OK} 已点击 {by} = {value}")
    return 0


def cmd_smoke(args):
    """一次跑通全部只读动作，用于验收后端是否真的可用。"""
    steps, failed = [], 0
    with IOSSession(args) as d:
        def step(name, fn):
            nonlocal failed
            try:
                out = fn()
                steps.append((OK, name, str(out)[:70]))
            except Exception as e:
                failed += 1
                steps.append((BAD, name, str(e).splitlines()[0][:70]))

        step("window_size", lambda: d.get_window_size())
        step("is_locked", lambda: d.is_locked())
        step("screenshot", lambda: f"{len(d.get_screenshot_as_base64())} b64 chars")
        step("page_source", lambda: f"{len(d.page_source)} 字符")
        step("deviceScreenInfo", lambda: script(d, "mobile: deviceScreenInfo"))
        step("viewportRect", lambda: script(d, "mobile: viewportRect"))
        step("pressButton home", lambda: script(d, "mobile: pressButton", {"name": "home"}))
        step("swipe up", lambda: script(d, "mobile: dragFromToForDuration",
                                       {"duration": 0.5, "fromX": 200, "fromY": 600,
                                        "toX": 200, "toY": 200}))
        step("queryAppState(Preferences)",
             lambda: d.query_app_state("com.apple.Preferences"))
        step("launch Preferences",
             lambda: script(d, "mobile: launchApp", {"bundleId": "com.apple.Preferences"}))
        step("terminate Preferences",
             lambda: script(d, "mobile: terminateApp", {"bundleId": "com.apple.Preferences"}))

    log("\n═══ smoke 结果 ═══")
    for mark, name, detail in steps:
        log(f"{mark} {name:28} {detail}")
    log(f"\n{len(steps)-failed}/{len(steps)} 通过")
    return 1 if failed else 0


def cmd_devices(args: argparse.Namespace) -> int:
    devices = list_devices()
    sims = list_simulators()
    if not devices and not sims:
        log("(没有真机也没有模拟器)")
        return 1
    for d in devices:
        if not d["connected"]:
            log(f"{DOT} {d['udid']}  未连接")
            log(f"    {d['name']} / {d['model']} / iOS {d['os']}  ({d['tunnel']})")
            continue
        ddi = ddi_available(d["udid"])
        ddi_txt = {True: "DDI 就绪", False: "DDI 未挂载", None: "DDI 未知"}[ddi]
        log(f"{OK if ddi else (WARN if ddi is None else BAD)} {d['udid']}")
        log(f"    {d['name']} / {d['model']} / iOS {d['os']}")
        log(f"    transport={d['transport']}  tunnel={d['tunnel']}  {ddi_txt}")
    if sims:
        log("\n─── 模拟器（不需要 DDI）───")
        for s in sims:
            mark = OK if s["connected"] else DOT
            log(f"{mark} {s['udid']}  {s['name']} / iOS {s['os']}  ({s['tunnel']})")
    return 0


# ──────────────────────────── CLI ──────────────────────────── #
def _common_args(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument("--udid", help="设备 UDID（默认第一台已连接真机）")
    p.add_argument("--simulator", action="store_true", help="用 iOS 模拟器（不需要 DDI）")
    p.add_argument("--bundle", help="被测应用 bundleId，会话建立时拉起")
    p.add_argument("--server", default=os.environ.get("IOS_APPIUM_URL", DEFAULT_SERVER))
    p.add_argument("--no-autostart", action="store_true", help="不自动拉起 appium server")
    p.add_argument("--skip-ddi", action="store_true", help="跳过 DDI 前置检查")
    p.add_argument("--verbose", action="store_true", help="打印 xcodebuild 日志")
    return p


def build_parser() -> argparse.ArgumentParser:
    # 公共参数放进 parent，这样 `cmd --udid X` 和 `--udid X cmd` 两种写法都能用
    parent = _common_args(argparse.ArgumentParser(add_help=False))

    p = argparse.ArgumentParser(
        prog="appium_ios.py",
        description="独立的 iOS × Appium 脚本（纯官方能力，可直接运行）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="真机：doctor →（DDI 就绪后）smoke。模拟器：smoke --simulator（不需要 DDI）。",
        parents=[parent],
    )

    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, helptext: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=helptext, parents=[parent])

    add("doctor", "环境体检（无需会话）").set_defaults(func=cmd_doctor)
    add("prepare", "打开 Xcode 设备窗口并等待 DDI 挂载").set_defaults(func=cmd_prepare)
    add("setup-wda", "让 Appium 把 WDA 编译安装到设备").set_defaults(func=cmd_setup_wda)
    add("devices", "列出设备").set_defaults(func=cmd_devices)
    add("info", "设备与屏幕信息").set_defaults(func=cmd_info)
    add("apps", "列出已装应用").set_defaults(func=cmd_apps)
    add("smoke", "跑通全部只读动作做验收").set_defaults(func=cmd_smoke)
    add("unlock", "解锁屏幕").set_defaults(func=cmd_unlock)

    sp = add("install", "安装 .ipa / .app")
    sp.add_argument("path")
    sp.set_defaults(func=cmd_install)

    for name, fn, helptext in (
        ("uninstall", cmd_uninstall, "卸载应用"),
        ("state", cmd_state, "查询应用状态"),
        ("launch", cmd_launch, "拉起应用"),
        ("terminate", cmd_terminate, "结束应用"),
    ):
        sp = add(name, helptext)
        sp.add_argument("bundle_id")
        sp.set_defaults(func=fn)

    sp = add("shot", "截图")
    sp.add_argument("out", nargs="?", default="screenshot.png")
    sp.set_defaults(func=cmd_shot)

    sp = add("source", "导出页面结构 XML")
    sp.add_argument("out", nargs="?")
    sp.set_defaults(func=cmd_source)

    sp = add("tap", "按坐标点击")
    sp.add_argument("x", type=int)
    sp.add_argument("y", type=int)
    sp.set_defaults(func=cmd_tap)

    sp = add("swipe", "方向滑动")
    sp.add_argument("direction", choices=["up", "down", "left", "right"])
    sp.add_argument("--scale", type=float, default=0.8)
    sp.set_defaults(func=cmd_swipe)

    sp = add("press", "硬件键")
    sp.add_argument("name", choices=["home", "volumeup", "volumedown"])
    sp.set_defaults(func=cmd_press)

    sp = add("type", "向当前焦点输入文本")
    sp.add_argument("text")
    sp.set_defaults(func=cmd_type)

    sp = add("click", "按定位器点击元素")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--text", help="按 label/name/value 匹配")
    g.add_argument("--predicate", help="NSPredicate")
    g.add_argument("--class-chain", help="iOS class chain")
    g.add_argument("--xpath")
    sp.add_argument("--timeout", type=float, default=10.0)
    sp.set_defaults(func=cmd_click)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        log("\n已中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
