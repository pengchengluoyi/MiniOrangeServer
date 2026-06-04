# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""跑图热区：本地骨架 + ComponentDetector，与图谱「检测热区」同源。"""
from __future__ import annotations

import os
import tempfile
import uuid
from typing import List, Optional, Tuple

import cv2
import numpy as np

from script.log import SLog
from driver.agent.Crawl.ui_discovery import ClickTarget

TAG = "CrawlHotspot"


def _train_mask_from_frames(
    frames: List[np.ndarray],
    threshold: int = 10,
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray], Optional[dict]]:
    """从内存截图生成骨架蒙版（与 SkeletonAlgo.train_skeleton 逻辑一致）。"""
    from server.core.vision.skeleton_algo import SkeletonAlgo

    grays: List[np.ndarray] = []
    for f in frames:
        g = SkeletonAlgo._to_gray(f)
        if g is not None:
            grays.append(g)
    if not grays:
        return None, None, None

    base = grays[0]
    h, w = base.shape[:2]
    system_bars = SkeletonAlgo.detect_system_bars(base)
    final_mask = np.ones((h, w), dtype=np.uint8) * 255
    final_mask = SkeletonAlgo.apply_ignored_areas(final_mask, system_bars)

    if len(grays) == 1:
        return final_mask, base, system_bars

    for curr in grays[1:]:
        if curr.shape != base.shape:
            curr = cv2.resize(curr, (w, h))
        diff = cv2.absdiff(base, curr)
        diff = SkeletonAlgo._zero_system_bars(diff, system_bars)
        _, diff_thresh = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
        static_part = cv2.bitwise_not(diff_thresh)
        static_part = SkeletonAlgo.apply_ignored_areas(static_part, system_bars)
        final_mask = cv2.bitwise_and(final_mask, static_part)

    kernel = np.ones((3, 3), np.uint8)
    final_mask = cv2.morphologyEx(final_mask, cv2.MORPH_OPEN, kernel)
    final_mask = SkeletonAlgo.apply_ignored_areas(final_mask, system_bars)
    return final_mask, base, system_bars


def _ocr_crop_label(frame: np.ndarray, x: int, y: int, w: int, h: int) -> str:
    try:
        from driver.agent.Perception.Vision.mOcr import analyze
    except Exception:
        return ""
    fh, fw = frame.shape[:2]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(fw, x + w), min(fh, y + h)
    if x2 <= x1 or y2 <= y1:
        return ""
    crop = frame[y1:y2, x1:x2]
    path = os.path.join(tempfile.gettempdir(), f"crawl_ocr_{uuid.uuid4().hex[:8]}.png")
    try:
        cv2.imwrite(path, crop)
        items = analyze(path) or []
        texts = [(it.get("text") or "").strip() for it in items if (it.get("text") or "").strip()]
        if texts:
            return texts[0][:32]
    except Exception as e:
        SLog.d(TAG, f"ocr crop failed: {e}")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return ""


