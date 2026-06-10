# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""回归执行上下文：全局手势审计 + 截图与 report 对齐。"""
from __future__ import annotations

import contextvars
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

_run_ctx: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "regression_run_ctx", default=None
)


def begin_run(
    *,
    run_id: str = "",
    sn: str = "",
    platform: str = "android",
    capture_screenshots: bool = True,
) -> None:
    _run_ctx.set(
        {
            "run_id": run_id or "",
            "sn": sn or "",
            "platform": platform or "android",
            "capture": bool(capture_screenshots and run_id and sn),
            "gestures": [],
            "watermark": 0,
        }
    )


def end_run() -> List[Dict[str, Any]]:
    ctx = _run_ctx.get()
    _run_ctx.set(None)
    if not ctx:
        return []
    return list(ctx.get("gestures") or [])


def get_ctx() -> Optional[Dict[str, Any]]:
    return _run_ctx.get()


def mark_step() -> int:
    ctx = _run_ctx.get()
    if not ctx:
        return 0
    wm = len(ctx.get("gestures") or [])
    ctx["watermark"] = wm
    return wm


def take_gestures_since_watermark() -> List[Dict[str, Any]]:
    ctx = _run_ctx.get()
    if not ctx:
        return []
    gestures = ctx.get("gestures") or []
    wm = int(ctx.get("watermark") or 0)
    return list(gestures[wm:])


def _default_settle_ms(entry: Dict[str, Any]) -> int:
    kind = (entry.get("kind") or "").lower()
    label = (entry.get("label") or "").strip()
    phase = (entry.get("phase") or "").lower()
    if kind == "click":
        if phase in ("consent_dismiss", "consent_agree", "permission_dismiss"):
            return 850
        if label in ("同意", "同意并继续", "仅在使用中允许", "始终允许"):
            return 850
        return 450
    if kind == "input":
        return 400
    if kind == "swipe":
        return 300
    return 200


def _capture(tag: str, *, settle_ms: int = 0, entry: Optional[Dict[str, Any]] = None) -> str:
    ctx = _run_ctx.get()
    if not ctx or not ctx.get("capture"):
        return ""
    try:
        from server.services.regression_capture import capture_device_screenshot

        wait_ms = settle_ms or (_default_settle_ms(entry) if entry else 0)
        return capture_device_screenshot(
            ctx["sn"],
            ctx.get("platform") or "android",
            run_id=ctx["run_id"],
            tag=tag,
            settle_ms=wait_ms,
            max_attempts=5 if wait_ms >= 600 else 3,
        )
    except Exception:
        return ""


def invalidate_screen_cache() -> None:
    """手势改变后作废屏文本/OCR 缓存。"""
    ctx = _run_ctx.get()
    if ctx is not None:
        ctx.pop("screen_blob", None)
        ctx.pop("screen_ocr", None)
        ctx.pop("screen_wm", None)
    try:
        from server.services.page_context_service import invalidate_engine_screen_cache

        invalidate_engine_screen_cache()
    except Exception:
        pass


def record_gesture(
    kind: str,
    summary: str,
    *,
    ok: bool = True,
    msg: str = "",
    method: str = "",
    x: int = 0,
    y: int = 0,
    label: str = "",
    source: str = "engine",
    phase: str = "",
    screenshot_before: str = "",
    screenshot_after: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """记录一次真实下发的设备手势（点击/滑动/返回等）。"""
    ctx = _run_ctx.get()
    if ctx is not None:
        invalidate_screen_cache()
    t0 = time.time()
    gid = uuid.uuid4().hex[:10]
    tag_base = f"g{gid}_{kind}"
    before = screenshot_before
    after = screenshot_after
    if ctx and ctx.get("capture") and not before:
        before = _capture(f"{tag_base}_before")
    entry: Dict[str, Any] = {
        "type": "gesture",
        "gesture_id": gid,
        "kind": kind,
        "summary": summary,
        "ok": ok,
        "msg": msg,
        "method": method or kind,
        "x": x,
        "y": y,
        "label": label,
        "source": source,
        "phase": phase,
        "screenshot_before": before,
        "screenshot_after": after,
        "started_at": datetime.fromtimestamp(t0).isoformat(timespec="milliseconds"),
        "duration_ms": 0,
    }
    if extra:
        entry.update(extra)
    if ctx:
        gestures: List[Dict[str, Any]] = ctx.setdefault("gestures", [])
        entry["index"] = len(gestures)
        gestures.append(entry)
    return entry


def finish_gesture(
    entry: Dict[str, Any],
    *,
    ok: Optional[bool] = None,
    msg: str = "",
    settle_ms: int = 0,
) -> None:
    """手势结束后补 after 截图与耗时（等待 UI 稳定，跳过白屏过渡帧）。"""
    ctx = _run_ctx.get()
    if ok is not None:
        entry["ok"] = ok
    if msg:
        entry["msg"] = msg
    if ctx and ctx.get("capture") and not entry.get("screenshot_after"):
        kind = entry.get("kind") or "step"
        gid = entry.get("gesture_id") or "x"
        entry["screenshot_after"] = _capture(
            f"g{gid}_{kind}_after",
            settle_ms=settle_ms,
            entry=entry,
        )
        invalidate_screen_cache()
    started = entry.get("started_at") or ""
    try:
        t0 = datetime.fromisoformat(started).timestamp()
        entry["duration_ms"] = int((time.time() - t0) * 1000)
    except Exception:
        entry["duration_ms"] = 0
