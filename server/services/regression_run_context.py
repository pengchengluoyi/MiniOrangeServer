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


def format_run_elapsed(ms: int) -> str:
    """相对本次 run 起点的 HH:MM:SS，供回放侧栏展示。"""
    s = max(0, int(ms)) // 1000
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def run_elapsed_ms(*, per_case: bool = True) -> int:
    ctx = _run_ctx.get()
    if not ctx:
        return 0
    t0 = (ctx.get("case_t0") if per_case else None) or ctx.get("run_t0")
    if not t0:
        return 0
    return int((time.time() - float(t0)) * 1000)


def stamp_run_timing(entry: Dict[str, Any]) -> Dict[str, Any]:
    """为手势/步骤写入 run_elapsed_ms / run_elapsed。"""
    if not entry:
        return entry
    ms = run_elapsed_ms()
    if ms >= 0 and _run_ctx.get():
        entry["run_elapsed_ms"] = ms
        entry["run_elapsed"] = format_run_elapsed(ms)
    return entry


def apply_run_timing(entry: Dict[str, Any], ms: int) -> Dict[str, Any]:
    """写入已知的相对用例起点毫秒（守卫 Detect/Assert 等回放用）。"""
    if not entry:
        return entry
    if ms >= 0 and _run_ctx.get():
        entry["run_elapsed_ms"] = int(ms)
        entry["run_elapsed"] = format_run_elapsed(int(ms))
    return entry


def capture_trace_frame(tag: str, *, settle_ms: int = 120) -> str:
    """守卫 Detect/Assert 等无手势时的截图。"""
    ctx = _run_ctx.get()
    if not ctx or not ctx.get("capture"):
        return ""
    return _capture(tag, settle_ms=settle_ms, max_attempts=2)


def begin_run(
    *,
    run_id: str = "",
    sn: str = "",
    platform: str = "android",
    capture_screenshots: bool = True,
    test_package: str = "",
) -> None:
    _run_ctx.set(
        {
            "run_id": run_id or "",
            "sn": sn or "",
            "platform": platform or "android",
            "test_package": (test_package or "").strip(),
            "capture": bool(capture_screenshots and run_id and sn),
            "gestures": [],
            "watermark": 0,
            "run_t0": time.time(),
            "case_t0": time.time(),
            "foreground_drift_events": [],
            "foreground_drift_ms": 0,
            "_foreground_drift_open_since": None,
        }
    )


def is_regression_run() -> bool:
    """当前是否在飞书/回归批量执行上下文中。"""
    ctx = _run_ctx.get()
    return bool(ctx and ctx.get("run_id"))


def is_device_prep_done() -> bool:
    ctx = _run_ctx.get()
    return bool(ctx and ctx.get("device_prep_done"))


def mark_device_prep_done() -> None:
    ctx = _run_ctx.get()
    if ctx is not None:
        ctx["device_prep_done"] = True


def begin_case() -> None:
    """每条用例单独计时（侧栏时间戳相对本用例起点）。"""
    ctx = _run_ctx.get()
    if not ctx:
        return
    ctx["case_t0"] = time.time()
    ctx["watermark"] = len(ctx.get("gestures") or [])


def record_foreground_drift(
    *,
    expected_package: str,
    actual_package: str,
    phase: str = "",
    note: str = "",
) -> Dict[str, Any]:
    """记录被测应用不在前台的时刻（不拉回应用）。"""
    ctx = _run_ctx.get()
    now = time.time()
    event = {
        "at": datetime.now().isoformat(timespec="milliseconds"),
        "expected_package": (expected_package or "").strip(),
        "actual_package": (actual_package or "").strip(),
        "phase": (phase or "").strip(),
        "note": (note or "").strip(),
        "run_elapsed_ms": run_elapsed_ms(per_case=False),
    }
    if not ctx:
        return event
    open_since = ctx.get("_foreground_drift_open_since")
    if open_since is None:
        ctx["_foreground_drift_open_since"] = now
    events = ctx.setdefault("foreground_drift_events", [])
    if not events or events[-1].get("actual_package") != event["actual_package"]:
        events.append(event)
    return event