def discover_hotspots_from_frames(
    frames: List[np.ndarray],
    screen_w: int,
    screen_h: int,
    *,
    max_items: int = 24,
    ocr_labels: bool = True,
) -> List[ClickTarget]:
    """
    从当前页 1~N 张截图检测热区，转为跑图 ClickTarget。
    优先 tab/导航，再内容卡片。
    """
    if not frames:
        return []

    from server.core.vision.component_detector import ComponentDetector

    mask, shot_gray, system_bars = _train_mask_from_frames(frames[:4])
    if mask is None:
        return []

    ref = frames[0]
    fh, fw = ref.shape[:2]
    detected = ComponentDetector.detect_for_page(
        mask=mask,
        screenshot_path=None,
        img_w=fw,
        img_h=fh,
        system_bars=system_bars,
    )
    if not detected:
        return []

    scale_x = screen_w / max(fw, 1)
    scale_y = screen_h / max(fh, 1)
    targets: List[ClickTarget] = []

    for comp in detected[:max_items]:
        rx = int(comp.get("x", 0) * scale_x)
        ry = int(comp.get("y", 0) * scale_y)
        rw = max(12, int(comp.get("w", 0) * scale_x))
        rh = max(12, int(comp.get("h", 0) * scale_y))
        label = (comp.get("label") or "").strip()
        ctype = comp.get("component_type") or "repeat_card"
        category = comp.get("category") or "action"
        shared = comp.get("shared_region") or ""

        if ocr_labels and (not label or label.startswith("区域") or label == "热区"):
            ocr_text = _ocr_crop_label(ref, rx, ry, rw, rh)
            if ocr_text:
                label = ocr_text

        if not label or label in ("热区",):
            if ctype == "tab_item":
                label = f"tab_{len(targets)}"
            else:
                label = f"区域{len(targets) + 1}"

        targets.append(
            ClickTarget(
                x=rx,
                y=ry,
                w=rw,
                h=rh,
                label=label[:32],
                source="hotspot",
                resource_id=ctype,
                category=category,
                component_type=ctype,
                shared_region=shared,
            )
        )

    def _prio(t: ClickTarget) -> Tuple[int, int, int]:
        if t.component_type == "tab_item":
            return (0, t.y, t.x)
        if t.category == "navigation":
            return (1, t.y, t.x)
        return (2, t.y, t.x)

    # 单图骨架常只有顶/底栏 Tab，补内容区重复块（卡片/列表）
    cards = sum(1 for t in targets if t.component_type == "repeat_card")
    if cards < 2 and shot_gray is not None:
        repeated = ComponentDetector.detect_repeated_from_image(shot_gray, system_bars, min_repeat=2)
        seen_boxes = {(t.x // 16, t.y // 16) for t in targets}
        for comp in repeated[:16]:
            rx = int(comp.get("x", 0) * scale_x)
            ry = int(comp.get("y", 0) * scale_y)
            rw = max(12, int(comp.get("w", 0) * scale_x))
            rh = max(12, int(comp.get("h", 0) * scale_y))
            key = (rx // 16, ry // 16)
            if key in seen_boxes:
                continue
            seen_boxes.add(key)
            targets.append(
                ClickTarget(
                    x=rx, y=ry, w=rw, h=rh,
                    label=(comp.get("label") or f"区域{len(targets) + 1}")[:32],
                    source="hotspot",
                    resource_id="repeat_card",
                    category="action",
                    component_type="repeat_card",
                    shared_region="content",
                )
            )

    targets.sort(key=_prio)
    n_tab = sum(1 for t in targets if t.component_type == "tab_item")
    n_card = sum(1 for t in targets if t.component_type == "repeat_card")
    SLog.i(TAG, f"Hotspots detected: {len(targets)} (tabs={n_tab}, cards={n_card})")
    return targets


def _default_content_band(screen_w: int, screen_h: int) -> ClickTarget:
    """无内容热区时的中间可滑动区域。"""
    return ClickTarget(
        x=int(screen_w * 0.06),
        y=int(screen_h * 0.14),
        w=int(screen_w * 0.88),
        h=int(screen_h * 0.68),
        label="content_feed",
        source="hotspot",
        resource_id="repeat_card",
        category="action",
        component_type="repeat_card",
        shared_region="content",
    )


def click_targets_for_explore(
    hotspots: List[ClickTarget],
    screen_w: int,
    screen_h: int,
    *,
    max_items: int = 16,
) -> List[ClickTarget]:
    """同页探索用：可点的内容热区（不点底栏/顶栏 Tab）。"""
    out: List[ClickTarget] = []
    for t in hotspots:
        if t.component_type == "tab_item":
            continue
        if t.shared_region in ("bottom_tab", "top_header"):
            continue
        cy = t.y + t.h // 2
        if cy < screen_h * 0.12 or cy > screen_h * 0.88:
            continue
        if t.w >= 24 and t.h >= 24:
            out.append(t)
    if not out:
        band = _default_content_band(screen_w, screen_h)
        out.append(
            ClickTarget(
                x=band.x + band.w // 2 - 20,
                y=band.y + band.h // 2 - 20,
                w=40,
                h=40,
                label="content_center",
                source="hotspot",
                component_type="repeat_card",
                shared_region="content",
            )
        )
    return out[:max_items]


def explore_targets_for_same_page(
    hotspots: List[ClickTarget],
    screen_w: int = 1080,
    screen_h: int = 1920,
) -> List[ClickTarget]:
    """同页多图：Feed 滑动区域（仅 20% 交互使用）。"""
    return [_default_content_band(screen_w, screen_h)]


def _synthetic_bottom_tabs(screen_w: int, screen_h: int, slots: int = 5) -> List[ClickTarget]:
    """骨架只检出顶栏时，按屏底均分底栏点击位（常见 4~5 Tab）。"""
    y = int(screen_h * 0.91)
    h = max(40, int(screen_h * 0.06))
    slot_w = max(48, screen_w // slots)
    out: List[ClickTarget] = []
    for i in range(slots):
        out.append(
            ClickTarget(
                x=i * slot_w,
                y=y,
                w=slot_w,
                h=h,
                label=f"tab_{i + 1}",
                source="hotspot",
                resource_id="tab_item",
                category="navigation",
                component_type="tab_item",
                shared_region="bottom_tab",
            )
        )
    return out


def _split_bottom_tab_bar(tab: ClickTarget, screen_w: int, slots: int = 5) -> List[ClickTarget]:
    """底栏只检出一条宽热区时，按 X 均分为多个可点 Tab。"""
    if tab.w < screen_w * 0.25:
        return [tab]
    slot_w = max(48, tab.w // slots)
    out: List[ClickTarget] = []
    for i in range(slots):
        sx = tab.x + i * slot_w
        if sx + slot_w > tab.x + tab.w:
            break
        out.append(
            ClickTarget(
                x=sx,
                y=tab.y,
                w=slot_w,
                h=tab.h,
                label=f"tab_{i + 1}",
                source="hotspot",
                resource_id="tab_item",
                category="navigation",
                component_type="tab_item",
                shared_region="bottom_tab",
            )
        )
    return out or [tab]


def navigation_targets_from_hotspots(
    hotspots: List[ClickTarget],
    screen_w: int = 1080,
    screen_h: int = 1920,
) -> List[ClickTarget]:
    """页面跳转：仅底栏 Tab（不点顶栏），其次可点击内容区。"""
    bottom_tabs = [
        t for t in hotspots
        if t.component_type == "tab_item"
        and (
            t.shared_region == "bottom_tab"
            or (t.y + t.h // 2) > screen_h * 0.82
        )
    ]
    if len(bottom_tabs) == 1 and bottom_tabs[0].w > screen_w * 0.28:
        bottom_tabs = _split_bottom_tab_bar(bottom_tabs[0], screen_w)
    if bottom_tabs:
        return bottom_tabs

    tabs = [t for t in hotspots if t.component_type == "tab_item"]
    if tabs and all((t.y + t.h // 2) < screen_h * 0.28 for t in tabs):
        SLog.w(TAG, "Only top tabs detected, use synthetic bottom tab bar for navigation")
        return _synthetic_bottom_tabs(screen_w, screen_h)
    if len(tabs) == 1 and tabs[0].w > screen_w * 0.28 and (tabs[0].y + tabs[0].h // 2) > screen_h * 0.75:
        return _split_bottom_tab_bar(tabs[0], screen_w)
    nav = [t for t in hotspots if t.category == "navigation"]
    if nav:
        return nav

    # 底栏 Tab + 可点内容热区（跑图以点击为主）
    result: List[ClickTarget] = list(_synthetic_bottom_tabs(screen_w, screen_h))
    seen = {(t.x // 16, t.y // 16) for t in result}
    for t in hotspots:
        if t.component_type == "tab_item":
            continue
        if t.shared_region in ("top_header", "bottom_tab"):
            continue
        cy = t.y + t.h // 2
        if cy > screen_h * 0.88 or cy < screen_h * 0.1:
            continue
        key = (t.x // 16, t.y // 16)
        if key in seen:
            continue
        seen.add(key)
        result.append(t)
        if len(result) >= 24:
            break
    return result
