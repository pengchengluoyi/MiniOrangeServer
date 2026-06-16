# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""定位调试信息（多通道仲裁与点击结果共享）。"""
from __future__ import annotations

from typing import Any, Dict, Optional

_LAST_LOCATE_DEBUG: Optional[Dict[str, Any]] = None


def pop_locate_debug() -> Optional[Dict[str, Any]]:
    global _LAST_LOCATE_DEBUG
    dbg = _LAST_LOCATE_DEBUG
    _LAST_LOCATE_DEBUG = None
    return dbg


def clear_locate_debug() -> None:
    global _LAST_LOCATE_DEBUG
    _LAST_LOCATE_DEBUG = None


def stash_locate_debug(debug: Optional[Dict[str, Any]]) -> None:
    global _LAST_LOCATE_DEBUG
    _LAST_LOCATE_DEBUG = debug


def _make_toggle_locate_debug(
    cx: int,
    cy: int,
    method: str,
    label: str,
    *,
    w: int = 44,
    h: int = 44,
) -> Dict[str, Any]:
    row = {
        "channel": "toggle",
        "method": method,
        "label": label,
        "raw_score": 1.0,
        "final_score": 1.0,
        "cx": cx,
        "cy": cy,
        "w": w,
        "h": h,
        "selected": True,
        "detail": f"{method}@({cx},{cy})",
    }
    return {
        "query": label,
        "profile": "login",
        "target_kind": "toggle",
        "spatial_zones": [],
        "candidates": [row],
        "overlay": [row],
        "winner_channel": "toggle",
    }


def _with_locate_debug(payload: Dict[str, Any]) -> Dict[str, Any]:
    dbg = pop_locate_debug()
    if dbg:
        payload = {**payload, "locate_debug": dbg}
    return payload
