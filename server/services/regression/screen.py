# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""截图统一入口：按通道优先级抓一张图给 VLM / 落 trace。

Step 4：ADB 通路（subprocess `adb -s <sn> exec-out screencap -p`）
Step 4b：Remote 通路复用 driver.tentacle.engine.mobile.mRemote.RemoteEngine
         （ClawNode GET_SCREENSHOT → SCREENSHOT_RESULT 同步往返）
"""
from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Optional

from script.log import SLog

from server.services.runtime.run_context import RunContext

TAG = "ScreenCapture"


@dataclass
class CapturedScreen:
    """抓到的一张屏。"""

    ok: bool
    source: str = ""  # adb / remote / cached
    image_path: str = ""
    image_base64: str = ""  # 不含 data: 前缀
    image_mime: str = "image/png"
    width: int = 0
    height: int = 0
    elapsed_ms: int = 0
    error: str = ""
    # ClawNode 截图失败时的结构化回包（ACTION_RESULT.message / hint 等），写入 trace
    remote_detail: dict[str, Any] = field(default_factory=dict)

    def has_image(self) -> bool:
        return self.ok and bool(self.image_base64)


# ---------- ADB 通路 ----------


def _capture_via_adb(adb_serial: str, *, timeout_sec: float = 15.0) -> CapturedScreen:
    if not adb_serial or adb_serial.startswith("claw-"):
        return CapturedScreen(ok=False, source="adb", error="invalid adb serial")
    started = time.time()
    try:
        # exec-out 直出二进制比 shell screencap → pull 更快、更稳
        proc = subprocess.run(
            ["adb", "-s", adb_serial, "exec-out", "screencap", "-p"],
            capture_output=True,
            timeout=timeout_sec,
        )
    except FileNotFoundError:
        return CapturedScreen(ok=False, source="adb", error="adb binary not in PATH")
    except subprocess.TimeoutExpired:
        return CapturedScreen(ok=False, source="adb", error=f"adb screencap timeout {timeout_sec}s")
    except Exception as e:
        return CapturedScreen(ok=False, source="adb", error=f"adb screencap failed: {e}")
    elapsed_ms = int((time.time() - started) * 1000)
    if proc.returncode != 0 or not proc.stdout:
        return CapturedScreen(
            ok=False,
            source="adb",
            error=f"adb screencap rc={proc.returncode} stderr={(proc.stderr or b'')[:200]!r}",
            elapsed_ms=elapsed_ms,
        )
    png_bytes = proc.stdout
    # 落盘到 tmp 便于排查
    fd, path = tempfile.mkstemp(prefix=f"screen_{adb_serial}_", suffix=".png")
    try:
        os.write(fd, png_bytes)
    finally:
        os.close(fd)
    # 解析 width/height（不强求；解析失败就 0）
    width, height = _peek_png_size(png_bytes)
    return CapturedScreen(
        ok=True,
        source="adb",
        image_path=path,
        image_base64=base64.b64encode(png_bytes).decode("ascii"),
        image_mime="image/png",
        width=width,
        height=height,
        elapsed_ms=elapsed_ms,
    )


def _capture_via_ios_wda(udid: str, *, timeout_sec: float = 30.0) -> CapturedScreen:
    started = time.time()
    try:
        from server.services.runtime.ios_wda_session import get_ios_engine

        engine = get_ios_engine(udid)
        # 走引擎层 screenshot()（返回 base64），wda / appium 后端通用
        png_bytes = base64.b64decode(engine.screenshot())
    except Exception as e:
        return CapturedScreen(
            ok=False,
            source="ios_wda",
            error=f"wda screenshot failed: {e}",
            elapsed_ms=int((time.time() - started) * 1000),
        )
    elapsed_ms = int((time.time() - started) * 1000)
    fd, path = tempfile.mkstemp(prefix=f"screen_ios_{udid[:8]}_", suffix=".png")
    try:
        os.write(fd, png_bytes)
    finally:
        os.close(fd)
    width, height = _peek_png_size(png_bytes)
    return CapturedScreen(
        ok=True,
        source="ios_wda",
        image_path=path,
        image_base64=base64.b64encode(png_bytes).decode("ascii"),
        image_mime="image/png",
        width=width,
        height=height,
        elapsed_ms=elapsed_ms,
    )


def _peek_png_size(data: bytes) -> tuple[int, int]:
    """PNG 文件头里取 width/height。失败返回 (0,0)。"""
    try:
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
            return 0, 0
        # IHDR length=13 starts at byte 8; width @16-19, height @20-23
        w = int.from_bytes(data[16:20], "big")
        h = int.from_bytes(data[20:24], "big")
        return w, h
    except Exception:
        return 0, 0


def _compress_web_png(png_bytes: bytes, ratio: float) -> tuple[bytes, str, int, int]:
    """按 Web 压缩比例缩小截图。返回 (bytes, mime, orig_w, orig_h)。width/height 仍报原图，坐标按视口换算。"""
    orig_w, orig_h = _peek_png_size(png_bytes)
    if ratio <= 1.0 or not png_bytes:
        return png_bytes, "image/png", orig_w, orig_h
    try:
        from PIL import Image

        img = Image.open(BytesIO(png_bytes)).convert("RGB")
        orig_w, orig_h = img.size
        preview_w = max(1, round(orig_w / ratio))
        preview_h = max(1, round(orig_h / ratio))
        if preview_w < orig_w or preview_h < orig_h:
            img = img.resize((preview_w, preview_h), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        return buf.getvalue(), "image/jpeg", orig_w, orig_h
    except Exception as exc:
        SLog.w(TAG, f"web screenshot compress failed: {exc}")
        return png_bytes, "image/png", orig_w, orig_h


def _capture_via_playwright(ctx: RunContext, *, timeout_sec: float = 15.0) -> CapturedScreen:
    started = time.time()
    try:
        from server.services.runtime.playwright_hub import get_hub

        png_bytes = get_hub().screenshot_png(
            str(ctx.sn or ""), timeout_ms=int(timeout_sec * 1000),
        )
    except Exception as e:
        return CapturedScreen(
            ok=False,
            source="playwright",
            error=f"playwright screenshot failed: {e}",
            elapsed_ms=int((time.time() - started) * 1000),
        )
    elapsed_ms = int((time.time() - started) * 1000)
    if not png_bytes:
        return CapturedScreen(ok=False, source="playwright", error="empty screenshot", elapsed_ms=elapsed_ms)
    ratio = 2.0
    try:
        from server.services.system_settings_service import get_ai_web_compress_ratio

        ratio = get_ai_web_compress_ratio(getattr(ctx, "provider_id", None))
    except Exception:
        ratio = 2.0
    out, mime, width, height = _compress_web_png(png_bytes, ratio)
    suffix = ".jpg" if mime == "image/jpeg" else ".png"
    fd, path = tempfile.mkstemp(prefix="screen_web_", suffix=suffix)
    try:
        os.write(fd, out)
    finally:
        os.close(fd)
    return CapturedScreen(
        ok=True,
        source="playwright",
        image_path=path,
        image_base64=base64.b64encode(out).decode("ascii"),
        image_mime=mime,
        width=width,
        height=height,
        elapsed_ms=elapsed_ms,
    )


# ---------- ClawNode 截图失败解析 ----------


_SCREENSHOT_HINT_LABELS: dict[str, str] = {
    "media_projection_unauthorized": "屏幕捕获未授权（请在 ClawNode App 内点「屏幕捕获授权」）",
    "accessibility_off": "无障碍服务未开启或未授权截图",
    "secure_window": "当前界面为安全窗口，无法截图",
    "screenshot_too_fast": "截图间隔过短，请稍后重试",
    "bitmap_decode_failed": "截图解码失败",
    "timeout": "等待 ClawNode 回传超时",
    "device_offline": "设备 WebSocket 未连接",
    "missing_base64": "ClawNode 回包缺少 base64_image",
    "unknown": "未知截图失败",
}


def classify_clawnode_screenshot_hint(message: str) -> tuple[str, str]:
    """把 ClawNode ACTION_RESULT.message 归类，便于 UI / trace 一眼看懂。"""
    raw = (message or "").strip()
    low = raw.lower()
    if "屏幕捕获授权" in raw or "screen capture authorization" in low:
        return "media_projection_unauthorized", _SCREENSHOT_HINT_LABELS["media_projection_unauthorized"]
    if "no_accessibility" in low or "accessibility" in low:
        return "accessibility_off", _SCREENSHOT_HINT_LABELS["accessibility_off"]
    if "secure_window" in low or "secure window" in low:
        return "secure_window", _SCREENSHOT_HINT_LABELS["secure_window"]
    if "interval" in low and "short" in low:
        return "screenshot_too_fast", _SCREENSHOT_HINT_LABELS["screenshot_too_fast"]
    if "wraphardwarebuffer" in low:
        return "bitmap_decode_failed", _SCREENSHOT_HINT_LABELS["bitmap_decode_failed"]
    if "takescreenshot failed" in low:
        if "secure" in low:
            return "secure_window", _SCREENSHOT_HINT_LABELS["secure_window"]
        if "accessibility" in low:
            return "accessibility_off", _SCREENSHOT_HINT_LABELS["accessibility_off"]
    return "unknown", _SCREENSHOT_HINT_LABELS["unknown"]


def parse_clawnode_screenshot_response(data: Optional[dict[str, Any]]) -> dict[str, Any]:
    """把 ClawNode WS 回包（SCREENSHOT_RESULT 或 ACTION_RESULT）规范成 audit dict。"""
    if not data:
        return {}
    detail: dict[str, Any] = {
        "trace_id": str(data.get("trace_id") or ""),
        "response_type": str(data.get("type") or ""),
        "status": str(data.get("status") or ""),
    }
    msg = str(data.get("message") or data.get("stderr") or "").strip()
    if msg:
        detail["clawnode_message"] = msg
        code, label = classify_clawnode_screenshot_hint(msg)
        detail["hint_code"] = code
        detail["hint_label"] = label
    b64 = data.get("base64_image") or data.get("base64") or data.get("base64Image")
    detail["has_image"] = bool(b64)
    if data.get("format"):
        detail["format"] = data.get("format")
    return detail


def format_remote_screenshot_error(
    detail: dict[str, Any],
    *,
    fallback: str = "remote screenshot failed",
) -> str:
    """人类可读 error 串（summary / EventResult.error 用）。"""
    msg = str(detail.get("clawnode_message") or "").strip()
    hint = str(detail.get("hint_label") or "").strip()
    rtype = str(detail.get("response_type") or "").strip()
    if rtype == "ACTION_RESULT" and msg:
        parts = [f"ClawNode ACTION_RESULT: {msg}"]
        if hint:
            parts.append(f"({hint})")
        return " ".join(parts)
    if msg:
        return f"{fallback}: {msg}"
    if hint:
        return f"{fallback}: {hint}"
    return fallback


def screenshot_failure_meta(screen: Optional[CapturedScreen]) -> dict[str, Any]:
    """写入 EventResult.vlm_meta['screenshot_capture'] 的标准结构。"""
    if screen is None:
        return {"source": "", "error": "no screen captured"}
    meta: dict[str, Any] = {
        "source": screen.source,
        "error": screen.error,
        "elapsed_ms": screen.elapsed_ms,
    }
    if screen.remote_detail:
        meta["clawnode"] = dict(screen.remote_detail)
    return meta


# ---------- Remote 通路 (ClawNode WebSocket) ----------

# 同一设备短时间内重复 GET_SCREENSHOT 会触发 MIUI INTERVAL_TOO_SHORT；服务端复用最近一帧。
_remote_capture_cache: dict[str, tuple[float, CapturedScreen]] = {}
REMOTE_CAPTURE_MIN_INTERVAL_SEC = 0.9


def invalidate_remote_capture_cache(sn: str) -> None:
    """UI 动作后清缓存，避免 VLM/persona 读到动作前的旧帧。"""
    if sn:
        _remote_capture_cache.pop(sn, None)


def _clone_cached_screen(screen: CapturedScreen, *, source: str) -> CapturedScreen:
    return CapturedScreen(
        ok=screen.ok,
        source=source,
        image_path=screen.image_path,
        image_base64=screen.image_base64,
        image_mime=screen.image_mime,
        width=screen.width,
        height=screen.height,
        elapsed_ms=screen.elapsed_ms,
        error=screen.error,
        remote_detail=dict(screen.remote_detail),
    )


def _capture_via_remote(
    sn: str,
    *,
    platform: str = "android",
    timeout_sec: float = 15.0,
    quality: int = 80,
    max_attempts: int = 4,
    settle_ms: int = 120,
    force_fresh: bool = False,
) -> CapturedScreen:
    """通过 ClawNode 抓图；与对话/Copilot 共用 bootstrap + 空白帧重试，避免回归黑屏。

    对话里 `capture_device_screenshot` 会 bootstrap 引擎、唤醒屏幕并跳过黑/白过渡帧；
    回归若直接 `_request(GET_SCREENSHOT)` 容易在息屏或首帧黑屏时落盘全黑 JPEG。
    """
    if not sn:
        return CapturedScreen(ok=False, source="remote", error="missing sn")

    cached = _remote_capture_cache.get(sn)
    if cached and not force_fresh:
        age = time.time() - cached[0]
        if age < REMOTE_CAPTURE_MIN_INTERVAL_SEC and cached[1].ok and cached[1].has_image():
            SLog.i(TAG, f"remote capture cache hit sn={sn} age_ms={int(age * 1000)}")
            return _clone_cached_screen(cached[1], source="remote_cached")

    started = time.time()
    try:
        from server.websocket.device_manager import DeviceManager

        dm = DeviceManager()
        if sn not in dm.active_connections:
            return CapturedScreen(
                ok=False,
                source="remote",
                error=f"device {sn} offline (not in active_connections)",
                elapsed_ms=int((time.time() - started) * 1000),
                remote_detail={
                    "hint_code": "device_offline",
                    "hint_label": _SCREENSHOT_HINT_LABELS["device_offline"],
                },
            )
        if getattr(dm, "loop", None) is None:
            return CapturedScreen(
                ok=False,
                source="remote",
                error="DeviceManager event loop not initialized (server startup issue)",
                elapsed_ms=int((time.time() - started) * 1000),
            )

        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
        from server.services.shared.screenshot.regression_capture import shot_is_blank

        try:
            engine, _size_hint = bootstrap_mobile_engine(
                sn,
                platform or "android",
                reuse=True,
            )
        except Exception as e:
            return CapturedScreen(
                ok=False,
                source="remote",
                error=f"bootstrap_mobile_engine failed: {e}",
                elapsed_ms=int((time.time() - started) * 1000),
            )

        if hasattr(engine, "ensure_screen_ready"):
            if not engine.ensure_screen_ready():
                SLog.w(TAG, f"remote capture: screen not fully ready sn={sn}, will retry with wake")
        elif hasattr(engine, "screen_on"):
            try:
                engine.screen_on()
                time.sleep(0.55)
            except Exception:
                pass

        if settle_ms > 0:
            time.sleep(settle_ms / 1000.0)

        img = None
        last_detail: dict[str, Any] = {}
        attempts = max(1, int(max_attempts))
        for attempt in range(attempts):
            try:
                shot = engine.screenshot() if hasattr(engine, "screenshot") else None
            except Exception as e:
                shot = None
                last_detail = {"clawnode_message": str(e), "hint_code": "unknown"}
            if shot is not None and not shot_is_blank(shot):
                img = shot
                break
            SLog.w(
                TAG,
                f"remote screenshot blank sn={sn} attempt={attempt + 1}/{attempts}",
            )
            if hasattr(engine, "screen_on"):
                try:
                    engine.screen_on()
                except Exception:
                    pass
            elif hasattr(engine, "ensure_screen_ready"):
                try:
                    engine.ensure_screen_ready()
                except Exception:
                    pass
            if attempt < attempts - 1:
                time.sleep(0.55)

        elapsed_ms = int((time.time() - started) * 1000)
        if img is None:
            hint = last_detail.get("hint_label") or "截图全黑/全白，已重试仍失败"
            return CapturedScreen(
                ok=False,
                source="remote",
                error=f"remote screenshot blank after {attempts} attempts: {hint}",
                elapsed_ms=elapsed_ms,
                remote_detail={
                    **last_detail,
                    "hint_code": last_detail.get("hint_code") or "blank_frame",
                    "hint_label": hint,
                },
            )

        try:
            from PIL import Image

            if not isinstance(img, Image.Image):
                return CapturedScreen(
                    ok=False,
                    source="remote",
                    error="screenshot returned non-image",
                    elapsed_ms=elapsed_ms,
                )
            if img.mode != "RGB":
                img = img.convert("RGB")
            width, height = img.size
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=int(quality))
            img_bytes = buf.getvalue()
        except Exception as e:
            return CapturedScreen(
                ok=False,
                source="remote",
                error=f"encode screenshot failed: {e}",
                elapsed_ms=elapsed_ms,
            )

        fd, path = tempfile.mkstemp(prefix=f"screen_{sn}_", suffix=".jpg")
        try:
            os.write(fd, img_bytes)
        finally:
            os.close(fd)

        b64_str = base64.b64encode(img_bytes).decode("ascii")
        SLog.i(
            TAG,
            f"capture via remote sn={sn} elapsed={elapsed_ms}ms "
            f"fmt=jpeg size={width}x{height} bytes={len(img_bytes)} attempts={attempts}",
        )
        result = CapturedScreen(
            ok=True,
            source="remote",
            image_path=path,
            image_base64=b64_str,
            image_mime="image/jpeg",
            width=width,
            height=height,
            elapsed_ms=elapsed_ms,
        )
        _remote_capture_cache[sn] = (time.time(), result)
        return result
    except Exception as e:
        SLog.e(TAG, f"remote capture failed sn={sn}: {e}")
        return CapturedScreen(
            ok=False,
            source="remote",
            error=f"remote screenshot exception: {e}",
            elapsed_ms=int((time.time() - started) * 1000),
        )


# ---------- 顶层入口 ----------


def capture_screen(
    ctx: RunContext,
    *,
    prefer: tuple[str, ...] = ("adb", "remote"),
    timeout_sec: float = 15.0,
    force_fresh: bool = False,
) -> CapturedScreen:
    """按 prefer 顺序尝试抓图，命中第一个成功即返回。

    ClawNode 直连（claw-*）默认优先 remote（与对话/Copilot 共用 bootstrap 引擎）。
    若所有通道都失败，返回 ok=False，由调用方决定是 fallback 还是 fail 整个事件。
    """
    if str(ctx.sn or "").startswith("claw-") and prefer == ("adb", "remote"):
        prefer = ("remote", "adb")
    last: Optional[CapturedScreen] = None
    for ch in prefer:
        if ch == "adb":
            if ctx.adb.get("state") != "connected":
                last = CapturedScreen(ok=False, source="adb", error="adb not connected")
                continue
            adb_serial = str(ctx.adb.get("serial") or "")
            res = _capture_via_adb(adb_serial, timeout_sec=timeout_sec)
            if res.ok:
                SLog.i(TAG, f"capture via adb sn={ctx.sn} serial={adb_serial} elapsed={res.elapsed_ms}ms size={res.width}x{res.height}")
                return res
            last = res
        elif ch == "remote":
            if ctx.remote.get("state") != "connected":
                last = CapturedScreen(ok=False, source="remote", error="remote not connected")
                continue
            res = _capture_via_remote(
                ctx.sn,
                platform=ctx.platform or "android",
                timeout_sec=timeout_sec,
                force_fresh=force_fresh,
            )
            if res.ok:
                SLog.i(TAG, f"capture via remote sn={ctx.sn} elapsed={res.elapsed_ms}ms size={res.width}x{res.height}")
                return res
            last = res
        elif ch in ("ios_wda", "ios"):
            if ctx.ios.get("state") != "connected" and str(ctx.platform or "").lower() not in ("ios", "iphone", "ipad"):
                last = CapturedScreen(ok=False, source="ios_wda", error="ios not connected")
                continue
            udid = str(ctx.ios.get("udid") or ctx.sn or "")
            res = _capture_via_ios_wda(udid, timeout_sec=timeout_sec)
            if res.ok:
                SLog.i(TAG, f"capture via ios_wda sn={ctx.sn} elapsed={res.elapsed_ms}ms size={res.width}x{res.height}")
                return res
            last = res
        elif ch in ("playwright", "web"):
            state = str(ctx.playwright.get("state") or "")
            if state not in ("connected", "available") and str(ctx.platform or "").lower() not in ("web", "browser", "playwright"):
                last = CapturedScreen(ok=False, source="playwright", error="playwright not available")
                continue
            res = _capture_via_playwright(ctx, timeout_sec=timeout_sec)
            if res.ok:
                SLog.i(TAG, f"capture via playwright sn={ctx.sn} elapsed={res.elapsed_ms}ms size={res.width}x{res.height}")
                return res
            last = res
        else:
            last = CapturedScreen(ok=False, source=ch, error=f"unknown channel {ch}")
    return last or CapturedScreen(ok=False, error="no channel tried")
