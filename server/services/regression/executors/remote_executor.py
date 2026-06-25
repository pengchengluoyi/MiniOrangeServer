# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Remote (ClawNode WebSocket) 执行通道。"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from script.log import SLog

from server.services.ai.regression.schemas import EventResult, EventStatus, PlanEvent
from server.services.regression.executors.base import (
    Executor,
    ExecutorContext,
    _now_iso,
    make_event_result,
)

TAG = "RemoteExecutor"

_SUPPORTED_CAPS: set[str] = {
    "launch_app",
    "close_app",
    "press_key",
    "wait_ms",
    "swipe_direction",
    "swipe_element_to_element",
    "install_apk",
    "set_clipboard",
    "tap_element",
    "long_press_element",
    "input_text",
    "read_device_data",
    "exec_script",
}


class RemoteExecutor:
    id = "remote"

    def supports(self, capability_id: str) -> bool:
        return capability_id in _SUPPORTED_CAPS

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        started_at = _now_iso()
        t0 = time.time()
        cap = event.capability_id
        try:
            if ctx.run_context.remote.get("state") != "connected":
                return self._decline(event, started_at, t0, "remote channel not connected")

            if cap == "wait_ms":
                return self._wait_ms(event, started_at, t0)
            if cap == "launch_app":
                return self._launch_app(event, ctx, started_at, t0)
            if cap == "close_app":
                return self._close_app(event, ctx, started_at, t0)
            if cap == "press_key":
                key = (event.params or {}).get("key") or (event.params or {}).get("keycode") or ""
                return self._send(event, ctx, "key", {"keyevent": key}, started_at, t0,
                                  summary_ok=f"按键 {key}")
            if cap == "set_clipboard":
                text = (event.params or {}).get("text") or ""
                return self._send(event, ctx, "set_clipboard", {"text": text}, started_at, t0,
                                  summary_ok=f"剪贴板设置 {len(text)} 字")
            if cap == "tap_element":
                p = event.params or {}
                x, y = p.get("x"), p.get("y")
                if x is None or y is None:
                    return self._fail(event, started_at, t0, "tap_element 缺坐标（router 未注入 VLM locate 结果）")
                return self._send(event, ctx, "tap", {"x": int(x), "y": int(y)}, started_at, t0,
                                  summary_ok=f"点击 ({x},{y})")
            if cap == "long_press_element":
                p = event.params or {}
                x, y = p.get("x"), p.get("y")
                if x is None or y is None:
                    return self._fail(event, started_at, t0, "long_press 缺坐标")
                duration = int(p.get("duration_ms") or 1000)
                return self._send(event, ctx, "swipe", {
                    "x1": int(x), "y1": int(y), "x2": int(x), "y2": int(y), "duration": duration,
                }, started_at, t0, summary_ok=f"长按 ({x},{y}) {duration}ms")
            if cap == "swipe_direction":
                return self._swipe_direction(event, ctx, started_at, t0)
            if cap == "swipe_element_to_element":
                p = event.params or {}
                fx, fy = p.get("from_x"), p.get("from_y")
                tx, ty = p.get("to_x"), p.get("to_y")
                if None in (fx, fy, tx, ty):
                    return self._fail(event, started_at, t0, "swipe 缺 from/to 坐标")
                return self._send(event, ctx, "swipe", {
                    "x1": int(fx), "y1": int(fy), "x2": int(tx), "y2": int(ty),
                    "duration": int(p.get("duration_ms") or 400),
                }, started_at, t0, summary_ok=f"拖拽 ({fx},{fy})→({tx},{ty})")
            if cap == "install_apk":
                url = (event.params or {}).get("url") or ""
                if not url:
                    return self._fail(event, started_at, t0, "remote install_apk 需 params.url (server 可分发)")
                return self._send(event, ctx, "install_apk", {
                    "url": url,
                    "fileName": (event.params or {}).get("file_name"),
                }, started_at, t0, summary_ok=f"下载安装 {url}", timeout_sec=600.0)
            if cap == "input_text":
                return self._input_text(event, ctx, started_at, t0)
            if cap == "read_device_data":
                return self._read_device_data(event, ctx, started_at, t0)
            if cap == "exec_script":
                return self._exec_script(event, ctx, started_at, t0)
            return self._decline(event, started_at, t0, f"RemoteExecutor 暂不处理 capability={cap}")
        except Exception as e:
            SLog.e(TAG, f"execute exception cap={cap} sn={ctx.run_context.sn}: {e}")
            return self._fail(event, started_at, t0, f"exception: {e}")

    # ---------- helpers ----------

    def _resolve_package(self, event: PlanEvent, ctx: ExecutorContext) -> str:
        params = event.params or {}
        pkg = str(params.get("package") or params.get("pkg") or "").strip()
        if pkg:
            return pkg
        shared = ctx.shared or {}
        pkg = str(shared.get("target_package") or "").strip()
        if pkg:
            return pkg
        return str(getattr(ctx.run_context, "target_package", "") or "").strip()

    def _bootstrap_remote_engine(self, ctx: ExecutorContext):
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

        sn = ctx.run_context.sn
        platform = ctx.run_context.platform or "android"
        engine, _size = bootstrap_mobile_engine(sn, platform, reuse=True)
        if type(engine).__name__ != "RemoteEngine":
            raise RuntimeError(f"expected RemoteEngine, got {type(engine).__name__}")
        return engine

    def _launch_app(self, event: PlanEvent, ctx: ExecutorContext, started_at: str, t0: float) -> EventResult:
        """通过 ClawNode OPEN_APP 拉起被测应用（RemoteEngine.start_app），并校验前台包名。"""
        pkg = self._resolve_package(event, ctx)
        if not pkg:
            return self._fail(event, started_at, t0, "launch_app 缺 params.package（也未配置 target_package）")

        activity = str((event.params or {}).get("activity") or "").strip()
        try:
            engine = self._bootstrap_remote_engine(ctx)
        except Exception as e:
            return self._fail(event, started_at, t0, f"remote engine bootstrap failed: {e}")

        # launch 前默认只按 Home 退出设置/桌面，不走拟人强停（强停会停在设置页，干扰 OPEN_APP 验收）
        stop_detail: dict = {}
        force_cold = bool((event.params or {}).get("force_cold_start"))
        if force_cold and hasattr(engine, "stop_app"):
            try:
                stop_ok = bool(engine.stop_app(pkg))
                stop_detail = {"stop_ok": stop_ok, "via": "persona_force_stop", "force_cold_start": True}
                if not stop_ok:
                    SLog.w(TAG, f"stop_app before launch failed pkg={pkg} sn={ctx.run_context.sn}")
                    ctx.shared["launch_stop_failed"] = True
                time.sleep(0.5)
            except Exception as e:
                SLog.w(TAG, f"stop_app before launch exception: {e}")
                stop_detail = {"stop_error": str(e), "force_cold_start": True}
                ctx.shared["launch_stop_failed"] = True
        elif hasattr(engine, "press_key"):
            try:
                engine.press_key("home")
                time.sleep(0.35)
                stop_detail = {"via": "home_before_launch"}
            except Exception as e:
                SLog.w(TAG, f"home before launch ignored: {e}")

        try:
            ok = bool(engine.start_app(pkg, activity=activity))
        except Exception as e:
            return self._fail(event, started_at, t0, f"OPEN_APP exception: {e}")

        from server.services.regression.screen import invalidate_remote_capture_cache
        invalidate_remote_capture_cache(ctx.run_context.sn)

        fg = ""
        try:
            fg = str(engine.current_package() or "").strip()
        except Exception:
            pass

        elapsed = int((time.time() - t0) * 1000)
        raw = {"package": pkg, "foreground": fg, "via": "OPEN_APP", **stop_detail}
        if ok:
            ctx.shared["last_launched_package"] = pkg
            return make_event_result(
                event,
                status=EventStatus.PASS,
                executor_used=self.id,
                started_at=started_at,
                elapsed_ms=elapsed,
                summary=f"OPEN_APP 已启动 {pkg}" + (f"（前台 {fg}）" if fg else ""),
                raw_response=raw,
            )

        err = f"OPEN_APP 未能将 {pkg} 拉到前台"
        if fg:
            err += f"（当前前台: {fg}）"
        if stop_detail.get("stop_ok") is False:
            err += "；前置强停未成功"
        return make_event_result(
            event,
            status=EventStatus.FAIL,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=elapsed,
            summary=err,
            error=err,
            raw_response=raw,
        )

    def _close_app(self, event: PlanEvent, ctx: ExecutorContext, started_at: str, t0: float) -> EventResult:
        pkg = self._resolve_package(event, ctx)
        if not pkg:
            return self._fail(event, started_at, t0, "close_app 缺 params.package")

        from server.services.regression.persona_remote_lifecycle import force_stop_app_via_persona

        app_name = str((ctx.shared or {}).get("app_name") or (event.params or {}).get("app_name") or "")
        sn = ctx.run_context.sn
        platform = ctx.run_context.platform or "android"
        ok, msg, detail = force_stop_app_via_persona(
            sn, pkg, platform=platform, app_name=app_name,
        )
        elapsed = int((time.time() - t0) * 1000)
        ex_used = str(detail.get("executor") or "ai_persona")
        if ok:
            return make_event_result(
                event,
                status=EventStatus.PASS,
                executor_used=ex_used,
                started_at=started_at,
                elapsed_ms=elapsed,
                summary=msg or f"拟人强停 {pkg}",
                raw_response={"package": pkg, "via": "persona_force_stop", **detail},
            )
        return make_event_result(
            event,
            status=EventStatus.FAIL,
            executor_used=ex_used,
            started_at=started_at,
            elapsed_ms=elapsed,
            summary=msg or f"拟人强停 {pkg} 失败",
            error=msg or f"拟人强停 {pkg} 失败",
            raw_response={"package": pkg, "via": "persona_force_stop", **detail},
        )

    def _swipe_direction(self, event: PlanEvent, ctx: ExecutorContext, started_at: str, t0: float) -> EventResult:
        params = event.params or {}
        direction = str(params.get("direction") or "up").lower()
        duration_ms = int(params.get("duration_ms") or 300)
        try:
            engine = self._bootstrap_remote_engine(ctx)
        except Exception as e:
            return self._fail(event, started_at, t0, f"remote engine bootstrap failed: {e}")

        w, h = engine.screen_size()
        cx = w // 2
        steps = {
            "up": (cx, int(h * 0.75), cx, int(h * 0.25)),
            "down": (cx, int(h * 0.25), cx, int(h * 0.75)),
            "left": (int(w * 0.85), h // 2, int(w * 0.15), h // 2),
            "right": (int(w * 0.15), h // 2, int(w * 0.85), h // 2),
        }
        if direction not in steps:
            return self._fail(event, started_at, t0, f"unsupported direction={direction}")
        x1, y1, x2, y2 = steps[direction]
        try:
            engine.swipe_norm(x1, y1, x2, y2, duration=duration_ms / 1000.0)
        except Exception as e:
            return self._fail(event, started_at, t0, f"remote swipe failed: {e}")
        from server.services.regression.screen import invalidate_remote_capture_cache
        invalidate_remote_capture_cache(ctx.run_context.sn)
        elapsed = int((time.time() - t0) * 1000)
        return make_event_result(
            event,
            status=EventStatus.PASS,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=elapsed,
            summary=f"滑动 {direction}",
            raw_response={"from": (x1, y1), "to": (x2, y2), "via": "SWIPE"},
        )

    def _input_text(self, event: PlanEvent, ctx: ExecutorContext, started_at: str, t0: float) -> EventResult:
        params = event.params or {}
        text = str(params.get("text") or "")
        if not text:
            return self._fail(event, started_at, t0, "input_text 缺 params.text")
        x, y = params.get("x"), params.get("y")

        if x is not None and y is not None:
            tap_res = self._send(
                event, ctx, "tap",
                {"x": int(x), "y": int(y)},
                started_at, t0,
                summary_ok=f"点击输入框 ({x},{y})",
            )
            if tap_res.status != EventStatus.PASS:
                return tap_res
            time.sleep(0.2)

        # 中文/emoji：剪贴板 + INPUT_TEXT（设备端 SET_TEXT 或 PASTE）
        clip_res = self._send(
            event, ctx, "set_clipboard",
            {"text": text},
            started_at, t0,
            summary_ok=f"剪贴板 {len(text)} 字",
        )
        if clip_res.status != EventStatus.PASS:
            return clip_res
        time.sleep(0.15)

        payload: dict[str, Any] = {"text": text}
        if x is not None and y is not None:
            payload["x"] = int(x)
            payload["y"] = int(y)
        return self._send(
            event, ctx, "input_text",
            payload,
            started_at, t0,
            summary_ok=f"输入 {len(text)} 字",
        )

    def _read_device_data(self, event: PlanEvent, ctx: ExecutorContext, started_at: str, t0: float) -> EventResult:
        key = str((event.params or {}).get("key") or "model")
        prop_map = {
            "model": "ro.product.model",
            "brand": "ro.product.brand",
            "android_version": "ro.build.version.release",
            "sdk": "ro.build.version.sdk",
            "abi": "ro.product.cpu.abi",
        }
        prop = prop_map.get(key, key)
        try:
            engine = self._bootstrap_remote_engine(ctx)
        except Exception as e:
            return self._fail(event, started_at, t0, f"remote engine bootstrap failed: {e}")
        try:
            value = str(engine.shell(f"getprop {prop}") or "").strip()
        except Exception as e:
            return self._fail(event, started_at, t0, f"getprop failed: {e}")
        elapsed = int((time.time() - t0) * 1000)
        if not value:
            return self._fail(event, started_at, t0, f"读取 {key} 为空")
        return make_event_result(
            event,
            status=EventStatus.PASS,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=elapsed,
            summary=f"{key}={value}",
            raw_response={"key": key, "value": value, "via": "RUN_SHELL"},
        )

    def _exec_script(self, event: PlanEvent, ctx: ExecutorContext, started_at: str, t0: float) -> EventResult:
        params = event.params or {}
        script = str(params.get("script") or "")
        script_id = str(params.get("script_id") or "")
        if not script.strip() and not script_id.strip():
            return self._fail(event, started_at, t0, "exec_script 需 params.script 或 params.script_id")

        language = str(params.get("language") or "dsl")
        timeout_ms = int(params.get("timeout_ms") or 60_000)
        script_vars = params.get("script_vars") if isinstance(params.get("script_vars"), dict) else None

        try:
            engine = self._bootstrap_remote_engine(ctx)
        except Exception as e:
            return self._fail(event, started_at, t0, f"remote engine bootstrap failed: {e}")

        try:
            ok, stdout, stderr = engine.exec_script(
                script,
                script_id=script_id,
                language=language,
                timeout_ms=timeout_ms,
                script_vars=script_vars,
            )
        except Exception as e:
            return self._fail(event, started_at, t0, f"EXEC_SCRIPT exception: {e}")

        elapsed = int((time.time() - t0) * 1000)
        raw = {
            "via": "EXEC_SCRIPT",
            "language": language,
            "script_id": script_id or None,
            "stdout": stdout[:4000] if stdout else "",
            "stderr": stderr[:1000] if stderr else "",
        }
        if ok:
            summary = f"EXEC_SCRIPT 成功"
            if script_id:
                summary += f" ({script_id})"
            if stdout:
                summary += f": {stdout[:120]}"
            return make_event_result(
                event,
                status=EventStatus.PASS,
                executor_used=self.id,
                started_at=started_at,
                elapsed_ms=elapsed,
                summary=summary,
                raw_response=raw,
            )
        err = stderr or stdout or "EXEC_SCRIPT failed"
        return make_event_result(
            event,
            status=EventStatus.FAIL,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=elapsed,
            summary=f"EXEC_SCRIPT 失败: {err[:200]}",
            error=err[:500],
            raw_response=raw,
        )

    def _wait_ms(self, event, started_at, t0):
        ms = int((event.params or {}).get("duration_ms") or (event.params or {}).get("ms") or 500)
        time.sleep(max(0, min(ms, 60_000)) / 1000.0)
        return make_event_result(
            event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000), summary=f"等待 {ms}ms",
        )

    def _send(
        self,
        event: PlanEvent,
        ctx: ExecutorContext,
        action: str,
        payload: dict[str, Any],
        started_at: str,
        t0: float,
        *,
        summary_ok: str,
        timeout_sec: float = 30.0,
    ) -> EventResult:
        """同步往 ClawNode 下发一个 control 子动作并等结果。

        实现细节：device_manager.handle_control_event / send_clawnode_control 是 async；
        我们用 asyncio.run_coroutine_threadsafe 把它桥到当前同步线程。
        """
        sn = ctx.run_context.sn
        trace_id = f"reg-{uuid.uuid4().hex[:10]}"
        try:
            from server.websocket.device_manager import DeviceManager
            from server.websocket.routers.wClawNode import translate_control_to_clawnode

            manager = DeviceManager()
            ws = manager.active_connections.get(sn)
            if ws is None or sn not in getattr(manager, "direct_nodes", set()):
                return self._decline(event, started_at, t0, "remote ws missing or not authenticated")

            frame_params = {"action": action, "req_id": trace_id, **payload}
            frame = translate_control_to_clawnode(frame_params)
            if not frame:
                return self._fail(event, started_at, t0, f"unknown remote action={action}")

            # 当前线程同步：用 asyncio.run 启个临时 loop 发送（如果在 async 上下文里会 raise）
            try:
                loop = asyncio.get_running_loop()
                inside_loop = True
            except RuntimeError:
                loop = None
                inside_loop = False

            if inside_loop and loop is not None:
                fut = asyncio.run_coroutine_threadsafe(self._await_send(manager, ws, frame), loop)
                ok = fut.result(timeout=10.0)
            else:
                ok = asyncio.run(self._await_send(manager, ws, frame))

            elapsed = int((time.time() - t0) * 1000)
            if ok:
                if action in {"tap", "swipe", "key", "keyevent", "key_event", "press_key", "open_app", "start_app", "launch_app", "input_text"}:
                    from server.services.regression.screen import invalidate_remote_capture_cache
                    invalidate_remote_capture_cache(sn)
                return make_event_result(
                    event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                    elapsed_ms=elapsed, summary=summary_ok,
                    raw_response={"trace_id": trace_id, "action": action},
                )
            return self._fail(event, started_at, t0, f"remote send failed action={action}")
        except Exception as e:
            return self._fail(event, started_at, t0, f"remote send exception: {e}")

    async def _await_send(self, manager, ws, frame: dict[str, Any]) -> bool:
        try:
            return bool(await manager._safe_send(ws, frame))  # noqa: SLF001
        except Exception:
            return False

    def _decline(self, event, started_at, t0, msg: str) -> EventResult:
        return make_event_result(
            event, status=EventStatus.DECLINED, executor_used=self.id, started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000), summary=msg, error=msg,
        )

    def _fail(self, event, started_at, t0, msg: str) -> EventResult:
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000), summary=msg, error=msg,
        )
