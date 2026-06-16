# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""从自然语言解析屏幕空间约束（九宫格及组合），供所有定位通道共用。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

# 归一化矩形 (x0, y0, x1, y1)，相对屏宽/屏高 0~1
ZONE_RECTS: dict[str, Tuple[float, float, float, float]] = {
    "top_left": (0.0, 0.0, 0.34, 0.34),
    "top_center": (0.33, 0.0, 0.67, 0.34),
    "top_right": (0.66, 0.0, 1.0, 0.34),
    "middle_left": (0.0, 0.33, 0.34, 0.67),
    "center": (0.33, 0.33, 0.67, 0.67),
    "middle_right": (0.66, 0.33, 1.0, 0.67),
    "bottom_left": (0.0, 0.66, 0.34, 1.0),
    "bottom_center": (0.33, 0.66, 0.67, 1.0),
    "bottom_right": (0.66, 0.66, 1.0, 1.0),
}

# 短语 → 一个或多个 zone（并集：点在任一 zone 内即通过）
_PHRASE_TO_ZONES: List[Tuple[re.Pattern, List[str]]] = [
    (re.compile(r"左上角|左上"), ["top_left"]),
    (re.compile(r"右上角|右上"), ["top_right"]),
    (re.compile(r"左下角|左下"), ["bottom_left"]),
    (re.compile(r"右下角|右下"), ["bottom_right"]),
    (re.compile(r"中上|上部中间|上中"), ["top_center"]),
    (re.compile(r"中下|下部中间|下中"), ["bottom_center"]),
    (re.compile(r"中间|正中|居中|中央"), ["center"]),
    (re.compile(r"左侧|左边"), ["top_left", "middle_left", "bottom_left"]),
    (re.compile(r"右侧|右边"), ["top_right", "middle_right", "bottom_right"]),
    (re.compile(r"顶部|上侧|上方"), ["top_left", "top_center", "top_right"]),
    (re.compile(r"底部|底栏|下侧|下方"), ["bottom_left", "bottom_center", "bottom_right"]),
    (re.compile(r"左中|中部左侧"), ["middle_left"]),
    (re.compile(r"右中|中部右侧"), ["middle_right"]),
]


@dataclass
class SpatialConstraint:
    """指令中的空间约束 + 剥离方位后的核心文案。"""

    zones: Set[str] = field(default_factory=set)
    core_text: str = ""

    @property
    def active(self) -> bool:
        return bool(self.zones)


def parse_spatial_constraint(label: str) -> SpatialConstraint:
    raw = (label or "").strip()
    zones: Set[str] = set()
    for pat, names in _PHRASE_TO_ZONES:
        if pat.search(raw):
            zones.update(names)

    q = re.sub(r"^(点击|点一下|tap|click|进入|打开)\s*", "", raw, flags=re.I).strip()
    q = re.sub(r"^(登录页(面)?|页面|当前页)(上的|中的)?\s*", "", q)
    for pat, names in _PHRASE_TO_ZONES:
        q = pat.sub("", q)
    q = re.sub(r"(的)\s*$", "", q).strip("「」『』【】\"' \t的")
    return SpatialConstraint(zones=zones, core_text=q or raw)


def point_in_zones(
    cx: int,
    cy: int,
    screen_w: int,
    screen_h: int,
    zones: Set[str],
) -> bool:
    if not zones or screen_w <= 0 or screen_h <= 0:
        return True
    nx = cx / screen_w
    ny = cy / screen_h
    for name in zones:
        rect = ZONE_RECTS.get(name)
        if not rect:
            continue
        x0, y0, x1, y1 = rect
        if x0 <= nx <= x1 and y0 <= ny <= y1:
            return True
    return False


def clip_region_hint(zones: Set[str]) -> str:
    """CLIP 始终全屏检索；方位约束仅由 point_in_zones + zones 完成。"""
    _ = zones
    return "full"
