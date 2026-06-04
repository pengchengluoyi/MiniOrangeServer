# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""跑图任务：等待设备节点回传结果。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

_pending: Dict[str, Dict[str, Any]] = {}


async def wait_result(req_id: str, timeout: float = 3600) -> Dict:
    event = asyncio.Event()
    _pending[req_id] = {"event": event, "result": None}
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        result = _pending[req_id].get("result") or {"code": 500, "msg": "empty crawl result"}
        return result
    except asyncio.TimeoutError:
        return {"code": 504, "msg": "跑图超时，请检查设备连接与 App 状态"}
    finally:
        _pending.pop(req_id, None)


def complete(req_id: str, result: Dict) -> None:
    slot = _pending.get(req_id)
    if not slot:
        return
    payload = result
    if isinstance(result, dict) and "payload" in result and "code" not in result:
        payload = result.get("payload") or result
    slot["result"] = payload if isinstance(payload, dict) and "code" in payload else result
    slot["event"].set()
