# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Tentacle 能力节点执行封装。"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from script.log import SLog

TAG = "CopilotExecutor"

def _task_payload(
    node_code: str,
    *,
    platform: str = "mobile",
    data: Optional[Dict] = None,
    display_name: str = "copilot",
) -> Dict[str, Any]:
    return {
        "id": f"copilot-{uuid.uuid4().hex[:8]}",
        "nodeCode": node_code,
        "nodeType": 200,
        "platform": platform,
        "displayName": display_name,
        "lastCodes": [],
        "nextCodes": [],
        "data": data or {},
    }


def _execute_ability(payload: Dict[str, Any]) -> Dict[str, Any]:
    from driver.tentacle.manager import Manager

    try:
        result = Manager().execute_interface(payload)
        if result is None:
            return {"ok": False, "msg": "组件未执行或节点被跳过"}
        if hasattr(result, "to_dict"):
            d = result.to_dict()
            ok = d.get("success", d.get("code") in (200, None))
            return {"ok": bool(ok), "msg": d.get("msg", ""), "data": d}
        return {"ok": True, "data": result}
    except Exception as e:
        SLog.e(TAG, f"execute failed: {e}")
        return {"ok": False, "msg": str(e)}
