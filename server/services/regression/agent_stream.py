# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Agent 执行流式事件推送：把 agent 每步的 thought/action/截图 通过 WS 广播给前端。

复用 DeviceManager.broadcast_to_observers（HITL 同款 observer 通道）。AgentExecutor 跑在
worker 线程，经主 event loop（DeviceManager().loop，main.py lifespan 注入）投递，fire-and-forget
不阻塞执行循环。截图压成小 JPEG 缩略图（默认宽 360）以控制 WS 载荷。
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from io import BytesIO
from threading import Lock
from typing import Any

from script.log import SLog

TAG = "AgentStream"

# 内存缓冲最近若干 run 的 agent_step 事件，供 AgentRun 页面历史回填（随时/事后可看）。
_MAX_RUNS = 20
_MAX_EVENTS_PER_RUN = 200
_RUNS: "OrderedDict[str, dict]" = OrderedDict()
_LOCK = Lock()


def _buffer(data: dict[str, Any]) -> None:
    run_id = data.get("run_id")
    if not run_id:
        return
    with _LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            run = {"run_id": run_id, "case_id": data.get("case_id", ""),
                   "goal": data.get("goal", ""), "events": [], "overall": "", "finished": False}
            _RUNS[run_id] = run
            while len(_RUNS) > _MAX_RUNS:
                _RUNS.popitem(last=False)
        _RUNS.move_to_end(run_id)
        if data.get("goal"):
            run["goal"] = data["goal"]
        if data.get("case_id"):
            run["case_id"] = data["case_id"]
        if data.get("phase") == "done":
            run["overall"] = data.get("overall", "")
            run["finished"] = True
        run["events"].append(data)
        if len(run["events"]) > _MAX_EVENTS_PER_RUN:
            run["events"] = run["events"][-_MAX_EVENTS_PER_RUN:]


def list_recent_runs() -> list[dict[str, Any]]:
    with _LOCK:
        return [
            {"run_id": r["run_id"], "case_id": r["case_id"], "goal": r["goal"],
             "overall": r["overall"], "finished": r["finished"], "steps": len(r["events"])}
            for r in reversed(_RUNS.values())
        ]


def get_run_events(run_id: str) -> dict[str, Any] | None:
    with _LOCK:
        r = _RUNS.get(run_id)
        return dict(r) if r else None



def make_thumb(png_b64: str, *, width: int = 360, quality: int = 70) -> str:
    """PNG/JPEG base64 → 缩略 JPEG base64（不含 data: 前缀）。失败返回空串。"""
    if not png_b64:
        return ""
    try:
        from PIL import Image

        raw_b64 = png_b64.strip()
        if raw_b64.startswith("data:") and "," in raw_b64:
            raw_b64 = raw_b64.split(",", 1)[1]
        raw = base64.b64decode(raw_b64)
        img = Image.open(BytesIO(raw)).convert("RGB")
        w, h = img.size
        if w > width:
            img = img.resize((width, max(1, int(h * width / w))))
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:  # pragma: no cover
        SLog.d(TAG, f"make_thumb failed: {e}")
        return ""


def emit_agent_event(data: dict[str, Any]) -> None:
    """向前端 observers 广播一个 agent_step 事件（fire-and-forget），并缓冲供历史回填。"""
    _buffer(data)
    try:
        import asyncio

        from server.websocket.device_manager import DeviceManager

        dm = DeviceManager()
        loop = getattr(dm, "loop", None)
        if loop is None:
            return
        payload = {"type": "agent_step", "data": data}
        asyncio.run_coroutine_threadsafe(dm.broadcast_to_observers(payload), loop)
    except Exception as e:  # pragma: no cover
        SLog.d(TAG, f"emit_agent_event failed: {e}")
