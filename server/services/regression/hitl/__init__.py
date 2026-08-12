# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""HITL（Human-In-The-Loop）子模块：让人工实时介入回归执行。

外部使用：
    from server.services.regression.hitl import (
        HitlRequest, HitlReply, HitlSessionManager,
        get_session_manager, set_transport,
    )

设计要点
--------
- HitlSessionManager 是进程级单例，保存所有"已发出、未答复"的请求；
  每个请求带一个 threading.Event，HitlExecutor 同步阻塞直到 event.set()。
- 真正把请求送到桌面端走 HitlTransport（默认 WebSocket 广播给 observers）；
  测试时可注入 NoopHitlTransport / DummyHitlTransport。
- 路由层（rHitl.py）调用 session_manager.submit_reply(...) 投递人工答案。
"""
from server.services.regression.hitl.schemas import (
    HitlRequest,
    HitlReply,
    HitlPending,
)
from server.services.regression.hitl.session import (
    HitlSessionManager,
    get_session_manager,
)
from server.services.regression.hitl.transport import (
    HitlTransport,
    WebSocketHitlTransport,
    NoopHitlTransport,
    RecordingHitlTransport,
    set_transport,
    get_transport,
)

__all__ = [
    "HitlRequest",
    "HitlReply",
    "HitlPending",
    "HitlSessionManager",
    "get_session_manager",
    "HitlTransport",
    "WebSocketHitlTransport",
    "NoopHitlTransport",
    "RecordingHitlTransport",
    "set_transport",
    "get_transport",
]