def close_foreground_drift_segment() -> int:
    """被测应用回到前台时，累计本次离屏时长（毫秒）。"""
    ctx = _run_ctx.get()
    if not ctx:
        return 0
    open_since = ctx.get("_foreground_drift_open_since")
    if open_since is None:
        return int(ctx.get("foreground_drift_ms") or 0)
    delta_ms = max(0, int((time.time() - float(open_since)) * 1000))
    ctx["foreground_drift_ms"] = int(ctx.get("foreground_drift_ms") or 0) + delta_ms
    ctx["_foreground_drift_open_since"] = None
    return int(ctx["foreground_drift_ms"])


def get_foreground_drift_summary() -> Dict[str, Any]:
    ctx = _run_ctx.get() or {}
    events = list(ctx.get("foreground_drift_events") or [])
    total_ms = int(ctx.get("foreground_drift_ms") or 0)
    open_since = ctx.get("_foreground_drift_open_since")
    if open_since is not None:
        total_ms += max(0, int((time.time() - float(open_since)) * 1000))
    packages = sorted({e.get("actual_package") for e in events if e.get("actual_package")})
    msg = ""
    if total_ms > 0 or events:
        sec = total_ms / 1000.0
        where = "、".join(packages[:4]) if packages else "其它应用"
        msg = f"被测应用累计 {sec:.1f}s 不在前台（当前前台曾为：{where}）"
    return {
        "total_ms": total_ms,
        "event_count": len(events),
        "events": events[-20:],
        "message": msg,
    }


def end_run() -> List[Dict[str, Any]]:
    ctx = _run_ctx.get()
    _run_ctx.set(None)
    if not ctx:
        return []
    return list(ctx.get("gestures") or [])


def take_foreground_drift_summary() -> Dict[str, Any]:
    """结束 run 前取出离屏统计并清零上下文。"""
    summary = get_foreground_drift_summary()
    ctx = _run_ctx.get()
    if ctx:
        ctx["foreground_drift_summary"] = summary
    return summary


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
            return 350
        if label in ("同意", "同意并继续", "仅在使用中允许", "始终允许"):
            return 350
        return 450
    if kind == "input":
        return 400
    if kind == "swipe":
        return 300
    return 200


def _capture(
    tag: str,
    *,
    settle_ms: int = 0,
    entry: Optional[Dict[str, Any]] = None,
    max_attempts: Optional[int] = None,
) -> str:
    ctx = _run_ctx.get()
    if not ctx or not ctx.get("capture"):
        return ""
    try:
        from server.services.regression_capture import capture_device_screenshot

        wait_ms = settle_ms or (_default_settle_ms(entry) if entry else 0)
        phase = ((entry or {}).get("phase") or "").lower()
        label = ((entry or {}).get("label") or "").strip()
        fast_overlay = phase in ("consent_dismiss", "consent_agree", "permission_dismiss") or label in (
            "同意",
            "同意并继续",
        )
        if fast_overlay:
            wait_ms = min(wait_ms, 350)
        attempts = (
            max_attempts
            if max_attempts is not None
            else (2 if fast_overlay else (5 if wait_ms >= 600 else 3))
        )
        return capture_device_screenshot(
            ctx["sn"],
            ctx.get("platform") or "android",
            run_id=ctx["run_id"],
            tag=tag,
            settle_ms=wait_ms,
            max_attempts=attempts,
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
        from server.services.screen_frame_service import invalidate_screen_frame

        invalidate_screen_frame()
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
    capture_meta = {"kind": kind, "label": label, "phase": phase}
    if ctx and ctx.get("capture") and not before:
        phase_l = (phase or "").lower()
        if phase_l in ("consent_dismiss", "consent_agree", "permission_dismiss"):
            before = _capture(
                f"{tag_base}_before",
                settle_ms=120,
                entry=capture_meta,
                max_attempts=1,
            )
        else:
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
    stamp_run_timing(entry)
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
