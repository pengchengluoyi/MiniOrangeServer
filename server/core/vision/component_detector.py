# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""从页面骨架蒙版与主截图中检测细粒度 UI 组件热区（单卡、Tab、菜单项等）。"""
from __future__ import annotations

import uuid
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from script.log import SLog

TAG = "ComponentDetector"

# 顶/底导航条带比例（通用，不绑定某一 App 的 Tab 文案与个数）
TOP_STRIP_Y = (0.052, 0.118)
MAX_NAV_SLOTS = 8
BOTTOM_STRIP_Y = (0.905, 0.978)
# 底 Tab：固定 y/h，仅用骨架最底行切 X（多图训练时白色会连到「开始造物」）
BOTTOM_TAB_FIXED_Y = 0.932
BOTTOM_TAB_FIXED_H = 0.056
BOTTOM_TAB_X_BAND = (0.935, 0.995)
TOP_TAB_FIXED_Y = 0.068
TOP_TAB_FIXED_H = 0.052
MAX_TAB_HEIGHT_RATIO = 0.085
TOP_SLOT_KEEP_EDGE = 0.018
TOP_SLOT_EMPTY_EDGE = 0.014
TOP_SLOT_MIN_MASK_FILL = 0.06
# 细粒度：内容热区不超过屏宽/高的比例
MAX_CONTENT_WIDTH_RATIO = 0.48
MAX_CONTENT_HEIGHT_RATIO = 0.18
MAX_CONTENT_AREA_RATIO = 0.065
MIN_CARD_AREA_RATIO = 0.003


