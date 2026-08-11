# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
RemoteEngine：通过 WebSocket 远程驱动 ClawNode 直连设备（SN 以 "claw-" 开头）。

与 MAdbEngine / IOSEngine 同基类（MobileEngine），但不走 adb：所有动作翻译成
ClawNode 方言 {trace_id, action_type, payload}，经 DeviceManager 的 WebSocket
下发，再用 trace_id + threading.Event 等待设备回传（SCREENSHOT_RESULT /
ACTION_RESULT）。

线程模型：被同步的 service 层（已由 rWebsocket 用 asyncio.to_thread offload 到
worker 线程）调用，故可安全地 event.wait() 阻塞 worker 线程；WebSocket 的发送
通过 run_coroutine_threadsafe 提交回主 loop（DeviceManager().loop）。
"""

import base64
import re
import threading
import uuid
import time
from io import BytesIO
from typing import Any, Dict, Optional, Tuple

from PIL import Image

from script.log import SLog
from driver.tentacle.engine.mobile.mobile_engine import MobileEngine

TAG = "RemoteEngine"

DEFAULT_TIMEOUT = 10.0      # 等待设备回传的超时（screenshot 往返较慢）
SEND_TIMEOUT = 5.0          # 仅等待"发送到 WS"完成的超时
DEFAULT_SIZE = (1080, 1920)  # screen_size 兜底


class RemoteEngine(MobileEngine):

    # 类级 sn -> engine 注册表：SingletonMeta 下全局仅一个 RemoteEngine 实例，
    # 但其 _serial 随设备切换；回传唤醒按 sn 查这里，防止串台。
    _by_sn: Dict[str, "RemoteEngine"] = {}

    def init_driver(self, test_subject=None):
        serial = test_subject or getattr(self, "_test_subject", None) or getattr(
            self, "_serial", None
        )
        self._serial = serial
        self._test_subject = serial
        # 非 None 即可，让基类 start() 不再反复 init；值仅作标记
        self.driver = "ClawNode_Remote_Active"
        self._pending: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._screen_size_cache: Optional[Tuple[int, int]] = None
        self._last_started_package: Optional[str] = None
        self._last_started_at: float = 0.0
        if serial:
            RemoteEngine._by_sn[serial] = self
        SLog.i(TAG, f"RemoteEngine bound to {serial}")

    # ---------------- 回传唤醒（由 wClawNode.handle_clawnode_result 调用）----------------

    def _resolve(self, trace_id: str, data: dict):
        """幂等、非阻塞：填充结果并唤醒等待者。在主 loop 线程被调用。"""
        if not trace_id:
            return
        with self._lock:
            slot = self._pending.get(trace_id)
            if slot:
                slot["result"] = data
                slot["event"].set()

    # ---------------- 请求往返核心 ----------------

    def _request(self, action_type: str, payload: dict, timeout: float = DEFAULT_TIMEOUT) -> Optional[dict]:
        """
        下发一条 ClawNode 指令并同步等待回传。返回拍平后的 data dict 或 None（超时/失败）。
        必须在 worker 线程调用（不能在主 event loop 线程，否则阻塞收包导致死锁）。
        """
        import asyncio
        from server.websocket.device_manager import DeviceManager

        dm = DeviceManager()
        loop = getattr(dm, "loop", None)
        ws = dm.active_connections.get(self._serial)
        if loop is None or ws is None:
            SLog.w(TAG, f"no loop/ws for {self._serial} (loop={loop is not None}, ws={ws is not None})")
            return None

        trace_id = f"eng-{uuid.uuid4().hex[:12]}"
        # 与 wClawNode.translate_* / send_command 对齐的标准帧；ClawNode 端仍兼容 action_type 旧格式
        frame = {
            "type": "command",
            "command": action_type,
            "params": payload or {},
            "trace_id": trace_id,
        }

        event = threading.Event()
        with self._lock:
            self._pending[trace_id] = {"event": event, "result": None}

        try:
            fut = asyncio.run_coroutine_threadsafe(dm._safe_send(ws, frame), loop)
            fut.result(timeout=SEND_TIMEOUT)  # 仅等发送完成
        except Exception as e:
            SLog.e(TAG, f"send failed action={action_type}: {e}")
            with self._lock:
                self._pending.pop(trace_id, None)
            return None

        if not event.wait(timeout):
            SLog.w(TAG, f"timeout waiting {action_type} trace={trace_id}")
            with self._lock:
                self._pending.pop(trace_id, None)
            return None

        with self._lock:
            slot = self._pending.pop(trace_id, None)
        return slot["result"] if slot else None

    # ---------------- 眼：截图 ----------------

    def screenshot(self, path=None):
        data = self._request("GET_SCREENSHOT", {"quality": 80})
        if not data:
            SLog.e(TAG, "screenshot failed (no data)")
            return None
        b64 = data.get("base64_image") or data.get("base64")
        if not b64:
            msg = str(data.get("message") or "").strip()
            rtype = str(data.get("type") or "")
            status = str(data.get("status") or "")
            SLog.e(
                TAG,
                f"screenshot missing base64 type={rtype} status={status} "
                f"msg={msg[:240] or list(data.keys())}",
            )
            return None
        try:
            img = Image.open(BytesIO(base64.b64decode(b64)))
            # 缓存分辨率，供 screen_size 复用
            self._screen_size_cache = img.size
            if path:
                img.save(path)
                return path
            return img
        except Exception as e:
            SLog.e(TAG, f"decode screenshot failed: {e}")
            return None

    def screen_size(self) -> Tuple[int, int]:
        if self._screen_size_cache:
            return self._screen_size_cache
        # 优先用 register 时上报的 resolution（如 "1080x2400"）
        try:
            from server.services.device_service import DeviceService
            dev = DeviceService.get_by_sn(self._serial)
            res = getattr(dev, "resolution", None) if dev else None
            if res and "x" in str(res):
                w, h = str(res).lower().split("x", 1)
                size = (int(w), int(h))
                self._screen_size_cache = size
                return size
        except Exception as e:
            SLog.d(TAG, f"resolution lookup failed: {e}")
        # 再退而求其次：截一帧拿尺寸
        img = self.screenshot()
        if img is not None and self._screen_size_cache:
            return self._screen_size_cache
        SLog.w(TAG, f"screen_size fallback to {DEFAULT_SIZE}")
        return DEFAULT_SIZE

    # ---------------- 手：手势 ----------------

    def click(self, element, position=None, label: str = "", **kwargs) -> bool:
        # 纯像素节点：只认坐标，忽略 label / UI 树相关参数
        target = position if position else element
        if not target or not isinstance(target, (tuple, list)) or len(target) < 2:
            SLog.w(TAG, f"click ignored: RemoteEngine needs (x,y), got {target!r}")
            return False
        x, y = int(target[0]), int(target[1])
        data = self._request("TAP", {"x": x, "y": y, "duration_ms": 80})
        return bool(data and (data.get("status") == "success"))

    def swipe_norm(self, x1: float, y1: float, x2: float, y2: float, duration: float = 0.5):
        w, h = self.screen_size()
        px = lambda v, base: int(v * base) if 0 <= v <= 1 else int(v)
        payload = {
            "x": px(x1, w), "y": px(y1, h),
            "x2": px(x2, w), "y2": px(y2, h),
            "duration_ms": int(duration * 1000),
        }
        self._request("SWIPE", payload)
        return None

    def press_key(self, event: str):
        # 扩协议：KEY_EVENT，由 ClawNode 用 performGlobalAction 落地（back/home）
        self._request("KEY_EVENT", {"keyevent": str(event)})
        return None

    def screen_on(self):
        # 远程节点没有 adb 兜底，直接让 ClawNode 端拉起唤醒页。
        self._request("WAKE_UP", {})
        return None

    @staticmethod
    def _is_mostly_black_image(img, *, threshold: float = 18.0) -> bool:
        """截图均值亮度极低时视为黑屏/息屏。"""
        if img is None:
            return True
        try:
            gray = img.convert("L")
            hist = gray.histogram()
            pixels = sum(hist) or 1
            mean = sum(i * c for i, c in enumerate(hist)) / pixels
            return mean < threshold
        except Exception:
            return False

    @staticmethod
    def _is_shell_foreground(pkg: str) -> bool:
        """锁屏/桌面壳层包名，不能视为被测应用已在前台。"""
        p = (pkg or "").strip().lower()
        if not p:
            return True
        if p in {"com.android.systemui", "android", "com.miui.home", "com.mi.android.launcher"}:
            return True
        if "launcher" in p or p.endswith(".launcher") or p.endswith(".home"):
            return True
        return False

    @classmethod
    def _foreground_matches_target(cls, current: str, target: str) -> bool:
        current = (current or "").strip()
        target = (target or "").strip()
        if not current or not target or cls._is_shell_foreground(current):
            return False
        return current == target or target in current or current in target

    def ensure_screen_ready(self, node_sn=None) -> bool:
        """远程节点：先 WAKE_UP 点亮，再截图判断是否仍为黑屏/息屏。"""
        for attempt in range(5):
            self.screen_on()
            time.sleep(0.55)
            shot = self.screenshot()
            if shot is not None and not self._is_mostly_black_image(shot):
                if attempt:
                    SLog.i(TAG, f"screen ready after wake attempt={attempt} sn={self._serial}")
                return True
            SLog.i(
                TAG,
                f"screen not ready sn={self._serial} attempt={attempt} "
                f"blank={self._is_mostly_black_image(shot)}",
            )
        SLog.w(TAG, f"screen still not ready sn={self._serial}")
        return False

    def start_app(self, package_name=None, *, activity: str = ""):
        pkg = (package_name or "").strip()
        if not pkg:
            return False
        if not self.ensure_screen_ready():
            SLog.w(TAG, f"open app: screen not ready sn={self._serial} pkg={pkg}, still sending OPEN_APP")
        payload: dict = {"package": pkg}
        act = (activity or "").strip()
        if act:
            payload["activity"] = act
        data = self._request("OPEN_APP", payload, timeout=30.0)
        ok = bool(data and str(data.get("status") or "").lower() == "success")
        if not ok:
            msg = ""
            if data:
                msg = (data.get("message") or data.get("stderr") or "").strip()
            SLog.w(TAG, f"OPEN_APP failed pkg={pkg} msg={msg or 'unknown'}")
            return False

        # ClawNode OPEN_APP 成功时 message 已是设备端读到的前台包名（或目标包名）
        open_msg = ""
        if data:
            open_msg = (data.get("message") or data.get("stdout") or "").strip()
        if open_msg and self._foreground_matches_target(open_msg, pkg):
            self._last_started_package = open_msg
            self._last_started_at = time.time()
            SLog.i(TAG, f"OPEN_APP ok pkg={pkg} foreground={open_msg} (device confirmed)")
            return True

        deadline = time.time() + 20.0
        while time.time() < deadline:
            current = self.current_package()
            if self._foreground_matches_target(current, pkg):
                self._last_started_package = current
                self._last_started_at = time.time()
                SLog.i(TAG, f"OPEN_APP ok pkg={pkg} foreground={current}")
                return True
            time.sleep(0.45)
        fg = self.current_package()
        SLog.w(
            TAG,
            f"OPEN_APP sent but foreground not confirmed pkg={pkg} current={fg or 'unknown'}, trying VLM",
        )
        if self._confirm_launch_visually(pkg):
            self._last_started_package = pkg
            self._last_started_at = time.time()
            return True
        return False

    def _confirm_launch_visually(self, pkg: str) -> bool:
        """包名轮询失败时，用 VLM 判断当前屏是否为目标应用（非桌面/设置）。"""
        time.sleep(0.6)
        shot = self.screenshot()
        if shot is None or self._is_mostly_black_image(shot):
            return False
        try:
            buf = BytesIO()
            shot.convert("RGB").save(buf, format="JPEG", quality=80)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            SLog.w(TAG, f"VLM launch confirm encode failed pkg={pkg}: {e}")
            return False
        try:
            from server.services.ai.regression.planner import assert_visual

            result = assert_visual(
                expectation=(
                    f"当前手机屏幕显示的是包名为 {pkg} 的应用主界面或启动页，"
                    "不是手机桌面、不是系统设置页、不是应用列表。"
                ),
                image_base64=b64,
                timeout_sec=45,
            )
            if result.passed:
                SLog.i(
                    TAG,
                    f"VLM confirmed app launched pkg={pkg} confidence={result.confidence}",
                )
                return True
            SLog.w(
                TAG,
                f"VLM launch confirm failed pkg={pkg}: {(result.ai_reasoning or '')[:200]}",
            )
        except Exception as e:
            SLog.w(TAG, f"VLM launch confirm exception pkg={pkg}: {e}")
        return False

    def stop_app(self, package_name=None):
        """Remote：设置 → 应用信息 → 强制停止（拟人化）。"""
        pkg = (package_name or "").strip()
        if not pkg:
            return False
        from server.services.regression.persona_remote_lifecycle import force_stop_app_via_persona

        ok, msg, _detail = force_stop_app_via_persona(self._serial, pkg)
        if not ok:
            SLog.w(TAG, f"persona force_stop failed pkg={pkg} sn={self._serial}: {msg}")
        return ok

    def clear_app_cache(self, package_name=None, *, app_name: str = ""):
        """Remote：EXEC_SCRIPT 打开应用详情页 → persona 按当前屏分步清存储。"""
        pkg = (package_name or "").strip()
        if not pkg:
            return False
        from server.services.regression.persona_remote_lifecycle import (
            clear_app_storage_via_persona,
            open_app_details_via_exec_script,
        )

        ok_boot, boot_msg = open_app_details_via_exec_script(self._serial, pkg)
        if not ok_boot:
            SLog.w(
                TAG,
                f"open_app_details failed pkg={pkg} sn={self._serial}: {boot_msg}",
            )
            return False

        ok, msg, _detail = clear_app_storage_via_persona(
            self._serial, pkg, app_name=app_name or "",
        )
        if not ok:
            SLog.w(TAG, f"persona clear_storage failed pkg={pkg} sn={self._serial}: {msg}")
        return ok

    def clear_app(self, package_name=None, *, app_name: str = ""):
        """别名 → clear_app_cache。"""
        return self.clear_app_cache(package_name, app_name=app_name)

    # ---------------- 给不了的能力：优雅降级返回空 ----------------

    def current_package(self) -> str:
        data = self._request("GET_FOREGROUND_APP", {})
        if data:
            pkg = (data.get("message") or data.get("stdout") or "").strip()
            if pkg:
                if not self._is_shell_foreground(pkg):
                    self._last_started_package = pkg
                    self._last_started_at = time.time()
                return pkg
        return ""

    def dump_hierarchy_xml(self) -> str:
        SLog.d(TAG, "dump_hierarchy_xml unsupported on ClawNode; returning empty")
        return ""

    def shell(self, cmd: str) -> str:
        """受限 shell：供飞书前置 check_sim / clear_cache / pm path 等使用。"""
        command = (cmd or "").strip()
        if not command:
            return ""

        pm_clear = re.match(r"pm clear (\S+)", command)
        if pm_clear:
            data = self._request("RUN_SHELL", {"command": command}, timeout=20.0)
            if not data:
                return ""
            stdout = (data.get("stdout") or "").strip()
            if stdout:
                return stdout
            return (data.get("message") or "").strip()

        data = self._request("RUN_SHELL", {"command": command}, timeout=20.0)
        if not data:
            return ""
        stdout = (data.get("stdout") or "").strip()
        if stdout:
            return stdout
        return (data.get("message") or "").strip()

    def exec_script(
        self,
        script: str = "",
        *,
        script_id: str = "",
        language: str = "dsl",
        timeout_ms: int = 60_000,
        script_vars: dict | None = None,
    ) -> Tuple[bool, str, str]:
        """
        在设备上执行 EXEC_SCRIPT（ClawNode >= 1.8.0）。
        返回 (ok, stdout, stderr)。
        """
        from server.services.shared.clawnode_script import (
            build_exec_script_command_params,
            parse_exec_script_response,
        )

        try:
            payload = build_exec_script_command_params(
                script=script,
                script_id=script_id,
                language=language,
                timeout_ms=timeout_ms,
                script_vars=script_vars,
            )
        except ValueError as e:
            return False, "", str(e)

        wait_sec = max(30.0, min(int(payload.get("timeout_ms") or timeout_ms) / 1000.0 + 15.0, 305.0))
        data = self._request("EXEC_SCRIPT", payload, timeout=wait_sec)
        ok, stdout, stderr = parse_exec_script_response(data)
        if ok:
            try:
                from server.services.regression.screen import invalidate_remote_capture_cache
                invalidate_remote_capture_cache(self._serial)
            except Exception:
                pass
        return ok, stdout, stderr
