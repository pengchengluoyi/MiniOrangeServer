# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
统一屏帧（截图）服务：OCR / CLIP / icon_row / 阻塞检测 共用同一张截图。

同一手势水位（gestures 长度）内复用；手势变更后 invalidate_screen_frame 强制下次重截。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from script.log import SLog

TAG = "ScreenFrame"
_FRAME_ATTR = "_mo_screen_frame"
_SNAP_ATTR = "_mo_screen_snap"


def screen_frame_watermark() -> int:
    try:
        from server.services.shared.run_context.regression_run_context import get_ctx

        ctx = get_ctx()
        if ctx is not None:
            return len(ctx.get("gestures") or [])
    except Exception:
        pass
    return -1


def invalidate_screen_frame(engine=None) -> None:
    """手势或强制刷新后清除屏帧、OCR、阻塞屏缓存。"""
    attrs = (_FRAME_ATTR, _SNAP_ATTR, "_mo_blocking_state")
    if engine is not None:
        for attr in attrs:
            try:
                delattr(engine, attr)
            except Exception:
                pass
    try:
        from driver.agent.Crawl.device_bootstrap import _ENGINE_CACHE

        for entry in _ENGINE_CACHE.values():
            eng = entry.get("engine")
            if eng is not None:
                for attr in attrs:
                    try:
                        delattr(eng, attr)
                    except Exception:
                        pass
    except Exception:
        pass


def _shot_to_bgr(shot) -> Optional[Any]:
    from server.services.shared.page_context.page_context_service import _shot_to_bgr as _bgr

    return _bgr(shot)


def get_screen_frame(engine, *, force: bool = False) -> Dict[str, Any]:
    """
    单次 engine.screenshot()，供 OCR/CLIP/icon_row 共用。
    返回 shot, shot_bgr, ocr_items, ocr_text, blob, screen_w, screen_h, wm, ...
    """
    wm = screen_frame_watermark()
    if not force and engine is not None:
        cached = getattr(engine, _FRAME_ATTR, None) or getattr(engine, _SNAP_ATTR, None)
        if isinstance(cached, dict) and cached.get("wm") == wm:
            if cached.get("shot") is not None or cached.get("screen_not_ready"):
                return cached

    w, h = 1080, 1920
    try:
        if hasattr(engine, "screen_size"):
            w, h = engine.screen_size()
        elif hasattr(engine, "_display_size"):
            w, h = engine._display_size()
    except Exception:
        pass

    shot = None
    locked = False
    blank = False
    try:
        if hasattr(engine, "_is_keyguard_showing"):
            locked = bool(engine._is_keyguard_showing())
        if hasattr(engine, "screenshot"):
            shot = engine.screenshot()
            if shot is not None and hasattr(engine, "_is_mostly_black_image"):
                blank = bool(engine._is_mostly_black_image(shot))
    except Exception as e:
        SLog.w(TAG, f"screen capture failed: {e}")

    if blank or locked:
        frame = {
            "wm": wm,
            "shot": None,
            "shot_bgr": None,
            "ocr_text": "",
            "blob": "",
            "ocr_items": [],
            "screen_w": w,
            "screen_h": h,
            "screen_not_ready": True,
            "reason": "keyguard" if locked else "screen_blank",
        }
        if engine is not None:
            setattr(engine, _FRAME_ATTR, frame)
            setattr(engine, _SNAP_ATTR, frame)
        return frame

    hierarchy_lines: List[str] = []
    try:
        from driver.agent.Crawl.ui_discovery import discover_clickables_from_hierarchy

        for t in discover_clickables_from_hierarchy(engine, w, h, max_items=80):
            if t.label:
                hierarchy_lines.append(t.label)
    except Exception as e:
        SLog.w(TAG, f"hierarchy collect failed: {e}")

    ocr_lines: List[str] = []
    ocr_items: List[Dict[str, Any]] = []
    try:
        if shot is not None:
            from driver.agent.Crawl.ui_discovery import _ocr_analyze_shot

            ocr_items = list(_ocr_analyze_shot(shot) or [])
            for it in ocr_items:
                t = (it.get("text") or "").strip()
                if t:
                    ocr_lines.append(t)
    except Exception as e:
        SLog.w(TAG, f"screen ocr failed: {e}")

    ocr_text = "\n".join(ocr_lines)
    parts: List[str] = []
    if hierarchy_lines:
        parts.append("\n".join(hierarchy_lines))
    if ocr_lines:
        parts.append(ocr_text)
    blob = "\n".join(parts)
    shot_bgr = _shot_to_bgr(shot) if shot is not None else None

    frame: Dict[str, Any] = {
        "wm": wm,
        "shot": shot,
        "shot_bgr": shot_bgr,
        "ocr_text": ocr_text,
        "blob": blob,
        "ocr_items": ocr_items,
        "screen_w": w,
        "screen_h": h,
        "screen_not_ready": False,
    }
    if engine is not None:
        setattr(engine, _FRAME_ATTR, frame)
        setattr(engine, _SNAP_ATTR, frame)
    try:
        from server.services.shared.run_context.regression_run_context import get_ctx

        ctx = get_ctx()
        if ctx is not None:
            ctx["screen_blob"] = blob
            ctx["screen_ocr"] = ocr_text
            ctx["screen_wm"] = wm
    except Exception:
        pass
    return frame


def get_frame_shot(engine, *, force: bool = False):
    return get_screen_frame(engine, force=force).get("shot")


def get_frame_bgr(engine, *, force: bool = False):
    frame = get_screen_frame(engine, force=force)
    bgr = frame.get("shot_bgr")
    if bgr is not None:
        return bgr
    return _shot_to_bgr(frame.get("shot"))
