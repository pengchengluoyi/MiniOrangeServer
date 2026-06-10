# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""从 UI 层级 / OCR 发现可点击目标。"""
from __future__ import annotations

import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional, Tuple

from script.log import SLog

TAG = "UiDiscovery"


@dataclass
class ClickTarget:
    x: int
    y: int
    w: int
    h: int
    label: str
    source: str  # hierarchy | ocr | hotspot | grid
    resource_id: str = ""
    category: str = "action"
    component_type: str = "repeat_card"
    shared_region: str = ""

    @property
    def center(self) -> Tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2


def _parse_bounds(bounds: str) -> Optional[Tuple[int, int, int, int]]:
    nums = re.findall(r"\d+", bounds or "")
    if len(nums) != 4:
        return None
    x1, y1, x2, y2 = map(int, nums)
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2 - x1, y2 - y1


def _is_icon_only_label(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if len(t) <= 2 and not re.search(r"[\u4e00-\u9fff]", t):
        return True
    if re.fullmatch(r"[\W_\d]+", t):
        return True
    return False


def page_name_from_label(label: str) -> str:
    """点击文案 → 页面名；纯图标 → 随机唯一名。"""
    t = (label or "").strip()
    if not t or _is_icon_only_label(t):
        return f"page_{uuid.uuid4().hex[:8]}"
    if t.endswith("页面"):
        return t
    return f"{t}页面"


def discover_clickables_from_hierarchy(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    max_items: int = 24,
) -> List[ClickTarget]:
    """解析 Android UIAutomator dump，提取 clickable 节点。"""
    targets: List[ClickTarget] = []
    try:
        xml_data = ""
        if hasattr(engine, "dump_hierarchy_xml"):
            xml_data = engine.dump_hierarchy_xml() or ""
        if not xml_data or "<?xml" not in xml_data:
            engine.shell("uiautomator dump /sdcard/view.xml")
            xml_data = engine.shell("cat /sdcard/view.xml")
        if not xml_data or "<?xml" not in xml_data:
            return targets
        root = ET.fromstring(xml_data)
    except Exception as e:
        SLog.w(TAG, f"dump hierarchy failed: {e}")
        return targets

    y_top = int(screen_h * 0.08)
    y_bottom = int(screen_h * 0.92)

    seen: set = set()
    for node in root.iter("node"):
        if node.get("clickable") != "true":
            continue
        rect = _parse_bounds(node.get("bounds") or "")
        if not rect:
            continue
        x, y, w, h = rect
        if w < 12 or h < 12:
            continue
        cy = y + h // 2
        if cy < y_top or cy > y_bottom:
            continue
        if w * h > screen_w * screen_h * 0.35:
            continue

        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        label = text or desc or node.get("resource-id") or ""
        label = label.split("/")[-1] if "/" in label else label
        if not label:
            label = f"icon_{len(targets)}"

        key = (x // 8, y // 8, w // 8, h // 8)
        if key in seen:
            continue
        seen.add(key)

        targets.append(
            ClickTarget(
                x=x, y=y, w=w, h=h,
                label=label[:32],
                source="hierarchy",
                resource_id=node.get("resource-id") or "",
            )
        )
        if len(targets) >= max_items:
            break

    targets.sort(key=lambda t: (t.y, t.x))
    return targets


_OCR_SHOT_CACHE: dict = {}
_OCR_SHOT_CACHE_MAX = 24
_OCR_SHOT_CACHE_TTL_SEC = 3.0


def _ocr_shot_cache_key(shot_or_path) -> str:
    import hashlib
    import time as _time

    if shot_or_path is None:
        return ""
    if isinstance(shot_or_path, str):
        if not os.path.exists(shot_or_path):
            return ""
        try:
            st = os.stat(shot_or_path)
            return f"path:{shot_or_path}:{int(st.st_mtime)}:{st.st_size}"
        except OSError:
            return f"path:{shot_or_path}"
    try:
        import numpy as np

        arr = np.asarray(shot_or_path)
        if arr.size == 0:
            return ""
        sample = arr.reshape(-1)[:4096].tobytes()
        digest = hashlib.md5(sample).hexdigest()
        return f"arr:{arr.shape}:{digest}:{int(_time.time() // _OCR_SHOT_CACHE_TTL_SEC)}"
    except Exception:
        return ""


def _ocr_analyze_shot(shot_or_path) -> list:
    """engine.screenshot() 可能返回路径或 PIL.Image，统一交给 OCR。"""
    try:
        from driver.agent.Perception.Vision.mOcr import analyze
    except Exception:
        return []
    if shot_or_path is None:
        return []
    cache_key = _ocr_shot_cache_key(shot_or_path)
    if cache_key:
        import time as _time

        hit = _OCR_SHOT_CACHE.get(cache_key)
        if hit and _time.time() - hit["ts"] < _OCR_SHOT_CACHE_TTL_SEC:
            return list(hit["items"])
    if isinstance(shot_or_path, str):
        if not os.path.exists(shot_or_path):
            return []
        items = analyze(shot_or_path) or []
    else:
        try:
            import numpy as np
        except ImportError:
            return []
        arr = np.array(shot_or_path)
        if arr.ndim == 3 and arr.shape[2] >= 3:
            arr = arr[:, :, :3][:, :, ::-1]
        items = analyze("", img=arr) or []
    if cache_key:
        import time as _time

        if len(_OCR_SHOT_CACHE) >= _OCR_SHOT_CACHE_MAX:
            oldest = min(_OCR_SHOT_CACHE, key=lambda k: _OCR_SHOT_CACHE[k]["ts"])
            _OCR_SHOT_CACHE.pop(oldest, None)
        _OCR_SHOT_CACHE[cache_key] = {"ts": _time.time(), "items": items}
    return items


def discover_clickables_ocr(
    shot_or_path,
    screen_w: int,
    screen_h: int,
    *,
    max_items: int = 16,
) -> List[ClickTarget]:
    """OCR 文本块作为可点区域（兜底）。shot_or_path 为文件路径或 PIL 截图。"""
    items = _ocr_analyze_shot(shot_or_path)
    targets: List[ClickTarget] = []
    y_top = int(screen_h * 0.1)
    y_bottom = int(screen_h * 0.9)

    for it in items:
        text = (it.get("text") or "").strip()
        box = it.get("box")
        if not text or box is None:
            continue
        try:
            if len(box) < 4:
                continue
        except TypeError:
            continue
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        x1, x2 = int(min(xs)), int(max(xs))
        y1, y2 = int(min(ys)), int(max(ys))
        w, h = x2 - x1, y2 - y1
        if w < 20 or h < 14:
            continue
        cy = y1 + h // 2
        if cy < y_top or cy > y_bottom:
            continue
        targets.append(
            ClickTarget(x=x1, y=y1, w=w, h=h, label=text[:32], source="ocr")
        )
        if len(targets) >= max_items:
            break
    return targets
