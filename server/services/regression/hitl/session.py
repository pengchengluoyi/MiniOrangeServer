# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""HitlSessionManager：把 HTTP/WS 的人工答复"桥接"回同步的执行线程。

线程模型
========
- 一次 HITL 请求 = 一个 _Session（含 threading.Event）
- HitlExecutor.execute() 调 submit_request() → 注册 session → wait_for_reply(...) 阻塞
- HTTP 路由（rHitl.py）调 submit_reply(request_id, HitlReply) → set event → 阻塞线程被释放
- 也支持超时主动 revoke（HitlExecutor wait 超时后会调 revoke，避免泄漏）

为什么用 threading 而不是 asyncio？
- Orchestrator 是同步运行的（在线程 / 子进程都可能），用 threading.Event 最稳；
  桥接到 asyncio 反而增加生命周期管理复杂度。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Optional

from script.log import SLog

from server.services.regression.hitl.schemas import HitlPending, HitlReply, HitlRequest

TAG = "HitlSession"


def _run_belongs_to_task(run_id: str, task_id: str) -> bool:
    return run_id == task_id or run_id.startswith(f"{task_id}::")


@dataclass
class _Session:
    request: HitlRequest
    event: threading.Event
    reply: Optional[HitlReply] = None
    revoked: bool = False
    revoke_reason: str = ""


class HitlSessionManager:
    """进程级单例。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}

    # ---------- 创建 / 投递 ----------

    @staticmethod
    def new_request_id() -> str:
        return f"hitl-{uuid.uuid4().hex[:16]}"

    def submit_request(self, request: HitlRequest) -> _Session:
        """注册一次请求；返回 _Session 句柄供 wait_for_reply 使用。"""
        with self._lock:
            if request.request_id in self._sessions:
                raise ValueError(f"duplicate request_id: {request.request_id}")
            session = _Session(request=request, event=threading.Event())
            self._sessions[request.request_id] = session
        SLog.i(
            TAG,
            f"submit hitl {request.request_id} kind={request.kind} cap={request.capability_id} "
            f"timeout={request.timeout_sec}s",
        )
        return session

    # ---------- 等待 ----------

    def wait_for_reply(
        self,
        session: _Session,
        *,
        timeout_sec: Optional[float] = None,
    ) -> Optional[HitlReply]:
        """同步阻塞，直到收到 reply / 被 revoke / 超时。

        返回 HitlReply 表示用户答复；返回 None 表示超时或被 revoke。
        """
        timeout = float(timeout_sec) if timeout_sec is not None else float(session.request.timeout_sec)
        got = session.event.wait(timeout=timeout)
        with self._lock:
            # 不论结果如何，session 已结束，移出 pending
            self._sessions.pop(session.request.request_id, None)
        if not got:
            SLog.w(TAG, f"hitl {session.request.request_id} timeout after {timeout}s")
            return None
        if session.revoked:
            SLog.w(TAG, f"hitl {session.request.request_id} revoked: {session.revoke_reason}")
            return None
        return session.reply

    # ---------- 答复 ----------

    def submit_reply(self, reply: HitlReply) -> bool:
        """路由调用：投递人工答复。返回 True=成功；False=未找到/已结束。"""
        with self._lock:
            session = self._sessions.get(reply.request_id)
            if session is None:
                SLog.w(TAG, f"submit_reply: no pending session {reply.request_id}")
                return False
            if reply.kind and reply.kind != session.request.kind:
                SLog.w(
                    TAG,
                    f"submit_reply: kind mismatch want={session.request.kind} got={reply.kind} "
                    f"({reply.request_id})",
                )
                # 不强制拒绝，但记一个 extra warning
                reply.extra.setdefault("warning_kind_mismatch", True)
            session.reply = reply
        session.event.set()
        SLog.i(TAG, f"submit_reply ok {reply.request_id} skipped={reply.skipped}")
        return True

    def revoke(self, request_id: str, reason: str = "revoked") -> bool:
        """主动撤销一次请求（例如 executor 超时退出 / case 被中止）。"""
        with self._lock:
            session = self._sessions.get(request_id)
            if session is None:
                return False
            session.revoked = True
            session.revoke_reason = reason
        session.event.set()
        SLog.i(TAG, f"revoke hitl {request_id} reason={reason}")
        return True

    def revoke_all(self, reason: str = "shutdown") -> int:
        with self._lock:
            ids = list(self._sessions.keys())
        count = 0
        for rid in ids:
            if self.revoke(rid, reason=reason):
                count += 1
        return count

    def revoke_for_task(self, task_id: str, reason: str = "task_cancelled") -> list[str]:
        """撤销属于某任务的 HITL（run_id 为 task_id 或 task_id::case_id）。"""
        prefix = str(task_id or "").strip()
        if not prefix:
            return []
        with self._lock:
            ids = [
                s.request.request_id
                for s in self._sessions.values()
                if _run_belongs_to_task(str(s.request.run_id or ""), prefix)
            ]
        revoked: list[str] = []
        for rid in ids:
            if self.revoke(rid, reason=reason):
                revoked.append(rid)
        return revoked

    # ---------- 查询 ----------

    def list_pending(self) -> list[HitlPending]:
        now = time.time()
        with self._lock:
            sessions = list(self._sessions.values())
        out: list[HitlPending] = []
        for s in sessions:
            req = s.request
            out.append(
                HitlPending(
                    request_id=req.request_id,
                    sn=req.sn,
                    run_id=req.run_id,
                    case_id=req.case_id,
                    event_seq=req.event_seq,
                    capability_id=req.capability_id,
                    kind=req.kind,
                    title=req.title,
                    created_at=req.created_at,
                    deadline_at=req.deadline_at,
                    waiting_ms=max(0, int((now - req.created_at) * 1000)),
                )
            )
        return out

    def get_request(self, request_id: str) -> Optional[HitlRequest]:
        with self._lock:
            s = self._sessions.get(request_id)
            return s.request if s else None

    def pending_count(self) -> int:
        with self._lock:
            return len(self._sessions)


# ---------- 单例 ----------

_default_manager: Optional[HitlSessionManager] = None
_default_lock = threading.Lock()


def get_session_manager() -> HitlSessionManager:
    """进程级单例。"""
    global _default_manager
    with _default_lock:
        if _default_manager is None:
            _default_manager = HitlSessionManager()
        return _default_manager
