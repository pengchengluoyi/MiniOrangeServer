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
import threading
import uuid
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
        frame = {"trace_id": trace_id, "action_type": action_type, "payload": payload}

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
            SLog.e(TAG, f"screenshot missing base64_image; keys={list(data.keys())}")
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
        # ClawNode 通过无障碍 dispatchGesture 操作，无需 WakeUpActivity（会抢前台）。
        return None

    def ensure_screen_ready(self, node_sn=None) -> bool:
        """纯像素节点无 adb 亮屏/解锁；手势不依赖本 App 在前台。"""
        return True

    def start_app(self, package_name=None):
        # 纯像素节点无法 am start；记录告警，返回 None 不中断上层
        SLog.w(TAG, f"start_app({package_name}) unsupported on ClawNode (no UI tree / am)")
        return None

    def stop_app(self, package_name=None):
        # 扩协议：STOP_APP（ClawNode 端可能仅 no-op，视权限而定）
        data = self._request("STOP_APP", {"package": package_name or ""})
        return bool(data and (data.get("status") == "success"))

    # ---------------- 给不了的能力：优雅降级返回空 ----------------

    def current_package(self) -> str:
        # 纯像素 VLA 节点无 UI 树/dumpsys 能力；返回空串，消费方走纯视觉
        return ""

    def dump_hierarchy_xml(self) -> str:
        SLog.d(TAG, "dump_hierarchy_xml unsupported on ClawNode; returning empty")
        return ""
