# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""HitlTransport：把 HitlRequest 推送到桌面端 + 通知撤销。

设计
====
Transport 是可替换的。默认 WebSocketHitlTransport 借用 DeviceManager.broadcast_to_observers
广播给所有桌面端。测试时可注入 NoopHitlTransport。

WS 帧形状（type）
----------------
- "hitl_request"  → 新增一条待人工处理
- "hitl_revoke"   → server 主动撤销（超时、case 中止）
- "hitl_resolved" → 已被回复（用于其它客户端同步关闭弹框）

桌面端可通过 HTTP POST /hitl/reply 投递答案（见 rHitl.py）。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Optional, Protocol

from script.log import SLog

from server.services.regression.hitl.schemas import HitlRequest

TAG = "HitlTransport"


class HitlTransport(Protocol):
    """传输接口；实现需保证『线程安全』（HitlExecutor 在子线程里调用）。"""

    def push_request(self, request: HitlRequest) -> bool: ...
    def push_revoke(self, request_id: str, reason: str = "") -> bool: ...
    def push_resolved(self, request_id: str, summary: dict[str, Any]) -> bool: ...


# ---------- 默认实现：WebSocket 广播给桌面 observers ----------


class WebSocketHitlTransport:
    """默认实现。借用 server.websocket.device_manager.DeviceManager。

    使用 asyncio.run_coroutine_threadsafe 把广播投递到 app 主事件循环
    （在 main.py 启动时通过 set_app_loop() 注入）。
    """

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.Lock()

    def set_app_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._loop = loop

    def _get_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        with self._lock:
            return self._loop

    def _broadcast(self, payload: dict[str, Any]) -> bool:
        loop = self._get_loop()
        if loop is None or loop.is_closed():
            SLog.w(TAG, "no app loop set; skip broadcast")
            return False
        try:
            from server.websocket.device_manager import DeviceManager
        except Exception as exc:  # pragma: no cover
            SLog.e(TAG, f"DeviceManager import failed: {exc}")
            return False
        manager = DeviceManager()
        try:
            fut = asyncio.run_coroutine_threadsafe(
                manager.broadcast_to_observers(payload), loop
            )
            fut.result(timeout=5.0)
            return True
        except Exception as exc:
            SLog.e(TAG, f"broadcast failed: {exc}")
            return False

    def push_request(self, request: HitlRequest) -> bool:
        return self._broadcast({"type": "hitl_request", "data": request.to_payload()})

    def push_revoke(self, request_id: str, reason: str = "") -> bool:
        return self._broadcast({
            "type": "hitl_revoke",
            "data": {"request_id": request_id, "reason": reason},
        })

    def push_resolved(self, request_id: str, summary: dict[str, Any]) -> bool:
        return self._broadcast({
            "type": "hitl_resolved",
            "data": {"request_id": request_id, **summary},
        })


# ---------- 测试实现 ----------


class NoopHitlTransport:
    """什么都不做；用在没有 WS 的场景（脚本/测试）。永远返回 True。"""

    def push_request(self, request: HitlRequest) -> bool:
        return True

    def push_revoke(self, request_id: str, reason: str = "") -> bool:
        return True

    def push_resolved(self, request_id: str, summary: dict[str, Any]) -> bool:
        return True


class RecordingHitlTransport:
    """测试用：把所有推送收下来，断言时回看。"""

    def __init__(self) -> None:
        self.requests: list[HitlRequest] = []
        self.revokes: list[tuple[str, str]] = []
        self.resolves: list[tuple[str, dict[str, Any]]] = []

    def push_request(self, request: HitlRequest) -> bool:
        self.requests.append(request)
        return True

    def push_revoke(self, request_id: str, reason: str = "") -> bool:
        self.revokes.append((request_id, reason))
        return True

    def push_resolved(self, request_id: str, summary: dict[str, Any]) -> bool:
        self.resolves.append((request_id, summary))
        return True


# ---------- 默认实例 ----------

_default_transport: Optional[HitlTransport] = None
_transport_lock = threading.Lock()


def set_transport(transport: HitlTransport) -> None:
    """主进程启动时 / 测试时设置全局 transport。"""
    global _default_transport
    with _transport_lock:
        _default_transport = transport
    SLog.i(TAG, f"hitl transport set to {type(transport).__name__}")


def get_transport() -> HitlTransport:
    """缺省值：WebSocketHitlTransport（main.py 应主动 set_app_loop）。"""
    global _default_transport
    with _transport_lock:
        if _default_transport is None:
            _default_transport = WebSocketHitlTransport()
        return _default_transport