def _iou(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    union = aw * ah + bw * bh - inter
    return inter / max(union, 1)


def _is_duplicate_nav_box(
    a: Tuple[int, int, int, int],
    b: Tuple[int, int, int, int],
    img_h: int,
) -> bool:
    """仅合并真正重复的框（高度重叠+横向大量重叠），相邻 Tab 不合并。"""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    if abs((ay + ah / 2) - (by + bh / 2)) > max(8, img_h * 0.012):
        return False
    x1, x2 = max(ax, bx), min(ax + aw, bx + bw)
    if x2 <= x1:
        return False
    overlap_w = x2 - x1
    min_w = max(1, min(aw, bw))
    if overlap_w / min_w < 0.5:
        return False
    return _iou(a, b) >= 0.32


def _dedupe_nav_items(nav: List[Dict], img_h: int) -> List[Dict]:
    """去掉同位置重复检测，保留较大框。"""
    nav = sorted(nav, key=lambda c: c["w"] * c["h"], reverse=True)
    kept: List[Dict] = []
    for c in nav:
        box = (c["x"], c["y"], c["w"], c["h"])
        if any(_is_duplicate_nav_box(box, (k["x"], k["y"], k["w"], k["h"]), img_h) for k in kept):
            continue
        kept.append(c)
    kept.sort(key=lambda c: c["x"])
    return kept


def _merge_rects(rects: List[Dict], iou_threshold: float = 0.35) -> List[Dict]:
    if not rects:
        return []
    rects = sorted(rects, key=lambda r: r["w"] * r["h"], reverse=True)
    merged: List[Dict] = []
    for r in rects:
        box = (r["x"], r["y"], r["w"], r["h"])
        absorbed = False
        for m in merged:
            mbox = (m["x"], m["y"], m["w"], m["h"])
            iou_val = _iou(box, mbox)
            is_nav = r.get("component_type") == "tab_item" or m.get("component_type") == "tab_item"
            thr = 0.38 if is_nav else iou_threshold
            if iou_val >= thr:
                absorbed = True
                break
        if not absorbed:
            merged.append(dict(r))
    return merged


def _coalesce_segments(
    segments: List[Tuple[int, int]],
    max_pixel_gap: int = 6,
) -> List[Tuple[int, int]]:
    """只合并像素级裂缝（同一按钮被切成两段），不合并相邻 Tab。"""
    if len(segments) < 2:
        return segments
    segs = sorted(segments)
    out: List[Tuple[int, int]] = [segs[0]]
    for s, e in segs[1:]:
        ps, pe = out[-1]
        if s - pe <= max_pixel_gap:
            out[-1] = (ps, e)
        else:
            out.append((s, e))
    return out


def _make_component(
    x: int,
    y: int,
    w: int,
    h: int,
    label: str,
    *,
    category: str = "action",
    component_type: str = "custom",
    confidence: float = 0.7,
) -> Dict:
    return {
        "uid": f"comp-{uuid.uuid4().hex[:8]}",
        "x": int(x),
        "y": int(y),
        "w": int(w),
        "h": int(h),
        "label": label,
        "category": category,
        "component_type": component_type,
        "type": component_type,
        "needs_confirmation": True,
        "confidence": round(confidence, 2),
    }


def _is_oversized(w: int, h: int, img_w: int, img_h: int, comp_type: str) -> bool:
    if comp_type in ("bottom_tab_bar", "top_header", "tab_item", "fab"):
        return False
    if w >= img_w * 0.58 or h >= img_h * 0.28:
        return True
    if w * h >= img_w * img_h * MAX_CONTENT_AREA_RATIO:
        return True
    return False


def _tabs_from_layout(
    img_w: int,
    img_h: int,
    y0_ratio: float,
    y1_ratio: float,
    slots: List[Tuple[float, float, str]],
    region_key: str,
    y_min: int = 0,
    y_max: Optional[int] = None,
) -> List[Dict]:
    """按屏宽比例切槽，保证顶/底 Tab 大小一致、位置对齐。"""
    y0 = max(y_min, int(img_h * y0_ratio))
    y1 = min(y_max or img_h, int(img_h * y1_ratio))
    strip_h = y1 - y0
    if strip_h < 20 or img_w < 40:
        return []

    pad_y = max(4, int(strip_h * 0.1))
    inner_h = max(28, strip_h - 2 * pad_y)
    ty = y0 + pad_y

    results: List[Dict] = []
    for x_ratio, w_ratio, label in slots:
        tx = int(img_w * x_ratio)
        tw = max(32, int(img_w * w_ratio))
        if tx + tw > img_w - 2:
            tw = img_w - 2 - tx
        if tw < 28 or tx < 0:
            continue
        comp = _make_component(
            tx, ty, tw, inner_h, label,
            category="navigation",
            component_type="tab_item",
            confidence=0.9,
        )
        comp["shared_region"] = region_key
        results.append(comp)
    return results


def _tighten_to_white(
    mask: np.ndarray,
    x: int,
    y: int,
    w: int,
    h: int,
    pad: int = 2,
    y_band: Optional[Tuple[int, int]] = None,
) -> Optional[Tuple[int, int, int, int]]:
    """将候选框收紧到骨架蒙版白色像素外接矩形；y_band 限制只在顶/底条带内搜索。"""
    if mask is None or mask.size == 0:
        return x, y, w, h
    gray = cv2.cvtColor(mask, cv2.COLOR_BGR2GRAY) if mask.ndim == 3 else mask
    H, W = gray.shape[:2]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(W, x + w), min(H, y + h)
    if y_band is not None:
        y1 = max(y1, y_band[0])
        y2 = min(y2, y_band[1])
    crop = gray[y1:y2, x1:x2]
    if crop.size == 0:
        return None
    ys, xs = np.where(crop > 127)
    if len(xs) == 0:
        return None
    tx0, tx1 = int(xs.min()), int(xs.max()) + 1
    ty0, ty1 = int(ys.min()), int(ys.max()) + 1
    nx = max(0, x1 + tx0 - pad)
    ny = max(0, y1 + ty0 - pad)
    nw = min(W - nx, (tx1 - tx0) + 2 * pad)
    nh = min(H - ny, (ty1 - ty0) + 2 * pad)
    if y_band is not None:
        ny = max(ny, y_band[0])
        nh = min(nh, y_band[1] - ny)
    if nw < 8 or nh < 8:
        return None
    return (nx, ny, nw, nh)


def _bottom_tab_geometry(img_h: int) -> Tuple[int, int]:
    """四枚底 Tab 共用同一 y/h，避免各 Tab 高度不一致、顶部伸进 feed。"""
    th = max(22, int(img_h * BOTTOM_TAB_FIXED_H))
    ty = min(int(img_h * BOTTOM_TAB_FIXED_Y), img_h - th - 2)
    return ty, th


def _top_tab_geometry(img_h: int) -> Tuple[int, int]:
    """顶 Tab 共用固定 y/h，与底栏策略一致。"""
    th = max(22, int(img_h * TOP_TAB_FIXED_H))
    ty = max(int(img_h * TOP_STRIP_Y[0]), int(img_h * TOP_TAB_FIXED_Y))
    ty = min(ty, img_h - th - 2)
    return ty, th


def _clamp_tab_box(
    x: int,
    y: int,
    w: int,
    h: int,
    img_h: int,
    region_key: str,
) -> Tuple[int, int, int, int]:
    """Tab 框不得超出顶/底条带，高度有上限，避免盖住 feed。"""
    max_h = max(22, int(img_h * MAX_TAB_HEIGHT_RATIO))
    if region_key == "bottom_tab":
        ty, th = _bottom_tab_geometry(img_h)
        return x, ty, w, th
    elif region_key == "top_header":
        y = max(int(img_h * TOP_STRIP_Y[0]), y)
        h = min(h, max(22, int(img_h * MAX_TAB_HEIGHT_RATIO)))
        y = min(y, int(img_h * TOP_STRIP_Y[1]) - h)
        return x, max(0, y), w, h
    else:
        h = min(h, max_h)
    return x, max(0, y), w, max(22, h)


def _region_edge_density(
    gray: np.ndarray,
    x0: int,
    x1: int,
    y0: int,
    y1: int,
) -> float:
    """槽位内边缘占比：纯渐变背景很低，图标/文字明显更高。"""
    H, W = gray.shape[:2]
    x0, x1 = max(0, x0), min(W, x1)
    y0, y1 = max(0, y0), min(H, y1)
    if x1 - x0 < 16 or y1 - y0 < 10:
        return 0.0
    crop = gray[y0:y1, x0:x1]
    edges = cv2.Canny(crop, 40, 110)
    return float(np.sum(edges > 0)) / max(edges.size, 1)


def _column_segments_from_fill(
    col_fill: np.ndarray,
    cw: int,
    min_seg_w: int,
    max_slots: int = MAX_NAV_SLOTS,
) -> List[Tuple[int, int]]:
    """从列投影切导航槽：仅自然分段 + 峰检测，不按固定 4 Tab 均分。"""
    thresh = 0.12
    segments: List[Tuple[int, int]] = []
    in_seg = False
    start = 0
    for i in range(cw):
        if col_fill[i] >= thresh and not in_seg:
            start = i
            in_seg = True
        elif col_fill[i] < thresh and in_seg:
            if i - start >= min_seg_w:
                segments.append((start, i))
            in_seg = False
    if in_seg and cw - start >= min_seg_w:
        segments.append((start, cw))

    if len(segments) >= 2:
        return _coalesce_segments(segments[:max_slots], max_pixel_gap=8)

    kernel = max(5, cw // 60)
    smooth = np.convolve(col_fill, np.ones(kernel) / kernel, mode="same")
    peak_thr = float(np.max(smooth)) * 0.42
    if peak_thr <= 0:
        return segments
    min_dist = max(20, int(cw * 0.065))
    peaks: List[int] = []
    for i in range(1, len(smooth) - 1):
        if smooth[i] >= peak_thr and smooth[i] >= smooth[i - 1] and smooth[i] >= smooth[i + 1]:
            if not peaks or i - peaks[-1] >= min_dist:
                peaks.append(i)
    if len(peaks) < 2:
        return segments

    slot_w = max(min_seg_w, int(cw / len(peaks)))
    for px in peaks[:max_slots]:
        sx = max(0, px - slot_w // 2)
        ex = min(cw, sx + slot_w)
        if ex - sx >= min_seg_w:
            segments.append((sx, ex))
    return _coalesce_segments(segments[:max_slots], max_pixel_gap=8)


def _is_empty_top_slot(
    active: np.ndarray,
    sx: int,
    ex: int,
    screenshot_gray: Optional[np.ndarray],
    strip_y0: int,
    strip_y1: int,
    global_x: int = 0,
) -> bool:
    """True=无特征空槽（应跳过）；False=有效热区。宽槽用中心区域边缘，避免误判文字 Tab。"""
    sw = ex - sx
    if sw < 12:
        return True
    seg = active[:, max(0, sx):min(active.shape[1], ex)]
    fill = float(np.sum(seg > 127)) / max(seg.size, 1) if seg.size else 0.0

    if screenshot_gray is None:
        return fill < TOP_SLOT_MIN_MASK_FILL

    gx0, gx1 = global_x + sx, global_x + ex
    edge = _region_edge_density(screenshot_gray, gx0, gx1, strip_y0, strip_y1)
    if edge >= TOP_SLOT_KEEP_EDGE:
        return False
    if fill >= TOP_SLOT_MIN_MASK_FILL:
        return False
    W = screenshot_gray.shape[1]
    if sw > W * 0.16:
        cx0 = global_x + sx + int(sw * 0.22)
        cx1 = global_x + sx + int(sw * 0.78)
        peak = _region_edge_density(screenshot_gray, cx0, cx1, strip_y0, strip_y1)
        if peak >= TOP_SLOT_KEEP_EDGE:
            return False
    return edge < TOP_SLOT_EMPTY_EDGE


def _detect_top_icon_hotspots(
    screenshot_gray: np.ndarray,
    img_h: int,
    img_w: int,
) -> List[Dict]:
    """个人页等：顶栏左右小图标（耳机/分享/设置），不依赖整条 Tab 白条。"""
    y0, y1 = int(img_h * TOP_STRIP_Y[0]), int(img_h * TOP_STRIP_Y[1])
    roi = screenshot_gray[y0:y1, :]
    rh, rw = roi.shape[:2]
    if rh < 12 or rw < 40:
        return []

    edges = cv2.Canny(roi, 32, 95)
    dil = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
    contours, _ = cv2.findContours(dil, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    ty, th = _top_tab_geometry(img_h)
    min_a = max(36, int(rh * rw * 0.00005))
    max_a = int(rh * rw * 0.028)
    raw: List[Tuple[int, int, int, int]] = []

    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        area = bw * bh
        if area < min_a or area > max_a:
            continue
        if bh < rh * 0.1 or bw < 6:
            continue
        pad = max(6, int(max(bw, bh) * 0.45))
        tx = max(0, x - pad)
        tw = min(rw - tx, bw + 2 * pad)
        raw.append((tx, ty, tw, th))

    raw.sort(key=lambda b: b[0])
    merged: List[Tuple[int, int, int, int]] = []
    for box in raw:
        if not merged:
            merged.append(box)
            continue
        px, py, pw, ph = merged[-1]
        bx, by, bw, bh = box
        if bx < px + pw + int(rw * 0.06):
            merged[-1] = (
                min(px, bx),
                min(py, by),
                max(px + pw, bx + bw) - min(px, bx),
                max(ph, bh),
            )
        else:
            merged.append(box)

    results: List[Dict] = []
    for i, (tx, ty, tw, th) in enumerate(merged):
        if _region_edge_density(screenshot_gray, tx, tx + tw, y0, y1) < TOP_SLOT_EMPTY_EDGE:
            continue
        comp = _make_component(
            tx, ty, tw, th, f"顶栏{i + 1}",
            category="navigation", component_type="tab_item", confidence=0.88,
        )
        comp["shared_region"] = "top_header"
        results.append(comp)
    return results


def _filter_corner_toolbar_icons(
    icons: List[Dict],
    strip_tabs: List[Dict],
    img_w: int,
    img_h: int,
) -> List[Dict]:
    """仅保留顶栏左右角标，且不与骨架条带槽重复。"""
    out: List[Dict] = []
    for ic in icons:
        cx = ic["x"] + ic["w"] / 2
        if cx > img_w * 0.22 and cx < img_w * 0.78:
            continue
        dup = False
        for t in strip_tabs:
            if _is_duplicate_nav_box(
                (ic["x"], ic["y"], ic["w"], ic["h"]),
                (t["x"], t["y"], t["w"], t["h"]),
                img_h,
            ):
                dup = True
                break
        if not dup:
            out.append(ic)
    return out


def _nav_tabs_from_skeleton_strip(
    mask: np.ndarray,
    y0_ratio: float,
    y1_ratio: float,
    region_key: str,
    label_prefix: str,
    screenshot_gray: Optional[np.ndarray] = None,
) -> List[Dict]:
    """通用：骨架条带内按白色列分布切导航槽，标签为 顶栏N/底栏N，不假定 Tab 个数与文案。"""
    h, w = mask.shape[:2]
    y0, y1 = int(h * y0_ratio), int(h * y1_ratio)
    if y1 <= y0 + 8:
        return []

    strip = mask[y0:y1, :]
    if strip.ndim == 3:
        strip = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(strip, 127, 255, cv2.THRESH_BINARY)

    row_ok = np.where(np.max(binary, axis=1) > 127)[0]
    if len(row_ok) == 0:
        return []

    ry0, ry1 = int(row_ok[0]), int(row_ok[-1]) + 1
    active = binary[ry0:ry1, :]
    ch, cw = active.shape[:2]
    if ch < 8 or cw < 40:
        return []
    if ch > h * 0.14:
        return []

    if region_key == "bottom_tab":
        col_src = active[max(0, ch - max(10, int(ch * 0.55))) :, :]
    else:
        col_src = active[: max(8, int(ch * 0.72)), :]
    col_fill = np.sum(col_src > 127, axis=0).astype(np.float32) / max(col_src.shape[0], 1)
    min_seg_w = max(14, int(cw * (0.04 if region_key == "top_header" else 0.05)))
    segments = _column_segments_from_fill(col_fill, cw, min_seg_w, MAX_NAV_SLOTS)
    if len(segments) < 1:
        return []

    if region_key == "bottom_tab":
        tab_ty, tab_th = _bottom_tab_geometry(h)
        x_band = (int(h * BOTTOM_TAB_X_BAND[0]), int(h * BOTTOM_TAB_X_BAND[1]))
    else:
        pad_y = max(2, int(ch * 0.1))
        tab_ty = y0 + ry0 + pad_y
        tab_th = min(max(22, ch - 2 * pad_y), int(h * MAX_TAB_HEIGHT_RATIO))
        x_band = (y0, y1)

    results: List[Dict] = []
    slot_idx = 0
    for sx, ex in segments:
        if region_key == "top_header" and _is_empty_top_slot(
            active, sx, ex, screenshot_gray, y0, y1, global_x=0
        ):
            continue
        pad_x = max(2, int((ex - sx) * 0.08))
        tx = sx + pad_x
        tw = max(20, (ex - sx) - 2 * pad_x)
        ty, th = tab_ty, tab_th
        tight = _tighten_to_white(mask, tx, ty, tw, th, y_band=x_band)
        if tight:
            tx, _, tw, _ = tight
            ty, th = tab_ty, tab_th
        tx, ty, tw, th = _clamp_tab_box(tx, ty, tw, th, h, region_key)
        slot_idx += 1
        comp = _make_component(
            tx, ty, tw, th,
            f"{label_prefix}{slot_idx}",
            category="navigation",
            component_type="tab_item",
            confidence=0.9,
        )
        comp["shared_region"] = region_key
        results.append(comp)
    return results


def _scale_components(
    comps: List[Dict],
    src_w: int,
    src_h: int,
    dst_w: int,
    dst_h: int,
) -> List[Dict]:
    """将检测坐标从蒙版像素空间缩放到页面 naturalSize。"""
    if not comps or src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return comps
    if src_w == dst_w and src_h == dst_h:
        return comps
    sx = dst_w / src_w
    sy = dst_h / src_h
    scaled: List[Dict] = []
    for c in comps:
        nc = dict(c)
        nc["x"] = int(round(c["x"] * sx))
        nc["y"] = int(round(c["y"] * sy))
        nc["w"] = max(8, int(round(c["w"] * sx)))
        nc["h"] = max(8, int(round(c["h"] * sy)))
        scaled.append(nc)
    return scaled


def _prepare_skeleton_binary(gray: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """二值化骨架；若白像素过半则视为反色蒙版并纠正。"""
    g = gray
    _, binary = cv2.threshold(g, 127, 255, cv2.THRESH_BINARY)
    if float(np.count_nonzero(binary)) / max(binary.size, 1) > 0.55:
        binary = cv2.bitwise_not(binary)
        g = binary
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
    return g, binary


def _region_key_from_y(cy: float, img_h: int) -> str:
    if cy >= img_h * BOTTOM_STRIP_Y[0]:
        return "bottom_tab"
    if cy <= img_h * TOP_STRIP_Y[1]:
        return "top_header"
    return "content"


def _split_white_blob_columns(
    binary: np.ndarray,
    x: int,
    y: int,
    bw: int,
    bh: int,
    max_slots: int = MAX_NAV_SLOTS,
) -> List[Tuple[int, int, int, int]]:
    """宽白条（底栏/顶栏）在蒙版内按列切成多个热区。"""
    crop = binary[y : y + bh, x : x + bw]
    if crop.size == 0:
        return []
    col_fill = np.sum(crop > 127, axis=0).astype(np.float32) / max(crop.shape[0], 1)
    min_seg = max(12, int(crop.shape[1] * 0.04))
    segments = _column_segments_from_fill(col_fill, crop.shape[1], min_seg, max_slots)
    if len(segments) < 2 and crop.shape[1] >= max(80, int(bw * 0.35)):
        est = min(max_slots, max(2, round(crop.shape[1] / max(64, crop.shape[1] * 0.24))))
        slot_w = max(min_seg, crop.shape[1] // est)
        segments = []
        sx = 0
        for i in range(est):
            ex = crop.shape[1] if i == est - 1 else min(crop.shape[1], sx + slot_w)
            if ex - sx >= min_seg:
                segments.append((sx, ex))
            sx = ex
    if len(segments) < 2:
        return [(x, y, bw, bh)]
    boxes: List[Tuple[int, int, int, int]] = []
    for sx, ex in segments:
        sub = crop[:, sx:ex]
        rows = np.where(np.max(sub > 127, axis=1))[0]
        if len(rows) == 0:
            continue
        ty0, ty1 = int(rows[0]), int(rows[-1]) + 1
        boxes.append((x + sx, y + ty0, ex - sx, ty1 - ty0))
    return boxes if boxes else [(x, y, bw, bh)]


def _detect_hotspots_from_skeleton_white(
    mask: np.ndarray,
    screenshot_gray: Optional[np.ndarray] = None,
    system_bars: Optional[Dict] = None,
    max_items: int = 48,
) -> List[Dict]:
    """
    以骨架蒙版白色连通域为唯一识别来源：白块外接框 = 热区。
    宽条（顶/底导航）仅在白条内部按列切分，不做跨 Tab 合并。
    """
    if mask is None or mask.size == 0:
        return []

    gray = cv2.cvtColor(mask, cv2.COLOR_BGR2GRAY) if mask.ndim == 3 else mask
    h, w = gray.shape[:2]
    gray, binary = _prepare_skeleton_binary(gray)

    y_content0, y_content1 = _content_bounds(h, system_bars)
    screen_area = h * w
    min_area = max(120, int(screen_area * 0.00025))
    max_area = int(screen_area * 0.12)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    raw_boxes: List[Tuple[int, int, int, int, str]] = []

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue
        x, y, bw, bh = cv2.boundingRect(cnt)
        if bw < 12 or bh < 10:
            continue
        cy = y + bh / 2
        region = _region_key_from_y(cy, h)

        if region == "content" and (y + bh < y_content0 or y > y_content1):
            continue
        if area > max_area and region == "content":
            parts = (
                _split_mask_blob_by_rows(binary, x, y, bw, bh, 0)
                if bw > w * 0.45
                else []
            )
            if parts:
                for px, py, pw, ph in parts[:24]:
                    raw_boxes.append((px, py, pw, ph, region))
                continue

        is_nav_bar = region in ("top_header", "bottom_tab") and bw > w * 0.28 and bh < h * 0.14
        if is_nav_bar:
            for bx, by, bw2, bh2 in _split_white_blob_columns(binary, x, y, bw, bh):
                raw_boxes.append((bx, by, bw2, bh2, region))
        else:
            if bw > w * 0.72 and bh > h * 0.35:
                continue
            raw_boxes.append((x, y, bw, bh, region))

    results: List[Dict] = []
    for x, y, bw, bh, region in raw_boxes:
        tight = _tighten_to_white(gray, x, y, bw, bh)
        if tight:
            x, y, bw, bh = tight
        elif bw < 12 or bh < 10:
            continue
        if region == "top_header" and screenshot_gray is not None:
            crop = binary[y : y + bh, x : x + bw]
            if crop.size and _is_empty_top_slot(
                crop,
                0,
                crop.shape[1],
                screenshot_gray,
                int(h * TOP_STRIP_Y[0]),
                int(h * TOP_STRIP_Y[1]),
                global_x=x,
            ):
                continue

        ctype = "tab_item" if region in ("top_header", "bottom_tab") else "repeat_card"
        comp = _make_component(
            x, y, bw, bh, "热区",
            category="navigation" if ctype == "tab_item" else "action",
            component_type=ctype,
            confidence=0.88,
        )
        comp["shared_region"] = region if region != "content" else ""
        results.append(comp)
        if len(results) >= max_items:
            break

    nav = _dedupe_nav_items(
        [c for c in results if c.get("component_type") == "tab_item"], h,
    )
    rest = [c for c in results if c.get("component_type") != "tab_item"]
    nav.sort(key=lambda c: (c.get("shared_region", ""), c["x"]))
    ti = bi = 0
    for c in nav:
        if c.get("shared_region") == "top_header":
            ti += 1
            c["label"] = f"顶栏{ti}"
        elif c.get("shared_region") == "bottom_tab":
            bi += 1
            c["label"] = f"底栏{bi}"
    rest.sort(key=lambda c: (c["y"], c["x"]))
    for i, c in enumerate(rest, 1):
        c["label"] = f"区域{i}"
    results = nav + rest

    if not results:
        fallback: List[Dict] = []
        fallback.extend(
            _nav_tabs_from_skeleton_strip(
                gray, TOP_STRIP_Y[0], TOP_STRIP_Y[1], "top_header", "顶栏", screenshot_gray,
            )
        )
        fallback.extend(
            _nav_tabs_from_skeleton_strip(
                gray, BOTTOM_STRIP_Y[0], BOTTOM_STRIP_Y[1], "bottom_tab", "底栏", screenshot_gray,
            )
        )
        fallback.extend(_detect_content_regions_from_mask(gray, w, h, system_bars))
        results = fallback

    SLog.i(TAG, f"Skeleton-white hotspots: {len(results)}")
    return results[:max_items]


def _split_mask_blob_by_rows(
    binary_roi: np.ndarray,
    x: int,
    y: int,
    bw: int,
    bh: int,
    y_offset: int,
    min_row_h: int = 28,
) -> List[Tuple[int, int, int, int]]:
    """大块白色蒙版按行谷值拆成多段，避免整页列表只出一个热区。"""
    crop = binary_roi[y : y + bh, x : x + bw]
    if crop.size == 0:
        return []
    row_fill = np.sum(crop > 127, axis=1).astype(np.float32)
    if np.max(row_fill) < 1:
        return []
    valley_thr = float(np.max(row_fill)) * 0.15
    bands: List[Tuple[int, int]] = []
    in_band = False
    start = 0
    for i in range(crop.shape[0]):
        if row_fill[i] >= valley_thr and not in_band:
            start = i
            in_band = True
        elif row_fill[i] < valley_thr and in_band:
            if i - start >= min_row_h:
                bands.append((start, i))
            in_band = False
    if in_band and crop.shape[0] - start >= min_row_h:
        bands.append((start, crop.shape[0]))
    out: List[Tuple[int, int, int, int]] = []
    for r0, r1 in bands[:32]:
        band = crop[r0:r1, :]
        cols = np.where(np.max(band > 127, axis=0))[0]
        if len(cols) < 4:
            continue
        tx0, tx1 = int(cols[0]), int(cols[-1]) + 1
        out.append((x + tx0, y_offset + y + r0, tx1 - tx0, r1 - r0))
    return out


def _detect_content_regions_from_mask(
    mask: np.ndarray,
    img_w: int,
    img_h: int,
    system_bars: Optional[Dict] = None,
    max_items: int = 24,
) -> List[Dict]:
    """仅从骨架内容区白色块产热区，避免对聊天/列表页套用双列截图启发式。"""
    gray = cv2.cvtColor(mask, cv2.COLOR_BGR2GRAY) if mask.ndim == 3 else mask
    h, w = gray.shape[:2]
    y0, y1 = _content_bounds(h, system_bars)
    roi = gray[y0:y1, :]
    _, binary = cv2.threshold(roi, 127, 255, cv2.THRESH_BINARY)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)

    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    screen_area = h * w
    content_h = max(1, y1 - y0)
    boxes: List[Tuple[int, int, int, int]] = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < screen_area * 0.002:
            continue
        x, y, bw, bh = cv2.boundingRect(cnt)
        if bw < 24 or bh < 24:
            continue
        if bw > w * 0.65 and bh > content_h * 0.18:
            boxes.extend(_split_mask_blob_by_rows(binary, x, y, bw, bh, y0))
            continue
        if area > screen_area * MAX_CONTENT_AREA_RATIO:
            continue
        if bw > w * MAX_CONTENT_WIDTH_RATIO or bh > content_h * 0.42:
            continue
        boxes.append((x, y0 + y, bw, bh))

    merged: List[Tuple[int, int, int, int]] = []
    for box in sorted(boxes, key=lambda b: b[2] * b[3], reverse=True):
        if any(_iou(box, m) >= 0.4 for m in merged):
            continue
        merged.append(box)
        if len(merged) >= max_items:
            break

    results: List[Dict] = []
    for i, (x, y, bw, bh) in enumerate(merged):
        tight = _tighten_to_white(gray, x, y, bw, bh)
        if tight:
            x, y, bw, bh = tight
        results.append(
            _make_component(
                x, y, bw, bh, f"区域{i + 1}",
                category="action", component_type="repeat_card", confidence=0.75,
            )
        )
    return results


def _page_allowed_shared_regions(
    node_id: Optional[str],
    shared_components: Optional[List[Dict]],
) -> Optional[set]:
    """
    返回当前页面允许展示的共用区域集合。
    None 表示未配置共用组件，按本页骨架白色正常识别。
    """
    if not shared_components:
        return None
    if not node_id:
        return set()
    allowed: set = set()
    for sc in shared_components:
        members = sc.get("members") or []
        if any(m.get("node_id") == node_id for m in members):
            if sc.get("region"):
                allowed.add(sc["region"])
    return allowed


def _filter_shared_for_page(
    comps: List[Dict],
    node_id: Optional[str],
    shared_components: Optional[List[Dict]],
) -> List[Dict]:
    """仅过滤图谱「共用组件」登记项；本页骨架识别的 Tab 不受成员限制。"""
    if not shared_components:
        return comps
    allowed = _page_allowed_shared_regions(node_id, shared_components)
    if allowed is None:
        return comps
    return [
        c for c in comps
        if not c.get("shared_component_uid")
        or c.get("shared_region") in allowed
    ]


def _constrain_all_to_mask(mask: Optional[np.ndarray], comps: List[Dict]) -> List[Dict]:
    """用热区与骨架白色区域的交集收紧框；无白色重叠的导航/卡片丢弃。"""
    if mask is None:
        return comps
    out: List[Dict] = []
    for c in comps:
        c = dict(c)
        ct = c.get("component_type", "")
        y_band = None
        region = c.get("shared_region")
        if ct == "tab_item" and region == "bottom_tab":
            mh = mask.shape[0]
            y_band = (int(mh * BOTTOM_TAB_X_BAND[0]), int(mh * BOTTOM_TAB_X_BAND[1]))
        elif ct == "tab_item" and region == "top_header":
            y_band = (int(mask.shape[0] * TOP_STRIP_Y[0]), int(mask.shape[0] * TOP_STRIP_Y[1]))
        tight = _tighten_to_white(mask, c["x"], c["y"], c["w"], c["h"], y_band=y_band)
        if tight:
            c["x"], c["y"], c["w"], c["h"] = tight
            if ct == "tab_item" and region:
                c["x"], c["y"], c["w"], c["h"] = _clamp_tab_box(
                    c["x"], c["y"], c["w"], c["h"], mask.shape[0], region
                )
        elif ct in ("tab_item", "repeat_card", "fab"):
            continue
        out.append(c)
    return out


def _detect_app_chrome(
    gray: np.ndarray,
    system_bars: Optional[Dict] = None,
) -> List[Dict]:
    """已弃用固定比例 Tab；保留空实现，导航改由骨架白色区域驱动。"""
    del gray, system_bars
    return []


def _tabs_from_strip(
    crop: np.ndarray,
    x_offset: int,
    y_offset: int,
    labels: List[str],
    region_key: str,
    min_slots: int = 2,
) -> List[Dict]:
    del crop, x_offset, min_slots
    n = len(labels)
    if n == 0:
        return []
    slots = [(i / n, 1.0 / n, labels[i]) for i in range(n)]
    return _tabs_from_layout(
        400, 800, 0, 0.08, slots, region_key, y_min=y_offset, y_max=y_offset + 64
    )


def _split_by_vertical_peaks(
    gray_crop: np.ndarray,
    x: int,
    y: int,
    min_slots: int = 2,
    max_slots: int = 6,
    default_prefix: str = "项",
) -> List[Dict]:
    h, w = gray_crop.shape[:2]
    if w < 40 or h < 8:
        return []

    edges = cv2.Canny(gray_crop, 30, 100)
    col_energy = np.sum(edges, axis=0).astype(np.float32)
    kernel = max(3, w // 40)
    smooth = np.convolve(col_energy, np.ones(kernel) / kernel, mode="same")
    threshold = float(np.max(smooth)) * 0.2
    peaks: List[int] = []
    min_dist = max(12, int(w * 0.06))
    for i in range(1, len(smooth) - 1):
        if smooth[i] >= threshold and smooth[i] >= smooth[i - 1] and smooth[i] >= smooth[i + 1]:
            if not peaks or i - peaks[-1] >= min_dist:
                peaks.append(i)

    if len(peaks) < min_slots:
        slot_count = min(max_slots, max(min_slots, w // max(48, w // 5)))
        slot_w = w // slot_count
        peaks = [slot_w // 2 + i * slot_w for i in range(slot_count)]

    slots: List[Dict] = []
    slot_w = max(int(w / len(peaks)), 20)
    for idx, px in enumerate(peaks[:max_slots]):
        tx = x + max(0, px - slot_w // 2)
        tw = min(slot_w, x + w - tx)
        slots.append(
            _make_component(
                tx, y, tw, h,
                f"{default_prefix}{idx + 1}",
                category="navigation",
                component_type="tab_item",
                confidence=0.72,
            )
        )
    return slots


def _split_bottom_tabs(mask: np.ndarray, nav_rect: Dict) -> List[Dict]:
    del nav_rect
    if mask is None:
        return []
    gray = cv2.cvtColor(mask, cv2.COLOR_BGR2GRAY) if mask.ndim == 3 else mask
    return _nav_tabs_from_skeleton_strip(
        gray, BOTTOM_STRIP_Y[0], BOTTOM_STRIP_Y[1], "bottom_tab", "底栏",
    )


def _split_top_header(gray: np.ndarray, rect: Dict) -> List[Dict]:
    del rect
    return _nav_tabs_from_skeleton_strip(
        gray, TOP_STRIP_Y[0], TOP_STRIP_Y[1], "top_header", "顶栏",
    )


def _split_box_to_columns(x: int, y: int, w: int, h: int, img_w: int, cols: int = 2) -> List[Tuple[int, int, int, int]]:
    if w < img_w * 0.55 or cols < 2:
        return [(x, y, w, h)]
    gap = max(4, int(w * 0.02))
    col_w = (w - gap * (cols - 1)) // cols
    out: List[Tuple[int, int, int, int]] = []
    for i in range(cols):
        cx = x + i * (col_w + gap)
        out.append((cx, y, col_w, h))
    return out


def _refine_row_splits(gray_roi: np.ndarray, y_offset: int, min_row_h: int) -> List[Tuple[int, int]]:
    rh = gray_roi.shape[0]
    edges = cv2.Canny(gray_roi, 30, 100)
    row_energy = np.sum(edges, axis=1).astype(np.float32)
    kernel = max(3, rh // 80)
    row_smooth = np.convolve(row_energy, np.ones(kernel) / kernel, mode="same")
    valley_thr = float(np.percentile(row_smooth, 22))

    rows: List[Tuple[int, int]] = []
    in_block = False
    start = 0
    for i in range(rh):
        is_gap = row_smooth[i] < valley_thr
        if is_gap and in_block:
            if i - start >= min_row_h:
                rows.append((start, i))
            in_block = False
        elif not is_gap and not in_block:
            start = i
            in_block = True
    if in_block and rh - start >= min_row_h:
        rows.append((start, rh))
    return rows


def _tighten_box(gray: np.ndarray, x: int, y: int, w: int, h: int, pad: int = 3) -> Tuple[int, int, int, int]:
    """在候选框内收紧到实际内容边界。"""
    H, W = gray.shape[:2]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(W, x + w), min(H, y + h)
    crop = gray[y1:y2, x1:x2]
    if crop.size == 0:
        return x, y, w, h

    gx = cv2.Sobel(crop, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(crop, cv2.CV_32F, 0, 1, ksize=3)
    mag = cv2.magnitude(gx, gy)
    mask = mag > float(np.percentile(mag, 55))
    if not np.any(mask):
        return x, y, w, h

    ys, xs = np.where(mask)
    ty0, ty1 = int(ys.min()), int(ys.max()) + 1
    tx0, tx1 = int(xs.min()), int(xs.max()) + 1
    nx = max(0, x1 + tx0 - pad)
    ny = max(0, y1 + ty0 - pad)
    nw = min(W - nx, (tx1 - tx0) + 2 * pad)
    nh = min(H - ny, (ty1 - ty0) + 2 * pad)
    if nw < 8 or nh < 8:
        return x, y, w, h
    return (nx, ny, nw, nh)


def _content_bounds(img_h: int, system_bars: Optional[Dict]) -> Tuple[int, int]:
    bars = system_bars or {}
    top = int(bars.get("top") or img_h * 0.11)
    bottom = int(bars.get("bottom") or img_h * 0.14)
    y0 = max(0, min(top, int(img_h * 0.2)))
    y1 = max(y0 + 40, img_h - max(0, bottom))
    # 内容区严格避开顶/底 Tab 条带
    y0 = max(y0, int(img_h * TOP_STRIP_Y[1]) + 4)
    y1 = min(y1, int(img_h * BOTTOM_STRIP_Y[0]) - 4)
    return y0, y1


def _is_valid_tab_box(c: Dict, img_w: int, img_h: int) -> bool:
    if c.get("component_type") != "tab_item":
        return True
    w, h = int(c.get("w") or 0), int(c.get("h") or 0)
    x, y = int(c.get("x") or 0), int(c.get("y") or 0)
    if h > img_h * MAX_TAB_HEIGHT_RATIO or w > img_w * 0.34 or h < 22 or w < 28:
        return False
    region = c.get("shared_region")
    cy = y + h / 2
    if region == "bottom_tab":
        ty, th = _bottom_tab_geometry(img_h)
        if y < img_h * 0.91 or y + h > img_h - 2:
            return False
        if abs(y - ty) > img_h * 0.015 or abs(h - th) > img_h * 0.02:
            return False
    if region == "top_header":
        if y > img_h * TOP_STRIP_Y[1] + 4 or cy > img_h * 0.135:
            return False
        if cy < img_h * (TOP_STRIP_Y[0] - 0.008):
            return False
    return True


def _detect_fine_grid_cards(
    gray: np.ndarray,
    y0: int,
    y1: int,
    img_w: int,
    min_repeat: int = 2,
) -> List[Tuple[int, int, int, int]]:
    """双列信息流：固定左右列 + 行切分，避免跨列大块。"""
    roi = gray[y0:y1, :]
    rh, rw = roi.shape[:2]
    if rh < 60 or rw < 60:
        return []

    margin = max(4, int(rw * 0.015))
    gutter = max(6, int(rw * 0.02))
    col_w = (rw - 2 * margin - gutter) // 2
    col_x = [margin, margin + col_w + gutter]

    min_row_h = max(32, int(rh * 0.04))
    rows = _refine_row_splits(roi, y0, min_row_h)

    boxes: List[Tuple[int, int, int, int]] = []
    for cx in col_x:
        for r0, r1 in rows:
            bh = r1 - r0
            if bh < min_row_h or bh > rh * 0.28:
                continue
            boxes.append((cx, y0 + r0, col_w, bh))

    return boxes if len(boxes) >= min_repeat else []


def _detect_horizontal_menu_rows(
    gray: np.ndarray,
    y0: int,
    y1: int,
    img_w: int,
    img_h: int,
) -> List[Tuple[int, int, int, int]]:
    """个人页等：横向菜单条（编辑资料 / 创作历史 等）拆成多个小项。"""
    roi = gray[y0:y1, :]
    rh, rw = roi.shape[:2]
    boxes: List[Tuple[int, int, int, int]] = []

    row_h_lo, row_h_hi = int(img_h * 0.04), int(img_h * 0.11)
    edges = cv2.Canny(roi, 30, 90)

    row_energy = np.sum(edges, axis=1)
    kernel = max(3, rh // 60)
    row_smooth = np.convolve(row_energy.astype(np.float32), np.ones(kernel) / kernel, mode="same")

    bands: List[Tuple[int, int]] = []
    in_band = False
    start = 0
    for i in range(rh):
        active = row_smooth[i] > float(np.percentile(row_smooth, 60))
        if active and not in_band:
            start = i
            in_band = True
        elif not active and in_band:
            bh = i - start
            if row_h_lo <= bh <= row_h_hi:
                bands.append((start, i))
            in_band = False
    if in_band:
        bh = rh - start
        if row_h_lo <= bh <= row_h_hi:
            bands.append((start, rh))

    for r0, r1 in bands:
        band = roi[r0:r1, :]
        col_energy = np.sum(cv2.Canny(band, 30, 90), axis=0).astype(np.float32)
        ck = max(3, rw // 50)
        col_smooth = np.convolve(col_energy, np.ones(ck) / ck, mode="same")
        thr = float(np.max(col_smooth)) * 0.25
        peaks: List[int] = []
        min_dist = max(20, int(rw * 0.14))
        for i in range(1, len(col_smooth) - 1):
            if col_smooth[i] >= thr and col_smooth[i] >= col_smooth[i - 1] and col_smooth[i] >= col_smooth[i + 1]:
                if not peaks or i - peaks[-1] >= min_dist:
                    peaks.append(i)

        slot_count = len(peaks) if len(peaks) >= 2 else 3
        slot_w = max(int(rw / slot_count), int(rw * 0.22))
        for idx, px in enumerate((peaks if len(peaks) >= 2 else [rw // 6, rw // 2, rw * 5 // 6])[:5]):
            tx = max(0, px - slot_w // 2)
            tw = min(slot_w, rw - tx)
            if tw >= rw * 0.18:
                boxes.append((tx, y0 + r0, tw, r1 - r0))

    return boxes


def _detect_small_fab_like(gray: np.ndarray, y0: int, y1: int, img_w: int, img_h: int) -> List[Tuple[int, int, int, int]]:
    """悬浮按钮等小方块（如「开始造物」）。"""
    roi = gray[y0:y1, :]
    edges = cv2.Canny(roi, 40, 120)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes: List[Tuple[int, int, int, int]] = []
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        area = bw * bh
        if area < img_w * img_h * 0.002 or area > img_w * img_h * 0.04:
            continue
        ar = bw / max(bh, 1)
        if 0.65 <= ar <= 1.5 and bw >= img_w * 0.18 and bw <= img_w * 0.55:
            boxes.append((x, y0 + y, bw, bh))
    return boxes


def _detect_repeated_regions(
    gray: np.ndarray,
    system_bars: Optional[Dict] = None,
    min_repeat: int = 2,
    max_items: int = 48,
) -> List[Dict]:
    if gray is None or gray.size == 0:
        return []

    h, w = gray.shape[:2]
    y0, y1 = _content_bounds(h, system_bars)

    raw_boxes: List[Tuple[int, int, int, int]] = []
    raw_boxes.extend(_detect_fine_grid_cards(gray, y0, y1, w, min_repeat=min_repeat))
    raw_boxes.extend(_detect_horizontal_menu_rows(gray, y0, y1, w, h))
    raw_boxes.extend(_detect_small_fab_like(gray, y0, y1, w, h))

    # 宽条拆成左右两卡
    expanded: List[Tuple[int, int, int, int]] = []
    for x, y, bw, bh in raw_boxes:
        if bw >= w * 0.55:
            expanded.extend(_split_box_to_columns(x, y, bw, bh, w, cols=2))
        else:
            expanded.append((x, y, bw, bh))

    merged_boxes: List[Tuple[int, int, int, int]] = []
    for box in expanded:
        if box[2] > w * MAX_CONTENT_WIDTH_RATIO or box[3] > h * MAX_CONTENT_HEIGHT_RATIO:
            continue
        if box[2] * box[3] < w * h * MIN_CARD_AREA_RATIO:
            continue
        dup = False
        for m in merged_boxes:
            if _iou(box, m) >= 0.42:
                dup = True
                break
        if not dup:
            merged_boxes.append(box)

    if len(merged_boxes) < 1:
        return []

    merged_boxes.sort(key=lambda b: (b[1], b[0]))
    merged_boxes = merged_boxes[:max_items]

    results: List[Dict] = []
    for i, (x, y, bw, bh) in enumerate(merged_boxes):
        results.append(
            _make_component(
                x, y, bw, bh,
                f"卡片 {i + 1}",
                category="action",
                component_type="repeat_card",
                confidence=min(0.9, 0.65 + i * 0.005),
            )
        )

    SLog.i(TAG, f"Detected {len(results)} fine-grained region(s) from screenshot")
    return results


class ComponentDetector:
    @staticmethod
    def detect_from_mask(
        mask: np.ndarray,
        img_w: Optional[int] = None,
        img_h: Optional[int] = None,
        system_bars: Optional[Dict] = None,
        split_bottom_tabs: bool = True,
    ) -> List[Dict]:
        """骨架只产出导航级小组件，不产出大块「内容区块」。"""
        if mask is None or mask.size == 0:
            return []

        gray = cv2.cvtColor(mask, cv2.COLOR_BGR2GRAY) if mask.ndim == 3 else mask
        h, w = gray.shape[:2]
        img_w = img_w or w
        img_h = img_h or h
        screen_area = h * w
        min_area = max(200, int(screen_area * 0.0004))
        max_area = int(screen_area * 0.06)

        _, binary = cv2.threshold(gray, 127, 255, cv2.THRESH_BINARY)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)

        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: List[Dict] = []

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < min_area or area > max_area:
                continue
            x, y, bw, bh = cv2.boundingRect(cnt)
            if bw < 10 or bh < 8:
                continue

            cy = y + bh / 2
            width_ratio = bw / max(w, 1)
            height_ratio = bh / max(h, 1)

            if cy >= h * 0.82 and width_ratio >= 0.5 and height_ratio <= 0.14:
                continue
            if cy <= h * 0.18 and width_ratio >= 0.4 and height_ratio <= 0.12:
                continue
            elif 0.65 <= bw / max(bh, 1) <= 1.4 and 0.015 <= area / screen_area <= 0.035:
                comp_type = "fab"
                label, category = "悬浮按钮", "action"
            else:
                continue

            candidates.append(
                _make_component(
                    x, y, bw, bh, label,
                    category=category,
                    component_type=comp_type,
                    confidence=0.78,
                )
            )

        merged = _merge_rects(candidates)
        final: List[Dict] = list(merged)
        final.sort(key=lambda c: (c["y"], c["x"]))
        SLog.i(TAG, f"Detected {len(final)} fine component(s) from skeleton mask")
        return final

    @staticmethod
    def detect_repeated_from_image(
        image: np.ndarray,
        system_bars: Optional[Dict] = None,
        min_repeat: int = 2,
    ) -> List[Dict]:
        if image is None or image.size == 0:
            return []
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        return _detect_repeated_regions(gray, system_bars, min_repeat=min_repeat)

    @staticmethod
    def detect_repeated_from_image_file(
        image_path: str,
        system_bars: Optional[Dict] = None,
    ) -> List[Dict]:
        from server.core.vision.skeleton_algo import SkeletonAlgo

        img = SkeletonAlgo._fetch_remote_image(image_path)
        if img is None:
            return []
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        bars = system_bars or SkeletonAlgo.detect_system_bars(gray)
        return _detect_repeated_regions(gray, bars)

    @staticmethod
    def detect_for_page(
        mask_path: Optional[str] = None,
        screenshot_path: Optional[str] = None,
        img_w: Optional[int] = None,
        img_h: Optional[int] = None,
        system_bars: Optional[Dict] = None,
        mask: Optional[np.ndarray] = None,
        node_id: Optional[str] = None,
        shared_components: Optional[List[Dict]] = None,
    ) -> List[Dict]:
        """骨架白色约束热区 + 内容区；共用 Tab 仅在本页为成员时返回。"""
        from server.core.vision.skeleton_algo import SkeletonAlgo

        combined: List[Dict] = []
        mask_gray = None
        h = img_h or 800
        w = img_w or 400

        if mask is not None:
            mask_gray = cv2.cvtColor(mask, cv2.COLOR_BGR2GRAY) if mask.ndim == 3 else mask
            h, w = mask_gray.shape[:2]
        elif mask_path:
            mask_gray = SkeletonAlgo._fetch_remote_image(mask_path)
            if mask_gray is not None:
                h, w = mask_gray.shape[:2]

        shot_gray = None
        if screenshot_path:
            img = SkeletonAlgo._fetch_remote_image(screenshot_path)
            if img is not None:
                shot_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
                h, w = shot_gray.shape[:2]

        bars = system_bars
        if shot_gray is not None and not bars:
            bars = SkeletonAlgo.detect_system_bars(shot_gray)

        src_w, src_h = w, h
        if mask_gray is not None:
            combined = _detect_hotspots_from_skeleton_white(
                mask_gray, shot_gray, bars, max_items=48,
            )

        if not combined:
            return []

        dst_w = int(img_w) if img_w else src_w
        dst_h = int(img_h) if img_h else src_h
        if shot_gray is not None and (not img_w or not img_h):
            dst_h, dst_w = shot_gray.shape[:2]
        if dst_w > 0 and dst_h > 0 and (dst_w, dst_h) != (src_w, src_h):
            combined = _scale_components(combined, src_w, src_h, dst_w, dst_h)

        combined = _filter_shared_for_page(combined, node_id, shared_components)
        merged = _merge_rects(combined, iou_threshold=0.38)
        merged.sort(key=lambda c: (c["y"], c["x"]))
        return merged

    @staticmethod
    def detect_from_mask_file(
        mask_path: str,
        img_w: Optional[int] = None,
        img_h: Optional[int] = None,
        system_bars: Optional[Dict] = None,
    ) -> List[Dict]:
        from server.core.vision.skeleton_algo import SkeletonAlgo

        gray = SkeletonAlgo._fetch_remote_image(mask_path)
        if gray is None:
            return []
        return ComponentDetector.detect_from_mask(gray, img_w, img_h, system_bars)
