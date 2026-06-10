# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""通用开关控件定位：复选框 / 单选框 / Switch（任意页面）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from script.log import SLog

TAG = "ToggleLocate"

_CHINESE_NUM = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


@dataclass
class ToggleIntent:
    kind: str  # checkbox | radio | switch
    anchor: str
    index: Optional[int]
    wants_checked: bool


_TOGGLE_CLIP: Dict[str, Tuple[str, List[str]]] = {
    "checkbox": (
        "empty checkbox",
        ["unchecked checkbox", "round checkbox", "圆形勾选框", "checkbox circle"],
    ),
    "radio": (
        "empty radio button",
        ["unchecked radio button", "圆形单选按钮", "radio button"],
    ),
    "switch": (
        "toggle switch off",
        ["switch off", "toggle off", "开关关闭"],
    ),
}


def is_toggle_intent(label: str) -> bool:
    return parse_toggle_intent(label) is not None


def parse_toggle_intent(label: str) -> Optional[ToggleIntent]:
    raw = (label or "").strip()
    if not raw:
        return None

    if re.search(r"单选|radio", raw, re.I):
        kind = "radio"
    elif re.search(r"开关|switch", raw, re.I):
        kind = "switch"
    elif re.search(r"勾选|勾上|checkbox|checkable|复选|选中|取消勾", raw, re.I):
        kind = "checkbox"
    else:
        return None

    wants_checked = not re.search(r"取消|去掉|不勾|uncheck", raw, re.I)

    quoted = re.search(r"[「『\"']([^」』\"']+)[」』\"']", raw)
    if quoted:
        anchor = quoted.group(1).strip()
    else:
        anchor = re.sub(
            r"^(勾选|勾上|选中|点击|点一下|tap|click|选择|单选)\s*",
            "",
            raw,
            flags=re.I,
        ).strip()
    anchor = re.sub(
        r"(的)?(勾选框|复选框|单选框|选择框|checkbox|radio|switch)\s*$",
        "",
        anchor,
        flags=re.I,
    ).strip("「」『』【】\"' \t")
    if anchor in ("", raw) and re.fullmatch(r".*(勾选框|复选框|单选框).*", raw):
        anchor = ""

    index: Optional[int] = None
    m = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*[项个]", raw)
    if m:
        token = m.group(1)
        if token.isdigit():
            index = max(0, int(token) - 1)
        elif token in _CHINESE_NUM:
            index = _CHINESE_NUM[token] - 1

    return ToggleIntent(kind=kind, anchor=anchor, index=index, wants_checked=wants_checked)


def toggle_clip_queries(intent: ToggleIntent) -> Tuple[str, List[str]]:
    base, aliases = _TOGGLE_CLIP.get(intent.kind, _TOGGLE_CLIP["checkbox"])
    extras = list(aliases)
    if intent.anchor:
        extras.append(intent.anchor)
    return base, extras


