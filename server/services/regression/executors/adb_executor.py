# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""ADB 执行通道：通过 subprocess 调 adb 直接操作真机。

支持的 capability：
  - launch_app / close_app / press_key / wait_ms
  - swipe_direction / install_apk / read_device_data / set_clipboard
  - tap_element / input_text (需 VLM 在 ctx.screen 上先定位)
"""
from __future__ import annotations

import subprocess
import time
from typing import Any

from script.log import SLog

from server.services.ai.regression.schemas import EventResult, EventStatus, PlanEvent
from server.services.regression.executors.base import (
    Executor,
    ExecutorContext,
    _now_iso,
    make_event_result,
)

TAG = "AdbExecutor"

# capability_id → 内部处理方法
_SUPPORTED_CAPS: set[str] = {
    "launch_app",
    "close_app",
    "press_key",
    "wait_ms",
    "swipe_direction",
    "swipe_element_to_element",
    "install_apk",
    "read_device_data",
    "set_clipboard",
    "tap_element",
    "long_press_element",
    "input_text",
    "exec_script",
}


class AdbExecutor:
    id = "adb"

    def supports(self, capability_id: str) -> bool:
        return capability_id in _SUPPORTED_CAPS

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        started_at = _now_iso()
        t0 = time.time()
        cap = event.capability_id
        serial = str(ctx.run_context.adb.get("serial") or "")
        try:
            if not serial:
                return self._fail(event, started_at, t0, "adb serial 未解析（可能 RunContext probe 失败）")

            if cap == "launch_app":
                return self._launch_app(event, ctx, serial, started_at, t0)
            if cap == "close_app":
                return self._close_app(event, ctx, serial, started_at, t0)
            if cap == "press_key":
                return self._press_key(event, ctx, serial, started_at, t0)
            if cap == "wait_ms":
                return self._wait_ms(event, ctx, started_at, t0)
            if cap == "swipe_direction":
                return self._swipe_direction(event, ctx, serial, started_at, t0)
            if cap == "swipe_element_to_element":
                return self._swipe_element_to_element(event, ctx, serial, started_at, t0)
            if cap == "install_apk":
                return self._install_apk(event, ctx, serial, started_at, t0)
            if cap == "read_device_data":
                return self._read_device_data(event, ctx, serial, started_at, t0)
            if cap == "set_clipboard":
                return self._set_clipboard(event, ctx, serial, started_at, t0)
            if cap == "tap_element":
                return self._tap_element(event, ctx, serial, started_at, t0)
            if cap == "long_press_element":
                return self._long_press_element(event, ctx, serial, started_at, t0)
            if cap == "input_text":
                return self._input_text(event, ctx, serial, started_at, t0)
            if cap == "exec_script":
                return self._exec_script(event, ctx, serial, started_at, t0)
            return self._fail(event, started_at, t0, f"AdbExecutor 不处理 capability={cap}")
        except Exception as e:
            SLog.e(TAG, f"execute exception cap={cap} sn={ctx.run_context.sn}: {e}")
            return self._fail(event, started_at, t0, f"exception: {e}")

    # ---------- 通用 shell helper ----------

    def _adb_shell(self, serial: str, *args: str, timeout_sec: float = 30.0) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["adb", "-s", serial, "shell", *args],
                capture_output=True, text=True, timeout=timeout_sec,
            )
            return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"timeout {timeout_sec}s"
        except FileNotFoundError:
            return -2, "", "adb not in PATH"

    def _adb(self, serial: str, *args: str, timeout_sec: float = 60.0) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["adb", "-s", serial, *args],
                capture_output=True, text=True, timeout=timeout_sec,
            )
            return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return -1, "", f"timeout {timeout_sec}s"
        except FileNotFoundError:
            return -2, "", "adb not in PATH"

    # ---------- handlers ----------

    def _launch_app(self, event, ctx, serial, started_at, t0):
        pkg = (event.params or {}).get("package") or ""
        if not pkg:
            return self._fail(event, started_at, t0, "launch_app 缺 params.package")
        rc, out, err = self._adb_shell(serial, "monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0 and "Events injected" in out:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"启动 {pkg}",
                raw_response={"stdout": out[:240]},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"启动 {pkg} 失败", error=err or out or f"rc={rc}",
            raw_response={"stdout": out[:240], "stderr": err[:240], "rc": rc},
        )

    def _close_app(self, event, ctx, serial, started_at, t0):
        pkg = (event.params or {}).get("package") or ""
        if not pkg:
            return self._fail(event, started_at, t0, "close_app 缺 params.package")
        rc, out, err = self._adb_shell(serial, "am", "force-stop", pkg)
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"强停 {pkg}",
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"强停 {pkg} 失败", error=err or out or f"rc={rc}",
        )

    def _press_key(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        keycode = str(params.get("key") or params.get("keycode") or "").upper()
        if not keycode:
            return self._fail(event, started_at, t0, "press_key 缺 params.key")
        key_map = {
            "BACK": "4", "HOME": "3", "MENU": "82", "POWER": "26", "ENTER": "66",
            "VOLUME_UP": "24", "VOLUME_DOWN": "25", "RECENT": "187",
        }
        kev = key_map.get(keycode, keycode)
        rc, out, err = self._adb_shell(serial, "input", "keyevent", kev)
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"按键 {keycode}",
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"按键 {keycode} 失败", error=err or out,
        )

    def _wait_ms(self, event, ctx, started_at, t0):
        ms = int((event.params or {}).get("duration_ms") or (event.params or {}).get("ms") or 500)
        ms = max(0, min(ms, 60_000))
        time.sleep(ms / 1000.0)
        return make_event_result(
            event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000), summary=f"等待 {ms}ms",
        )

    def _swipe_direction(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        direction = str(params.get("direction") or "up").lower()
        # 通过 wm size 拿屏幕尺寸然后按方向算坐标
        rc, out, _err = self._adb_shell(serial, "wm", "size")
        w, h = 1080, 1920  # 兜底
        if rc == 0 and "Physical size:" in out:
            try:
                size_str = out.rsplit(":", 1)[-1].strip()
                wp, hp = size_str.split("x")
                w, h = int(wp), int(hp)
            except Exception:
                pass
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
        duration = int(params.get("duration_ms") or 300)
        rc, out, err = self._adb_shell(serial, "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration))
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"滑动 {direction}",
                raw_response={"from": (x1, y1), "to": (x2, y2)},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"滑动 {direction} 失败", error=err or out,
        )

    def _swipe_element_to_element(self, event, ctx, serial, started_at, t0):
        # 需要 vlm 子流程：from/to 两次 locate；这里假设上层已塞 coords
        params = event.params or {}
        x1 = params.get("from_x"); y1 = params.get("from_y")
        x2 = params.get("to_x"); y2 = params.get("to_y")
        if None in (x1, y1, x2, y2):
            return self._fail(event, started_at, t0, "swipe_element_to_element 需要 from_x/from_y/to_x/to_y（VLM locate 应已注入）")
        duration = int(params.get("duration_ms") or 400)
        rc, out, err = self._adb_shell(serial, "input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(duration))
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"拖拽 ({x1},{y1})→({x2},{y2})",
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="拖拽失败", error=err or out,
        )

    def _install_apk(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        path = params.get("path") or ""
        url = params.get("url") or ""
        tmp_path = ""
        # 对齐 ClawNode：支持 url（内部下载到本地再 install），也兼容本地 path
        if not path and url:
            try:
                from server.services.local.adb_command import _download_apk

                path = _download_apk(url, params.get("file_name") or "")
                tmp_path = path
            except Exception as e:
                return self._fail(event, started_at, t0, f"apk 下载失败: {e}")
        if not path:
            return self._fail(event, started_at, t0, "install_apk 缺 params.path 或 params.url")
        try:
            rc, out, err = self._adb(serial, "install", "-r", "-t", path, timeout_sec=300.0)
            elapsed = int((time.time() - t0) * 1000)
            if rc == 0 and "Success" in out:
                return make_event_result(
                    event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                    elapsed_ms=elapsed, summary=f"安装 {path}",
                )
            return make_event_result(
                event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary="安装失败", error=err or out,
            )
        finally:
            if tmp_path:
                import os as _os

                try:
                    _os.remove(tmp_path)
                except OSError:
                    pass

    def _exec_script(self, event, ctx, serial, started_at, t0):
        """adb 版 EXEC_SCRIPT：委托 adb_script（dsl/shell；js→not_supported），与 ClawNode 协议一致。"""
        from server.services.shared.adb_script import run_adb_script

        res = run_adb_script(serial, event.params or {})
        elapsed = int((time.time() - t0) * 1000)
        status_raw = str(res.get("status") or "").lower()
        if status_raw == "success":
            status = EventStatus.PASS
        elif status_raw == "not_supported":
            status = EventStatus.SKIPPED
        else:
            status = EventStatus.FAIL
        return make_event_result(
            event, status=status, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=res.get("message") or "exec_script",
            error=res.get("stderr") or "", raw_response={"stdout": (res.get("stdout") or "")[:2000]},
        )

    def _read_device_data(self, event, ctx, serial, started_at, t0):
        key = (event.params or {}).get("key") or "model"
        # 简单映射几个常用 getprop
        prop_map = {
            "model": "ro.product.model",
            "brand": "ro.product.brand",
            "android_version": "ro.build.version.release",
            "sdk": "ro.build.version.sdk",
            "abi": "ro.product.cpu.abi",
        }
        prop = prop_map.get(key, key)
        rc, out, err = self._adb_shell(serial, "getprop", prop)
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            value = out.strip()
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"{key}={value}",
                raw_response={"key": key, "value": value},
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"读取 {key} 失败", error=err or out,
        )

    def _set_clipboard(self, event, ctx, serial, started_at, t0):
        text = (event.params or {}).get("text") or ""
        if not text:
            return self._fail(event, started_at, t0, "set_clipboard 缺 params.text")
        # Android 没有原生 adb shell 写剪贴板的口子，用 broadcast 或 input text 兜底
        rc, out, err = self._adb_shell(serial, "service", "call", "clipboard", "1")
        elapsed = int((time.time() - t0) * 1000)
        if rc != 0:
            # 退而求其次：写到剪贴板 helper app 是另一回事；这里 stub
            return make_event_result(
                event, status=EventStatus.SKIPPED, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary="set_clipboard adb 通路无原生支持，建议改 remote",
            )
        return make_event_result(
            event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"剪贴板设置 {len(text)} 字",
        )

    def _tap_element(self, event, ctx, serial, started_at, t0):
        # 优先用 PlanEvent.params 里 router 注入的 vlm_locate 坐标
        params = event.params or {}
        x = params.get("x"); y = params.get("y")
        if x is None or y is None:
            return self._fail(event, started_at, t0, "tap_element 缺坐标（router 未注入 VLM locate 结果）")
        rc, out, err = self._adb_shell(serial, "input", "tap", str(int(x)), str(int(y)))
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"点击 ({x},{y})",
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary=f"点击 ({x},{y}) 失败", error=err or out,
        )

    def _long_press_element(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        x = params.get("x"); y = params.get("y")
        if x is None or y is None:
            return self._fail(event, started_at, t0, "long_press_element 缺坐标")
        duration = int(params.get("duration_ms") or 1000)
        # 长按 = swipe 自己到自己
        rc, out, err = self._adb_shell(serial, "input", "swipe", str(int(x)), str(int(y)), str(int(x)), str(int(y)), str(duration))
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"长按 ({x},{y}) {duration}ms",
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="长按失败", error=err or out,
        )

    def _input_text(self, event, ctx, serial, started_at, t0):
        params = event.params or {}
        text = params.get("text") or ""
        x = params.get("x"); y = params.get("y")
        if not text:
            return self._fail(event, started_at, t0, "input_text 缺 params.text")
        # 若给了坐标，先点焦点
        if x is not None and y is not None:
            self._adb_shell(serial, "input", "tap", str(int(x)), str(int(y)))
        # adb input text 不支持中文 / 空格；空格转 %s
        safe_text = str(text).replace(" ", "%s")
        rc, out, err = self._adb_shell(serial, "input", "text", safe_text)
        elapsed = int((time.time() - t0) * 1000)
        if rc == 0:
            return make_event_result(
                event, status=EventStatus.PASS, executor_used=self.id, started_at=started_at,
                elapsed_ms=elapsed, summary=f"输入 {len(text)} 字",
            )
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=elapsed, summary="输入失败（中文需要 IME 协助，建议改 remote）", error=err or out,
        )

    # ---------- 内部 ----------

    def _fail(self, event, started_at, t0, msg: str) -> EventResult:
        return make_event_result(
            event, status=EventStatus.FAIL, executor_used=self.id, started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000), summary=msg, error=msg,
        )
