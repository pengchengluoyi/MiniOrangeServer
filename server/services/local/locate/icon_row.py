# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
通用无字图标行检测：不绑定登录页、不锁定固定 Y 带。

从 hierarchy 可点击节点中聚类出「同一水平行、小尺寸、无长文案」的图标组，
适用于登录第三方图标、工具栏图标、底栏上图标等。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from script.log import SLog

TAG = "LocateIconRow"

_SKIP_TEXT_FRAGMENTS = (
    "协议",
    "阅读",
    "同意",
    "隐私",
    "用户",
    "条款",
    "checkbox",
    "CheckBox",
    "运营商",
    "认证服务",
)


def _is_icon_like_node(w: int, h: int, label: str, screen_w: int, screen_h: int) -> bool:
    if w < 18 or h < 18:
        return False
    if w > int(screen_w * 0.28) or h > int(screen_h * 0.12):
        return False
    lbl = (label or "").strip()
    low = lbl.lower()
    if any(k in lbl or k in low for k in _SKIP_TEXT_FRAGMENTS):
        return False
    if len(lbl) > 10 and re.search(r"[\u4e00-\u9fff]{4,}", lbl):
        return False
    return True


def _node_to_dict(node: Any, source: str = "hierarchy") -> Dict[str, Any]:
    x, y, w, h = int(node.x), int(node.y), int(node.w), int(node.h)
    return {
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "cx": x + w // 2,
        "cy": y + h // 2,
        "label": (getattr(node, "label", None) or "").strip(),
        "source": source,
    }


def detect_icon_rows(
    clickables: List[Any],
    *,
    screen_w: int,
    screen_h: int,
    min_icons: int = 2,
    y_band_ratio: float = 0.04,
) -> List[List[Dict[str, Any]]]:
    """
    返回多行图标组，每行按 x 从左到右排序。
    y_band_ratio：同一行内 cy 允许偏差（相对屏高）。
    """
    icons: List[Dict[str, Any]] = []
    for t in clickables or []:
        w, h = int(t.w), int(t.h)
        lbl = (getattr(t, "label", None) or "").strip()
        if _is_icon_like_node(w, h, lbl, screen_w, screen_h):
            icons.append(_node_to_dict(t))

    if len(icons) < min_icons:
        return []

    band = max(28, int(screen_h * y_band_ratio))
    icons.sort(key=lambda c: c["cy"])
    rows: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    row_y: Optional[int] = None

    for icon in icons:
        cy = icon["cy"]
        if row_y is None or abs(cy - row_y) <= band:
            current.append(icon)
            if row_y is None:
                row_y = cy
            else:
                row_y = (row_y + cy) // 2
        else:
            if len(current) >= min_icons:
                current.sort(key=lambda c: c["x"])
                rows.append(current)
            current = [icon]
            row_y = cy

    if len(current) >= min_icons:
        current.sort(key=lambda c: c["x"])
        rows.append(current)

    if rows:
        SLog.i(TAG, f"detected {len(rows)} icon row(s) sizes={[len(r) for r in rows]}")
    return rows


def flatten_icon_row_candidates(rows: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        for c in row:
            c = dict(c)
            c["icon_row"] = True
            out.append(c)
    return out