def _node_bounds(info: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    b = info.get("bounds") or {}
    if not b:
        return None
    x1, y1 = int(b.get("left", 0)), int(b.get("top", 0))
    x2, y2 = int(b.get("right", 0)), int(b.get("bottom", 0))
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def _is_small_toggle(x1: int, y1: int, x2: int, y2: int, screen_w: int, screen_h: int) -> bool:
    w, h = x2 - x1, y2 - y1
    if w < 8 or h < 8:
        return False
    if w > screen_w * 0.15 or h > screen_h * 0.08:
        return False
    if w * h > screen_w * screen_h * 0.025:
        return False
    return True


def discover_toggle_candidates(
    engine,
    screen_w: int,
    screen_h: int,
) -> List[Dict[str, Any]]:
    """从无障碍树收集复选框/单选/开关等小控件候选（全页面通用）。"""
    d = engine._ensure_u2() if hasattr(engine, "_ensure_u2") else None
    if not d:
        return []

    y_top = int(screen_h * 0.06)
    y_bottom = int(screen_h * 0.96)
    out: List[Dict[str, Any]] = []
    seen: set = set()

    def add_candidate(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        source: str,
        checked: bool = False,
        class_name: str = "",
        text: str = "",
    ) -> None:
        if not _is_small_toggle(x1, y1, x2, y2, screen_w, screen_h):
            return
        cy = (y1 + y2) // 2
        if cy < y_top or cy > y_bottom:
            return
        key = (x1 // 6, y1 // 6, x2 // 6, y2 // 6)
        if key in seen:
            return
        seen.add(key)
        w, h = x2 - x1, y2 - y1
        out.append(
            {
                "x": x1,
                "y": y1,
                "w": w,
                "h": h,
                "label": (text or class_name or source).strip(),
                "source": source,
                "checked": checked,
                "class_name": class_name,
            }
        )

    specs = (
        ("u2_checkbox", "android.widget.CheckBox"),
        ("u2_radio", "android.widget.RadioButton"),
        ("u2_switch", "android.widget.Switch"),
        ("u2_toggle", "android.widget.ToggleButton"),
    )
    try:
        for source, cls in specs:
            sel = d(className=cls)
            if not sel.exists(timeout=0.35):
                continue
            count = int(getattr(sel, "count", 1) or 1)
            for idx in range(min(count, 24)):
                node = sel[idx] if count > 1 else sel
                info = node.info or {}
                rect = _node_bounds(info)
                if not rect:
                    continue
                x1, y1, x2, y2 = rect
                add_candidate(
                    x1,
                    y1,
                    x2,
                    y2,
                    source=source,
                    checked=bool(info.get("checked")),
                    class_name=cls,
                    text=(info.get("text") or info.get("content-desc") or "").strip(),
                )

        sel = d(checkable=True)
        if sel.exists(timeout=0.35):
            count = int(getattr(sel, "count", 1) or 1)
            for idx in range(min(count, 32)):
                node = sel[idx] if count > 1 else sel
                info = node.info or {}
                cls = (info.get("className") or "").strip()
                if cls in {s[1] for s in specs}:
                    continue
                rect = _node_bounds(info)
                if not rect:
                    continue
                x1, y1, x2, y2 = rect
                add_candidate(
                    x1,
                    y1,
                    x2,
                    y2,
                    source="u2_checkable",
                    checked=bool(info.get("checked")),
                    class_name=cls,
                    text=(info.get("text") or info.get("content-desc") or "").strip(),
                )
    except Exception as e:
        SLog.w(TAG, f"toggle u2 scan failed: {e}")

    out.sort(key=lambda c: (c["y"] + c["h"] // 2, c["x"]))
    return out


_AGREEMENT_ANCHOR_WORDS = (
    "用户协议",
    "隐私条款",
    "隐私政策",
    "已仔细阅读",
    "阅读并同意",
    "服务协议",
)


def _toggle_search_anchors(raw: str, anchor: str) -> List[str]:
    """把「底部协议勾选框」等口语映射到屏上真实 OCR 文案（如《用户协议》）。"""
    out: List[str] = []
    a = (anchor or "").strip()
    if a and a not in ("底部协议", "底部", "协议勾选框", "底部协议勾选框", "协议"):
        out.append(a)
    if re.search(r"底部|协议|隐私|勾选|条款|同意", raw or ""):
        out.extend(_AGREEMENT_ANCHOR_WORDS)
    return list(dict.fromkeys(out))


def _find_anchor_bounds(
    engine,
    anchor: str,
    screen_w: int,
    screen_h: int,
) -> Optional[Tuple[int, int, int, int]]:
    if not anchor:
        return None
    needles = [anchor]
    if len(anchor) > 4:
        needles.append(anchor[: max(4, len(anchor) // 2)])

    try:
        from server.services.page_navigation_service import _ocr_find_text_bounds

        bounds = _ocr_find_text_bounds(engine, *needles)
        if bounds:
            return bounds
    except Exception:
        pass

    d = engine._ensure_u2() if hasattr(engine, "_ensure_u2") else None
    if not d:
        return None
    try:
        for needle in needles:
            sel = d(textContains=needle)
            if not sel.exists(timeout=0.4):
                sel = d(descriptionContains=needle)
            if not sel.exists(timeout=0.4):
                continue
            info = sel.info or {}
            rect = _node_bounds(info)
            if rect:
                return rect
    except Exception as e:
        SLog.w(TAG, f"anchor hierarchy lookup failed: {e}")
    return None


def _find_toggle_anchor_bounds(
    engine,
    screen_w: int,
    screen_h: int,
    anchors: List[str],
) -> Optional[Tuple[int, int, int, int]]:
    y_lo = int(screen_h * 0.62)
    for needle in anchors:
        bounds = _find_anchor_bounds(engine, needle, screen_w, screen_h)
        if not bounds:
            continue
        if bounds[1] >= y_lo or needle in _AGREEMENT_ANCHOR_WORDS:
            return bounds
    for needle in anchors:
        bounds = _find_anchor_bounds(engine, needle, screen_w, screen_h)
        if bounds:
            return bounds
    return None


def _grid_scan_bottom_band(screen_w: int, screen_h: int) -> List[Dict[str, Any]]:
    """底栏协议勾选区网格，供无 CheckBox 节点时 CLIP 视觉匹配。"""
    y_lo, y_hi = 0.76, 0.96
    x_lo, x_hi = 0.04, 0.42
    cell = max(40, int(screen_w * 0.07))
    step = max(16, cell // 2)
    out: List[Dict[str, Any]] = []
    y = int(screen_h * y_lo)
    y_end = int(screen_h * y_hi)
    x_start = int(screen_w * x_lo)
    x_end = int(screen_w * x_hi)
    while y + cell <= y_end:
        x = x_start
        while x + cell <= x_end:
            out.append(
                {
                    "x": x,
                    "y": y,
                    "w": cell,
                    "h": cell,
                    "label": "",
                    "source": "grid_bottom",
                }
            )
            x += step
        y += step
    return out


def _spatial_pick_toggle(
    candidates: List[Dict[str, Any]],
    anchor_bounds: Optional[Tuple[int, int, int, int]],
    *,
    index: Optional[int],
    wants_checked: bool,
) -> Optional[Dict[str, Any]]:
    pool = list(candidates)
    if wants_checked:
        unchecked = [c for c in pool if not c.get("checked")]
        if unchecked:
            pool = unchecked

    if index is not None and 0 <= index < len(pool):
        return pool[index]

    if not pool:
        return None

    if not anchor_bounds:
        return pool[0]

    ax1, ay1, ax2, ay2 = anchor_bounds
    acy = (ay1 + ay2) // 2
    row_band = max(28, (ay2 - ay1) + 24)

    row_hits = [
        c
        for c in pool
        if abs((c["y"] + c["h"] // 2) - acy) <= row_band
    ]
    if not row_hits:
        row_hits = pool

    left_of = [
        c
        for c in row_hits
        if (c["x"] + c["w"] // 2) <= ax1 + 8
    ]
    if left_of:
        return min(left_of, key=lambda c: ax1 - (c["x"] + c["w"] // 2))

    if not row_hits:
        return None

    return min(
        row_hits,
        key=lambda c: abs((c["y"] + c["h"] // 2) - acy) * 3 + abs((c["x"] + c["w"] // 2) - ax1),
    )


def _try_clip_on_toggles(
    engine,
    screen_w: int,
    screen_h: int,
    candidates: List[Dict[str, Any]],
    intent: ToggleIntent,
) -> Optional[Tuple[int, int, str]]:
    if not candidates:
        return None
    try:
        from server.core.vision.clip_service import clip_enabled, get_clip_service
        from server.services.clip_locate_service import _score_patch_candidates

        if not clip_enabled():
            return None
        svc = get_clip_service()
        if not svc.available():
            return None
        shot = engine.screenshot() if hasattr(engine, "screenshot") else None
        if shot is None:
            return None
        from server.services.clip_locate_service import _screenshot_to_bgr

        frame = _screenshot_to_bgr(shot)
        if frame is None:
            return None
        query, aliases = toggle_clip_queries(intent)
        text_vec = svc.encode_text_mixed(query, aliases)
        if text_vec is None:
            return None
        scored = _score_patch_candidates(frame, candidates, text_vec, svc, query=query, pad=8)
        if not scored:
            return None
        score, pick = scored[0]
        threshold = 0.18
        if score < threshold:
            SLog.i(TAG, f"toggle CLIP miss score={score:.3f} query={query!r}")
            return None
        cx = int(pick["x"]) + int(pick["w"]) // 2
        cy = int(pick["y"]) + int(pick["h"]) // 2
        SLog.i(
            TAG,
            f"toggle CLIP hit query={query!r} score={score:.3f} "
            f"@({cx},{cy}) src={pick.get('source')}",
        )
        return cx, cy, "clip_toggle"
    except Exception as e:
        SLog.w(TAG, f"toggle CLIP failed: {e}")
    return None


def _ocr_left_of_anchor(
    engine,
    anchor_bounds: Tuple[int, int, int, int],
    screen_h: int,
    *,
    screen_w: int = 1080,
) -> Optional[Tuple[int, int, str]]:
    x1, y1, x2, y2 = anchor_bounds
    cy = (y1 + y2) // 2
    if cy < int(screen_h * 0.5):
        return None
    offset = max(36, int(screen_w * 0.038))
    cx = max(int(screen_w * 0.05), x1 - offset)
    return cx, cy, "ocr_toggle_left"


def _ocr_agreement_checkbox_pos(
    engine,
    screen_w: int,
    screen_h: int,
) -> Optional[Tuple[int, int, str]]:
    """登录页底栏协议行：取最左侧协议 OCR 框，在其左侧点圆形勾选框。"""
    try:
        from server.services.page_navigation_service import _ocr_box_bounds

        shot = engine.screenshot() if hasattr(engine, "screenshot") else None
        if shot is None:
            return None
        from driver.agent.Crawl.ui_discovery import _ocr_analyze_shot

        y_lo = int(screen_h * 0.74)
        hits: List[Tuple[Tuple[int, int, int, int], str]] = []
        for it in _ocr_analyze_shot(shot) or []:
            text = (it.get("text") or "").strip()
            if not text:
                continue
            bounds = _ocr_box_bounds(it)
            if not bounds:
                continue
            cy = (bounds[1] + bounds[3]) // 2
            if cy < y_lo:
                continue
            if any(
                w in text
                for w in ("用户协议", "隐私", "已阅读", "阅读并同意", "条款", "同意")
            ):
                hits.append((bounds, text))
        if not hits:
            return None
        x1, y1, x2, y2 = min(hits, key=lambda h: h[0][0])[0]
        cy = (y1 + y2) // 2
        offset = max(40, int(screen_w * 0.042))
        cx = max(int(screen_w * 0.04), x1 - offset)
        SLog.i(TAG, f"agreement checkbox ocr-left @({cx},{cy}) text_x1={x1}")
        return cx, cy, "ocr_agreement_checkbox"
    except Exception as e:
        SLog.w(TAG, f"agreement checkbox ocr failed: {e}")
        return None


def resolve_toggle_tap(
    engine,
    screen_w: int,
    screen_h: int,
    label: str,
) -> Optional[Tuple[int, int, str]]:
    """
    通用开关点击坐标解析。
    顺序：无障碍小控件 + 文案锚点 → OCR 左侧偏移 → CLIP 打补丁。
  已处于目标状态时返回 method=already_checked。
    """
    intent = parse_toggle_intent(label)
    if not intent:
        return None

    candidates = discover_toggle_candidates(engine, screen_w, screen_h)
    anchor_list = _toggle_search_anchors(label, intent.anchor)
    anchor_bounds = _find_toggle_anchor_bounds(engine, screen_w, screen_h, anchor_list)

    if intent.kind == "checkbox" and re.search(r"底部|协议|隐私|勾选|条款", label or ""):
        ocr_cb = _ocr_agreement_checkbox_pos(engine, screen_w, screen_h)
        if ocr_cb:
            return ocr_cb

    if anchor_bounds and not candidates:
        ocr_hit = _ocr_left_of_anchor(
            engine, anchor_bounds, screen_h, screen_w=screen_w
        )
        if ocr_hit:
            SLog.i(
                TAG,
                f"toggle ocr-left (no u2) anchor={anchor_list!r} "
                f"@({ocr_hit[0]},{ocr_hit[1]})",
            )
            return ocr_hit

    if intent.wants_checked:
        checked_nodes = [c for c in candidates if c.get("checked")]
        if checked_nodes:
            already = _spatial_pick_toggle(
                checked_nodes,
                anchor_bounds,
                index=intent.index,
                wants_checked=True,
            )
            if already:
                cx = already["x"] + already["w"] // 2
                cy = already["y"] + already["h"] // 2
                return cx, cy, "already_checked"

    pick = _spatial_pick_toggle(
        candidates,
        anchor_bounds,
        index=intent.index,
        wants_checked=intent.wants_checked,
    )
    if pick:
        cx = pick["x"] + pick["w"] // 2
        cy = pick["y"] + pick["h"] // 2
        return cx, cy, str(pick.get("source") or "u2_toggle")

    if anchor_bounds:
        ocr_hit = _ocr_left_of_anchor(
            engine, anchor_bounds, screen_h, screen_w=screen_w
        )
        if ocr_hit:
            SLog.i(
                TAG,
                f"toggle ocr-left anchor={anchor_list!r} @({ocr_hit[0]},{ocr_hit[1]})",
            )
            return ocr_hit

    clip_hit = _try_clip_on_toggles(engine, screen_w, screen_h, candidates, intent)
    if clip_hit:
        return clip_hit

    if intent.kind == "checkbox":
        grid = _grid_scan_bottom_band(screen_w, screen_h)
        clip_hit = _try_clip_on_toggles(engine, screen_w, screen_h, grid, intent)
        if clip_hit:
            return clip_hit

    SLog.w(
        TAG,
        f"toggle miss label={label!r} anchor={intent.anchor!r} "
        f"candidates={len(candidates)} anchor_bounds={bool(anchor_bounds)}",
    )
    return None
