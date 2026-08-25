# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""覆盖生成的异步任务：进度、取消、流式落库。

tick 以前是同步 HTTP，前端硬等 10 分钟。现在 POST 立刻返回 job_id，
生成过程按分片上报 done/total，每写完一枝/一批就经 flush 回写 qa_process。

任务挂在进程内存里。服务重启后 GET 会 404，前端停转圈即可；
已经 flush 过的脑图和用例还在 automation config 里，不会整单丢失。
"""
from __future__ import annotations

import contextvars
import threading
import time
import uuid
from typing import Any, Callable, Optional

_JOB: contextvars.ContextVar[Optional["CoverJob"]] = contextvars.ContextVar("qa_cover_job", default=None)
_LOCK = threading.Lock()
_JOBS: dict[str, "CoverJob"] = {}
_APP_RUNNING: dict[str, str] = {}
_TTL_SEC = 2 * 60 * 60
_MAX = 64


class Cancelled(Exception):
    """人点了取消。正在进行的那一次模型调用会跑完，下一次 _ask_json 起不再发。"""


class CoverJob:
    def __init__(self, *, app_id: str, requirement_id: str = "", jobs: list | None = None):
        self.id = uuid.uuid4().hex[:16]
        self.app_id = str(app_id or "")
        self.requirement_id = str(requirement_id or "")
        self.jobs = [str(x) for x in (jobs or []) if str(x).strip()]
        self.status = "running"  # running | done | cancelled | error
        self.phase = "queued"
        self.label = "排队中"
        self.done = 0
        self.total = 0
        self.failures: list[dict] = []
        self.error = ""
        self.result: dict = {}
        self.doc: dict | None = None
        self.working_req: dict | None = None
        self.flush: Optional[Callable[[dict], None]] = None
        self.created_at = time.time()
        self.updated_at = self.created_at
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def cancel(self) -> None:
        self._cancel.set()
        with self._lock:
            if self.status == "running":
                self.status = "cancelled"
                self.label = "已取消"
                self.updated_at = time.time()

    def report(self, *, phase: str = "", label: str = "", done: int | None = None, total: int | None = None, inc: int = 0) -> None:
        self.check()
        with self._lock:
            if phase:
                self.phase = phase
            if label:
                self.label = label
            if total is not None:
                self.total = max(0, int(total))
            if done is not None:
                self.done = max(0, int(done))
            if inc:
                self.done = max(0, self.done) + max(0, int(inc))
            if self.total and self.done > self.total:
                self.total = self.done
            self.updated_at = time.time()

    def add_total(self, n: int) -> None:
        self.check()
        with self._lock:
            self.total = max(0, self.total) + max(0, int(n))
            self.updated_at = time.time()

    def attach(self, doc: dict, *, working_req: dict | None = None) -> None:
        self.doc = doc
        if working_req is not None:
            self.working_req = working_req

    def save(self, doc: dict | None = None) -> None:
        payload = doc if isinstance(doc, dict) else self.doc
        if payload is None:
            return
        self.doc = payload
        snap = self.public()
        payload["cover_job"] = snap
        if self.flush:
            self.flush(payload)

    def finish(self, result: dict) -> None:
        with self._lock:
            self.result = result if isinstance(result, dict) else {}
            self.doc = (self.result.get("qa_process") if isinstance(self.result.get("qa_process"), dict) else self.doc)
            if self.status == "running":
                self.status = "done"
                self.phase = "done"
                self.label = "已完成"
                if self.total:
                    self.done = self.total
            self.updated_at = time.time()
        if isinstance(self.doc, dict):
            self.save(self.doc)

    def fail(self, error: str) -> None:
        with self._lock:
            if self.status == "running":
                self.status = "error"
            self.error = str(error or "")[:240]
            self.label = self.error or "失败"
            self.updated_at = time.time()
        if isinstance(self.doc, dict):
            self.save(self.doc)

    def mark_cancelled(self) -> None:
        with self._lock:
            self.status = "cancelled"
            self.phase = "cancelled"
            self.label = "已取消"
            self.updated_at = time.time()
        if isinstance(self.doc, dict):
            self.save(self.doc)

    def public(self) -> dict:
        with self._lock:
            return {
                "job_id": self.id,
                "app_id": self.app_id,
                "requirement_id": self.requirement_id,
                "status": self.status,
                "phase": self.phase,
                "label": self.label,
                "done": self.done,
                "total": self.total,
                "error": self.error,
                "failures": list(self.failures),
                "jobs": list(self.jobs),
                "updated_at": self.updated_at,
            }


def _gc_locked() -> None:
    now = time.time()
    dead = [jid for jid, job in _JOBS.items() if now - job.updated_at > _TTL_SEC]
    for jid in dead:
        job = _JOBS.pop(jid, None)
        if job and _APP_RUNNING.get(job.app_id) == jid:
            _APP_RUNNING.pop(job.app_id, None)
    while len(_JOBS) > _MAX:
        oldest = min(_JOBS.values(), key=lambda j: j.updated_at)
        _JOBS.pop(oldest.id, None)
        if _APP_RUNNING.get(oldest.app_id) == oldest.id:
            _APP_RUNNING.pop(oldest.app_id, None)


def create(*, app_id: str, requirement_id: str = "", jobs: list | None = None) -> CoverJob:
    job = CoverJob(app_id=app_id, requirement_id=requirement_id, jobs=jobs)
    with _LOCK:
        _gc_locked()
        running = _APP_RUNNING.get(job.app_id)
        if running and running in _JOBS and _JOBS[running].status == "running":
            raise JobConflict(_JOBS[running])
        _JOBS[job.id] = job
        _APP_RUNNING[job.app_id] = job.id
    return job


def get(job_id: str) -> Optional[CoverJob]:
    with _LOCK:
        return _JOBS.get(str(job_id or ""))


def running_for(app_id: str) -> Optional[CoverJob]:
    with _LOCK:
        jid = _APP_RUNNING.get(str(app_id or ""))
        job = _JOBS.get(jid or "")
        if job and job.status == "running":
            return job
        return None


def release(job: CoverJob) -> None:
    with _LOCK:
        if _APP_RUNNING.get(job.app_id) == job.id:
            _APP_RUNNING.pop(job.app_id, None)


def bind(job: Optional[CoverJob]):
    return _JOB.set(job)


def reset(token) -> None:
    _JOB.reset(token)


def current() -> Optional[CoverJob]:
    return _JOB.get()


def check() -> None:
    job = _JOB.get()
    if job is not None:
        job.check()


def report(**kwargs) -> None:
    job = _JOB.get()
    if job is not None:
        job.report(**kwargs)


def add_total(n: int) -> None:
    job = _JOB.get()
    if job is not None:
        job.add_total(n)


def save(doc: dict | None = None) -> None:
    job = _JOB.get()
    if job is not None:
        job.save(doc)


def inc(n: int = 1, **kwargs) -> None:
    job = _JOB.get()
    if job is not None:
        job.report(inc=n, **kwargs)


class JobConflict(Exception):
    def __init__(self, job: CoverJob):
        super().__init__("already running")
        self.job = job


__all__ = [
    "Cancelled",
    "CoverJob",
    "JobConflict",
    "add_total",
    "bind",
    "check",
    "create",
    "current",
    "get",
    "inc",
    "release",
    "report",
    "reset",
    "running_for",
    "save",
]
