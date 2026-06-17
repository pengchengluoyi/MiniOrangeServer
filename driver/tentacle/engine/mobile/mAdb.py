# !/usr/bin/env python
# -*-coding:utf-8 -*-
import re
import time
import subprocess
import xml.etree.ElementTree as ET
from io import BytesIO
from typing import Any, Dict, Optional

from PIL import Image

from script.log import SLog
from driver.tentacle.common.mPath import get_adb_path
from driver.tentacle.engine.mobile.mobile_engine import MobileEngine
from driver.tentacle.engine.mobile.adb_locator import AdbLocator

TAG = "MAdbEngine"

_ANDROID_KEY_MAP = {
    "home": 3,
    "back": 4,
    "menu": 82,
    "power": 26,
    "volume_up": 24,
    "volume_down": 25,
    "enter": 66,
}


class MAdbEngine(MobileEngine):

    def init_driver(self, test_subject=None):
        serial = test_subject or getattr(self, "_test_subject", None) or getattr(
            self, "_serial", None
        )
        if self.driver is not None and self._serial == serial:
            return
        if self.driver is not None:
            self._u2 = None
            self.driver = None

        self.adb_exe_path = get_adb_path()
        self._serial = serial
        self._test_subject = serial
        self._u2 = None
        self._input_mode = "unknown"  # full | accessibility | none
        self.adb_base = (
            f"{self.adb_exe_path} -s {serial}" if serial else self.adb_exe_path
        )
        self.driver = "Android_Driver_Active"

    def _ensure_u2(self):
        """Android 14+ 常拒绝 adb shell input；优先用 uiautomator2 注入触控。"""
        if self._u2 is not None:
            return self._u2
        if not self._serial:
            return None
        try:
            import uiautomator2 as u2

            self._u2 = u2.connect(self._serial)
            SLog.i(TAG, f"uiautomator2 connected serial={self._serial}")
            return self._u2
        except Exception as e:
            SLog.e(
                TAG,
                f"uiautomator2 connect failed ({self._serial}): {e}. "
                "请在手机上执行: python -m uiautomator2 init",
            )
            return None

    @staticmethod
    def _is_inject_denied(err: Exception) -> bool:
        msg = str(err)
        return "SecurityException" in msg or "INJECT_EVENTS" in msg

    def _accessibility_scroll(self, direction: str) -> bool:
        """通过无障碍滚动（小米/澎湃上常比坐标 swipe 更可用）。"""
        d = self._ensure_u2()
        if not d:
            return False

        def _scroll_one(scrollable) -> bool:
            if direction in ("left", "right"):
                if direction == "left":
                    scrollable.scroll.horiz.forward(steps=24)
                else:
                    scrollable.scroll.horiz.backward(steps=24)
            elif direction == "up":
                scrollable.scroll.vert.forward(steps=18)
            else:
                scrollable.scroll.vert.backward(steps=18)
            return True

        try:
            base = d(scrollable=True)
            if base.exists(timeout=1.5):
                count = getattr(base, "count", 1) or 1
                for idx in range(min(int(count), 6)):
                    try:
                        target = base[idx] if count > 1 else base
                        if _scroll_one(target):
                            return True
                    except Exception:
                        continue
            fallback = d(classNameMatches=".*(ScrollView|RecyclerView|ListView).*")
            if fallback.exists(timeout=1):
                if _scroll_one(fallback):
                    return True
        except Exception as e:
            SLog.w(TAG, f"accessibility scroll {direction} failed: {e}")
        return False

    def probe_input_mode(self) -> str:
        """检测触控能力：full=坐标注入, accessibility=仅无障碍滚动/点击, none=不可用。"""
        d = self._ensure_u2()
        if not d:
            self._input_mode = "none"
            return self._input_mode
        w, h = self._display_size()
        try:
            d.swipe(w // 2, h // 2, w // 2 + 12, h // 2, 0.08)
            self._input_mode = "full"
            return self._input_mode
        except Exception as e:
            if not self._is_inject_denied(e):
                self._input_mode = "accessibility"
                return self._input_mode
        if self._accessibility_scroll("left"):
            self._input_mode = "accessibility"
            SLog.w(
                TAG,
                "坐标注入被系统拒绝，已切换无障碍滚动模式。"
                "小米/红米请打开：开发者选项→USB调试(安全设置)、无障碍→ATX/uiautomator",
            )
            return self._input_mode
        self._input_mode = "none"
        return self._input_mode

    def ensure_input_ready(self) -> None:
        """跑图/自动化前确认至少有一种可用操控方式。"""
        if self._input_mode in ("full", "accessibility"):
            return
        if self._ensure_u2() is None:
            import sys
            import subprocess

            SLog.i(TAG, f"Installing uiautomator2 agent on {self._serial} ...")
            self._u2 = None
            try:
                subprocess.run(
                    [sys.executable, "-m", "uiautomator2", "init", "-s", self._serial],
                    timeout=180,
                    check=False,
                )
            except Exception as e:
                SLog.w(TAG, f"uiautomator2 init command failed: {e}")
            self._u2 = None
            if self._ensure_u2() is None:
                raise RuntimeError(
                    f"无法建立 uiautomator2 连接（{self._serial}）。"
                    "请手动执行: python -m uiautomator2 init -s <设备序列号>"
                )
        mode = self.probe_input_mode()
        if mode == "none":
            raise RuntimeError(
                "本机禁止注入触控事件（小米/澎湃常见）。请依次检查：\n"
                "1. 开发者选项 → 开启「USB 调试(安全设置)」\n"
                "2. 设置 → 无障碍 → 已下载的应用/服务 → 开启 ATX 或 uiautomator\n"
                "3. 关闭「权限监控」后重试跑图"
            )

    def shell(self, cmd):
        full_cmd = f"{self.adb_base} shell {cmd}"
        try:
            return subprocess.check_output(full_cmd, shell=True).decode(
                "utf-8", errors="ignore"
            ).strip()
        except Exception:
            return ""

    def screen_size(self) -> tuple[int, int]:
        return self._display_size()

    def _display_size(self) -> tuple[int, int]:
        out = self.shell("wm size")
        m = re.search(r"(\d+)x(\d+)", out or "")
        if m:
            return int(m.group(1)), int(m.group(2))
        return 1080, 1920

    def screen_on(self):
        d = self._ensure_u2()
        if d:
            try:
                d.screen_on()
                return
            except Exception:
                pass
        self.shell("input keyevent 224")

    @staticmethod
    def _is_mostly_black_image(img, *, threshold: float = 18.0) -> bool:
        """截图均值亮度极低时视为黑屏/息屏。"""
        if img is None:
            return True
        try:
            gray = img.convert("L")
            hist = gray.histogram()
            pixels = sum(hist) or 1
            mean = sum(i * c for i, c in enumerate(hist)) / pixels
            return mean < threshold
        except Exception:
            return False

    def _device_unlock_password(self, node_sn: Optional[str] = None) -> Optional[str]:
        """从 m_device / Memory 读取锁屏密码。"""
        candidates = []
        for key in (node_sn, self._serial, getattr(self, "_test_subject", None)):
            if key and str(key) not in candidates:
                candidates.append(str(key))
        try:
            from server.services.device_service import DeviceService

            for key in candidates:
                pwd = DeviceService.get_password(key)
                if pwd:
                    return pwd
        except Exception:
            pass
        try:
            import builtins
            from driver.agent.Memory import memory_manager

            for key in candidates:
                mem = memory_manager.short_term.get_global(f"{key}_password")
                if mem:
                    return str(mem).strip() or None
            node = getattr(builtins, "TARGET_DEVICE_SN", None)
            if node:
                mem = memory_manager.short_term.get_global(f"{node}_password")
                if mem:
                    return str(mem).strip() or None
        except Exception:
            pass
        return None

    def _is_keyguard_showing(self) -> bool:
        out = self.shell("dumpsys window") or ""
        markers = (
            "mDreamingLockscreen=true",
            "mShowingLockscreen=true",
            "mKeyguardShowing=true",
            "isStatusBarKeyguard=true",
            "KeyguardShowing=true",
        )
        if any(m in out for m in markers):
            return True
        d = self._ensure_u2()
        if not d:
            return False
        try:
            for text in ("紧急呼叫", "Emergency call", "滑动解锁", "上滑解锁"):
                if d(textContains=text).exists(timeout=0.25):
                    return True
        except Exception:
            pass
        return False

    def _wake_display(self) -> None:
        self.screen_on()
        self.shell("input keyevent 224")
        time.sleep(0.35)
        d = self._ensure_u2()
        if d:
            try:
                info = d.info or {}
                if not info.get("screenOn", True):
                    self.shell("input keyevent 26")
                    time.sleep(0.35)
                    d.screen_on()
                    time.sleep(0.35)
            except Exception:
                pass

    def _pin_keypad_visible(self, d) -> bool:
        """锁屏 PIN 数字键盘是否已露出（上滑/切密码后）。"""
        if not d:
            return False
        for spec in ({"text": "1"}, {"description": "1"}, {"text": "0"}):
            try:
                if d(**spec).exists(timeout=0.35):
                    return True
            except Exception:
                continue
        return False

    def _needs_swipe_up_hint(self, d) -> bool:
        """锁屏是否仍提示需要上滑。"""
        if not d:
            return True
        for label in (
            "上滑",
            "向上滑",
            "滑动解锁",
            "滑动手势",
            "Swipe",
            "swipe",
            "向上滑动",
        ):
            try:
                if d(textContains=label).exists(timeout=0.25):
                    return True
            except Exception:
                continue
        return False

    def _swipe_up_to_pin_entry(self, d, w: int, h: int, *, max_rounds: int = 3) -> None:
        """多数系统需上滑后才出现 PIN/密码输入界面。"""
        for i in range(max_rounds):
            if d and self._pin_keypad_visible(d) and not self._needs_swipe_up_hint(d):
                if i:
                    SLog.i(
                        TAG,
                        f"keyguard pin pad visible after swipe={i} sn={self._serial}",
                    )
                return
            SLog.i(TAG, f"keyguard swipe-up round={i + 1} sn={self._serial}")
            try:
                if d:
                    d.swipe(w // 2, int(h * 0.93), w // 2, int(h * 0.08), 0.22)
                else:
                    self.swipe_norm(0.5, 0.93, 0.5, 0.08, 0.25)
            except Exception as e:
                SLog.w(TAG, f"keyguard swipe-up failed: {e}")
            time.sleep(0.32)
            self._switch_to_password_entry(d)
            time.sleep(0.15)

    def _switch_to_password_entry(self, d) -> None:
        """指纹/人脸锁屏上切到密码输入（MIUI / 原生常见文案）。"""
        if not d:
            return
        for label in ("密码解锁", "使用密码", "输入密码", "数字密码", "密码", "PIN"):
            try:
                o = d(textContains=label)
                if o.exists(timeout=0.5):
                    o.click()
                    time.sleep(0.45)
                    return
            except Exception:
                continue

    _PIN_GRID_NORM = {
        "1": (0.25, 0.58),
        "2": (0.50, 0.58),
        "3": (0.75, 0.58),
        "4": (0.25, 0.66),
        "5": (0.50, 0.66),
        "6": (0.75, 0.66),
        "7": (0.25, 0.74),
        "8": (0.50, 0.74),
        "9": (0.75, 0.74),
        "0": (0.50, 0.82),
    }

    def _tap_pin_grid_fallback(self, d, pwd: str, w: int, h: int) -> bool:
        """标准 3×4 PIN 盘坐标兜底（MIUI 数字键常无 text）。"""
        if not pwd.isdigit():
            return False
        for ch in pwd:
            pos = self._PIN_GRID_NORM.get(ch)
            if not pos:
                return False
            x, y = int(w * pos[0]), int(h * pos[1])
            try:
                if d:
                    d.click(x, y)
                else:
                    self.shell(f"input tap {x} {y}")
            except Exception:
                self.shell(f"input tap {x} {y}")
            time.sleep(0.07)
        time.sleep(0.35)
        return not self._is_keyguard_showing()

    def _tap_lock_screen_keypad(self, d, pwd: str, *, w: int = 0, h: int = 0) -> bool:
        """点击锁屏上的数字键盘（不依赖 focused 输入框）。"""
        if not d or not pwd.isdigit():
            return False
        if w <= 0 or h <= 0:
            w, h = self._display_size()
        for ch in pwd:
            clicked = False
            for spec in (
                {"text": ch},
                {"textMatches": f"^{ch}$"},
                {"description": ch},
            ):
                try:
                    o = d(**spec)
                    if o.exists(timeout=0.38):
                        o.click()
                        clicked = True
                        time.sleep(0.07)
                        break
                except Exception:
                    continue
            if not clicked:
                SLog.i(TAG, f"keypad digit missing ch={ch!r}, swipe-up retry sn={self._serial}")
                self._swipe_up_to_pin_entry(d, w, h, max_rounds=2)
                self._switch_to_password_entry(d)
                for spec in (
                    {"text": ch},
                    {"textMatches": f"^{ch}$"},
                    {"description": ch},
                ):
                    try:
                        o = d(**spec)
                        if o.exists(timeout=0.38):
                            o.click()
                            clicked = True
                            time.sleep(0.07)
                            break
                    except Exception:
                        continue
            if not clicked:
                return False
        time.sleep(0.35)
        for confirm in ("确认", "完成", "解锁", "OK", "Enter"):
            try:
                o = d(text=confirm)
                if o.exists(timeout=0.4):
                    o.click()
                    time.sleep(0.4)
                    break
            except Exception:
                continue
        time.sleep(0.5)
        return not self._is_keyguard_showing()

    def _send_unlock_password(self, pwd: str) -> bool:
        """锁屏密码：屏上键盘 → u2.press → adb keyevent → input text（不用 send_keys）。"""
        if not pwd:
            return False
        d = self._ensure_u2()
        w, h = self._display_size()

        if d and pwd.isdigit():
            if not self._pin_keypad_visible(d) or self._needs_swipe_up_hint(d):
                self._swipe_up_to_pin_entry(d, w, h)
            self._switch_to_password_entry(d)
            if not self._pin_keypad_visible(d):
                try:
                    d.click(w // 2, int(h * 0.55))
                    time.sleep(0.15)
                except Exception:
                    pass
        elif d:
            self._switch_to_password_entry(d)

        if pwd.isdigit():
            if d and self._tap_lock_screen_keypad(d, pwd):
                SLog.i(TAG, f"keyguard unlock ok via keypad sn={self._serial}")
                return True

            if d:
                try:
                    for ch in pwd:
                        d.press(ch)
                        time.sleep(0.12)
                    time.sleep(0.35)
                    d.press("enter")
                    time.sleep(0.8)
                    if not self._is_keyguard_showing():
                        SLog.i(TAG, f"keyguard unlock ok via press sn={self._serial}")
                        return True
                except Exception as e:
                    SLog.w(TAG, f"unlock press failed: {e}")

            if self._tap_pin_grid_fallback(d, pwd, w, h):
                SLog.i(TAG, f"keyguard unlock ok via pin grid sn={self._serial}")
                return True

            try:
                SLog.i(TAG, f"keyguard unlock: keyevents sn={self._serial}")
                for ch in pwd:
                    self.shell(f"input keyevent {7 + int(ch)}")
                    time.sleep(0.12)
                time.sleep(0.35)
                self.shell("input keyevent 66")
                time.sleep(0.8)
                if not self._is_keyguard_showing():
                    SLog.i(TAG, f"keyguard unlock ok via keyevents sn={self._serial}")
                    return True
            except Exception as e:
                SLog.w(TAG, f"unlock keyevents failed: {e}")

        try:
            SLog.i(TAG, f"keyguard unlock: input text sn={self._serial}")
            escaped = pwd.replace(" ", "%s")
            self.shell(f"input text {escaped}")
            time.sleep(0.35)
            self.shell("input keyevent 66")
            time.sleep(0.8)
            if not self._is_keyguard_showing():
                SLog.i(TAG, f"keyguard unlock ok via input text sn={self._serial}")
                return True
        except Exception as e:
            SLog.w(TAG, f"unlock input text failed: {e}")

        still = self._is_keyguard_showing()
        if still:
            SLog.w(TAG, f"keyguard unlock failed sn={self._serial}")
        return not still

    def _unlock_keyguard(self, node_sn: Optional[str] = None) -> bool:
        w, h = self._display_size()
        d = self._ensure_u2()
        pwd = self._device_unlock_password(node_sn)

        if d:
            try:
                d.unlock()
            except Exception:
                pass
        self._swipe_up_to_pin_entry(d, w, h)

        if pwd:
            SLog.i(TAG, f"keyguard unlock: entering password len={len(pwd)} sn={self._serial}")
            self._send_unlock_password(pwd)
        else:
            SLog.w(
                TAG,
                f"keyguard visible but no password for sn={self._serial}; "
                "请在设备管理配置锁屏密码",
            )

        return not self._is_keyguard_showing()

    def ensure_screen_ready(self, node_sn: Optional[str] = None) -> bool:
        """唤醒并解锁屏幕，避免黑屏/锁屏导致 OCR·点击失败。"""
        self._wake_display()
        max_attempts = 5 if self._device_unlock_password(node_sn) else 3
        for attempt in range(max_attempts):
            shot = self.screenshot()
            blank = self._is_mostly_black_image(shot)
            locked = self._is_keyguard_showing()
            if not blank and not locked:
                if attempt:
                    SLog.i(TAG, f"screen ready after wake attempt={attempt} sn={self._serial}")
                return True
            SLog.i(
                TAG,
                f"screen not ready sn={self._serial} attempt={attempt} "
                f"blank={blank} locked={locked}",
            )
            self._wake_display()
            # 仅锁屏时解锁；blank 可能是键盘/转场瞬时黑帧，误触会反复输入锁屏密码
            if locked:
                self._unlock_keyguard(node_sn)
            time.sleep(0.35)
        ok = not self._is_keyguard_showing() and not self._is_mostly_black_image(
            self.screenshot()
        )
        if not ok:
            SLog.w(TAG, f"screen still not ready sn={self._serial}")
        return ok

    def _audit_gesture_begin(self, kind: str, summary: str, **extra) -> Optional[Dict]:
        try:
            from server.services.shared.run_context.regression_run_context import record_gesture

            return record_gesture(
                kind,
                summary,
                source="engine",
                x=int(extra.get("x") or 0),
                y=int(extra.get("y") or 0),
                label=str(extra.get("label") or ""),
                method=str(extra.get("method") or kind),
            )
        except Exception:
            return None

    @staticmethod
    def _audit_gesture_end(entry: Optional[Dict], *, ok: bool = True, msg: str = "") -> None:
        if not entry:
            return
        try:
            from server.services.shared.run_context.regression_run_context import finish_gesture

            finish_gesture(entry, ok=ok, msg=msg)
        except Exception:
            pass

    def swipe_norm(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        duration: float = 0.5,
    ):
        w, h = self._display_size()
        fx, fy = int(w * x1), int(h * y1)
        tx, ty = int(w * x2), int(h * y2)
        audit = self._audit_gesture_begin(
            "swipe",
            f"滑动 ({fx},{fy})→({tx},{ty})",
            x=fx,
            y=fy,
            method="swipe_norm",
        )
        d = self._ensure_u2()
        ok = False
        try:
            if d and self._input_mode != "accessibility":
                try:
                    d.swipe(fx, fy, tx, ty, float(duration))
                    ok = True
                except Exception as e:
                    if self._is_inject_denied(e):
                        self._input_mode = "accessibility"
                        SLog.w(TAG, "坐标 swipe 被拒绝，已切换无障碍滚动")
                    elif not self._is_inject_denied(e):
                        SLog.w(TAG, f"u2 swipe failed: {e}")
            if not ok and self._input_mode in ("accessibility", "full", "unknown"):
                dx, dy = tx - fx, ty - fy
                if abs(dx) >= abs(dy):
                    direction = "left" if dx < 0 else "right"
                else:
                    direction = "up" if dy < 0 else "down"
                if self._accessibility_scroll(direction):
                    ok = True
            if not ok and self._input_mode == "full":
                ms = max(int(float(duration) * 1000), 100)
                self.shell(f"input swipe {fx} {fy} {tx} {ty} {ms}")
                ok = True
        finally:
            self._audit_gesture_end(audit, ok=ok)

    def press_key(self, event: str):
        if not event:
            return
        name = str(event).lower()
        audit = self._audit_gesture_begin(
            "back" if name == "back" else "key",
            f"按键 {name!r}",
            method="press_key",
            label=name,
        )
        SLog.i(TAG, f"Gesture audit press_key {name!r} serial={self._serial}")
        ok = False
        d = self._ensure_u2()
        try:
            if d and name in ("back", "home", "menu", "power"):
                try:
                    d.press(name)
                    ok = True
                    return
                except Exception as e:
                    SLog.w(TAG, f"u2 press {name} failed: {e}")
            code = _ANDROID_KEY_MAP.get(name)
            if code is not None:
                self.shell(f"input keyevent {code}")
                ok = True
            elif str(event).isdigit():
                self.shell(f"input keyevent {event}")
                ok = True
            else:
                self.shell(f"input keyevent {str(event).upper()}")
                ok = True
        finally:
            self._audit_gesture_end(audit, ok=ok)

    def build_chain(self, locator_chain):
        return AdbLocator(self, locator_chain)

    def current_package(self) -> str:
        d = self._ensure_u2()
        if d:
            try:
                info = d.app_current() or {}
                pkg = str(info.get("package") or "").strip()
                if pkg:
                    return pkg
            except Exception as e:
                SLog.w(TAG, f"u2 app_current failed: {e}")
        try:
            out = self.shell("dumpsys activity activities") or ""
            for pat in (
                r"mResumedActivity.*?\{[^}]*\s([a-zA-Z0-9_.]+)/",
                r"mFocusedApp.*?(?:ActivityRecord|App).*?([a-zA-Z0-9_.]+)/",
            ):
                m = re.search(pat, out)
                if m:
                    return m.group(1)
        except Exception as e:
            SLog.w(TAG, f"shell current package failed: {e}")
        return ""

    def start_app(self, package_name=None):
        if not package_name:
            return False
        package_name = package_name.strip().replace("\u200c", "")
        p_name = "".join(c for c in package_name if c.isprintable()).strip()
        d = self._ensure_u2()
        if d:
            try:
                d.app_start(p_name, stop=True)
                time.sleep(1.0)
                launched = self.current_package()
                if launched and (
                    launched == p_name or p_name in launched or launched in p_name
                ):
                    return True
                SLog.w(
                    TAG,
                    f"u2 app_start done but foreground={launched or '-'}, expected={p_name}",
                )
            except Exception as e:
                SLog.w(TAG, f"u2 app_start failed, fallback monkey: {e}")
        rc = self.shell(
            f"monkey -p {p_name} -c android.intent.category.LAUNCHER 1; echo $?"
        )
        time.sleep(1.5)
        launched = self.current_package()
        if launched and (
            launched == p_name or p_name in launched or launched in p_name
        ):
            return True
        if rc and "0" in str(rc).splitlines()[-1:]:
            SLog.w(TAG, f"monkey returned ok but foreground={launched or '-'}")
        return False

    def stop_app(self, package_name=None):
        if package_name:
            self.shell(f"am force-stop {package_name}")
        return True

    def screenshot(self, path=None):
        try:
            cmd = f"{self.adb_base} exec-out screencap -p"
            img_bytes = subprocess.check_output(cmd, shell=True)
            if not img_bytes:
                return None
            img = Image.open(BytesIO(img_bytes))
            if path:
                img.save(path)
                return path
            return img
        except Exception as e:
            SLog.e(TAG, f"截图失败: {e}")
            return None

    def dump_hierarchy_xml(self) -> str:
        """优先 uiautomator2 dump（不依赖 /sdcard/view.xml）。"""
        d = self._ensure_u2()
        if d:
            try:
                xml = d.dump_hierarchy(compressed=False, pretty=False)
                if xml and "<?xml" in xml:
                    return xml
            except Exception as e:
                SLog.w(TAG, f"u2 dump_hierarchy failed: {e}")
        try:
            self.shell("uiautomator dump /sdcard/view.xml")
            return self.shell("cat /sdcard/view.xml") or ""
        except Exception:
            return ""

    def swipe_in_rect(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        direction: str = "up",
        duration: float = 0.35,
    ) -> None:
        """在热区矩形内滑动（同页探索用）。"""
        sw, sh = self._display_size()
        if sw <= 0 or sh <= 0:
            return
        margin = 0.12
        cx = (x + w // 2) / sw
        if direction in ("up", "down"):
            y_top = (y + h * margin) / sh
            y_bot = (y + h * (1 - margin)) / sh
            if direction == "up":
                self.swipe_norm(cx, y_bot, cx, y_top, duration)
            else:
                self.swipe_norm(cx, y_top, cx, y_bot, duration)
        else:
            x_left = (x + w * margin) / sw
            x_right = (x + w * (1 - margin)) / sw
            cy = (y + h // 2) / sh
            if direction == "left":
                self.swipe_norm(x_right, cy, x_left, cy, duration)
            else:
                self.swipe_norm(x_left, cy, x_right, cy, duration)

    def find_element(self, locator_chain=None):
        locator_chain = locator_chain or []
        try:
            xml_data = self.dump_hierarchy_xml()
        except Exception as e:
            SLog.e(TAG, f"XML 解析失败: {e}")
            return None
        if not xml_data or "<?xml" not in xml_data:
            return None
        try:
            root = ET.fromstring(xml_data)
        except Exception as e:
            SLog.e(TAG, f"XML 解析失败: {e}")
            return None

        attr_map = {
            "id": "resource-id",
            "text": "text",
            "type": "class",
            "desc": "content-desc",
            "resourceId": "resource-id",
            "classname": "class",
            "description": "content-desc",
        }
        target_node = None
        for condition in locator_chain:
            filters = {
                attr_map[k]: v
                for k, v in condition.items()
                if k in attr_map and v
            }
            for node in root.iter("node"):
                if all(node.get(k) == v for k, v in filters.items()):
                    target_node = node
                    break

        if target_node is not None:
            nums = re.findall(r"\d+", target_node.get("bounds") or "")
            if len(nums) == 4:
                x1, y1, x2, y2 = map(int, nums)
                return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

    @staticmethod
    def _label_variants(label: str) -> list:
        raw = str(label or "").strip()
        if not raw:
            return []
        out: list = []
        for cand in (
            raw,
            re.sub(r"(按钮|按键|图标|入口|菜单)$", "", raw).strip(),
            re.sub(r"(按钮|按键|图标|入口|菜单|tab|Tab|TAB)$", "", raw, flags=re.I).strip(),
        ):
            if cand and cand not in out:
                out.append(cand)
        return out

    def click_by_label(
        self,
        label: str,
        *,
        exact_only: bool = False,
        allow_legal: bool = False,
    ) -> bool:
        """优先用无障碍节点点击（不依赖 INJECT_EVENTS）。"""
        if not label:
            return False
        legal = any(k in label for k in ("用户协议", "隐私条款", "隐私政策", "服务协议"))
        if legal and not allow_legal:
            SLog.w(TAG, f"click_by_label blocked legal link label={label!r}")
            return False
        if any(k in label for k in ("同意", "同意并继续")) and not exact_only:
            exact_only = True
        d = self._ensure_u2()
        if not d:
            return False
        if any(k in label for k in ("同意", "同意并继续")):
            try:
                from server.services.local.navigation.page_navigation_service import (
                    _pick_u2_consent_agree_candidate,
                )

                w, h = self._display_size()
                picked = _pick_u2_consent_agree_candidate(d, w, h)
                if picked:
                    node, cx, cy, matched = picked
                    audit = self._audit_gesture_begin(
                        "click",
                        f"点击「{matched}」",
                        label=matched,
                        method="click_by_label",
                        x=cx,
                        y=cy,
                    )
                    ok = False
                    try:
                        node.click()
                        ok = True
                    finally:
                        if audit is not None:
                            half = 44
                            audit["target_rect"] = {
                                "left": max(0, cx - half),
                                "top": max(0, cy - half),
                                "width": half * 2,
                                "height": half * 2,
                                "center": [cx, cy],
                                "label": matched,
                            }
                            audit["screen_size"] = {"w": w, "h": h}
                        self._audit_gesture_end(
                            audit,
                            ok=ok,
                            msg=f"Tap「{matched}」@({cx},{cy}) [click_by_label]"
                            if ok
                            else "click_by_label failed",
                        )
                    return ok
            except Exception as e:
                SLog.w(TAG, f"consent click_by_label candidate pick failed: {e}")
        try:
            for text in self._label_variants(label):
                selectors = (
                    (d(text=text), d(description=text))
                    if exact_only
                    else (
                        d(text=text),
                        d(textContains=text),
                        d(description=text),
                        d(descriptionContains=text),
                    )
                )
                for sel in selectors:
                    if not sel.exists(timeout=0.8):
                        continue
                    node_text = ""
                    bounds = None
                    try:
                        info = sel.info or {}
                        node_text = (info.get("text") or info.get("contentDescription") or "").strip()
                        bounds = info.get("bounds") or {}
                    except Exception:
                        node_text = ""
                    if node_text and any(
                        k in node_text for k in ("用户协议", "隐私条款", "隐私政策", "服务协议")
                    ):
                        if not allow_legal and any(k in label for k in ("同意", "登录", "一键")):
                            SLog.w(
                                TAG,
                                f"click_by_label skip legal-bearing node={node_text!r} "
                                f"wanted={label!r}",
                            )
                            continue
                    tap_x, tap_y = 0, 0
                    if bounds:
                        tap_x = (
                            int(bounds.get("left", 0)) + int(bounds.get("right", 0))
                        ) // 2
                        tap_y = (
                            int(bounds.get("top", 0)) + int(bounds.get("bottom", 0))
                        ) // 2
                    SLog.i(
                        TAG,
                        f"Gesture audit click_by_label label={label!r} node={node_text!r} "
                        f"exact={exact_only} @({tap_x},{tap_y})",
                    )
                    audit = self._audit_gesture_begin(
                        "click",
                        f"点击「{node_text or label}」",
                        label=label or node_text,
                        method="click_by_label",
                        x=tap_x,
                        y=tap_y,
                    )
                    ok = False
                    try:
                        sel.click()
                        ok = True
                    finally:
                        if audit is not None and tap_x > 0 and tap_y > 0:
                            half = 44
                            w, h = self._display_size()
                            audit["target_rect"] = {
                                "left": max(0, tap_x - half),
                                "top": max(0, tap_y - half),
                                "width": half * 2,
                                "height": half * 2,
                                "center": [tap_x, tap_y],
                                "label": label or node_text,
                            }
                            audit["screen_size"] = {"w": w, "h": h}
                        self._audit_gesture_end(
                            audit,
                            ok=ok,
                            msg=(
                                f"Tap「{node_text or label}」@({tap_x},{tap_y}) [click_by_label]"
                                if ok and tap_x > 0 and tap_y > 0
                                else ("click_by_label ok" if ok else "click_by_label failed")
                            ),
                        )
                    return ok
        except Exception as e:
            SLog.w(TAG, f"accessibility click '{label}' failed: {e}")
        return False

    def _node_at_point(self, x: int, y: int):
        """返回坐标处面积最小的 clickable 节点 (node, text)。"""
        try:
            xml_data = self.dump_hierarchy_xml() or ""
            if not xml_data or "<?xml" not in xml_data:
                self.shell("uiautomator dump /sdcard/view.xml")
                xml_data = self.shell("cat /sdcard/view.xml")
            if not xml_data or "<?xml" not in xml_data:
                return None, ""
            root = ET.fromstring(xml_data)
            best = None
            best_area = None
            for node in root.iter("node"):
                if node.get("clickable") != "true":
                    continue
                nums = re.findall(r"\d+", node.get("bounds") or "")
                if len(nums) != 4:
                    continue
                x1, y1, x2, y2 = map(int, nums)
                if x1 <= x <= x2 and y1 <= y <= y2:
                    area = (x2 - x1) * (y2 - y1)
                    if best is None or area < best_area:
                        best = node
                        best_area = area
            if best is None:
                return None, ""
            text = (best.get("text") or best.get("content-desc") or "").strip()
            return best, text
        except Exception:
            return None, ""

    def _coordinate_targets_legal_link(self, x: int, y: int) -> bool:
        _, text = self._node_at_point(x, y)
        if not text:
            return False
        return any(k in text for k in ("用户协议", "隐私条款", "隐私政策", "服务协议", "《", "》"))

    def _click_accessibility_at(self, x: int, y: int, *, allow_legal_link: bool = False) -> bool:
        """无障碍模式下：找包含该坐标的 clickable 节点再点。"""
        try:
            best, text = self._node_at_point(x, y)
            if best is None:
                return False
            nums = re.findall(r"\d+", best.get("bounds") or "")
            if len(nums) != 4:
                return False
            x1, y1, x2, y2 = map(int, nums)
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            is_legal = any(k in text for k in ("用户协议", "隐私条款", "隐私政策", "服务协议"))
            if text and not is_legal and self.click_by_label(text, exact_only=True):
                return True
            if text and is_legal and not allow_legal_link:
                SLog.w(TAG, f"accessibility_at blocked legal link {text!r} at ({x},{y})")
                return False
            d = self._ensure_u2()
            if d:
                d.click(cx, cy)
                return True
        except Exception as e:
            SLog.w(TAG, f"accessibility click at ({x},{y}) failed: {e}")
        return False

    def click(
        self,
        element,
        position=None,
        label: str = "",
        *,
        skip_label_lookup: bool = False,
        exact_label: bool = False,
        consent_dismiss: bool = False,
        locate_method: str = "",
        skip_gesture_audit: bool = False,
    ) -> bool:
        target = position if position else element
        x, y = None, None
        if target:
            x, y = int(target[0]), int(target[1])

        consent_action = consent_dismiss or label in ("同意", "同意并继续")
        if consent_action and x is not None and y is not None and self._coordinate_targets_legal_link(x, y):
            SLog.w(
                TAG,
                f"blocked consent tap on legal link ({x},{y}); refusing blind coordinate",
            )
            return False

        # 有明确坐标时优先坐标点击，避免 fuzzy click_by_label 误点《用户协议》等链接
        if x is not None and y is not None and (skip_label_lookup or position is not None):
            tap_summary = f"点击「{label}」@({x},{y})" if label else f"点击 ({x},{y})"
            audit = None
            if not consent_dismiss and not skip_gesture_audit:
                audit = self._audit_gesture_begin(
                    "click",
                    tap_summary,
                    x=int(x),
                    y=int(y),
                    label=label,
                    method=(locate_method or "coordinate").strip() or "coordinate",
                )
            if audit is not None:
                w, h = self._display_size()
                audit["screen_size"] = {"w": w, "h": h}
                audit["target_rect"] = {
                    "left": max(0, int(x) - 44),
                    "top": max(0, int(y) - 44),
                    "width": 88,
                    "height": 88,
                    "center": [int(x), int(y)],
                    "label": label or "",
                }
            ok = False
            try:
                SLog.i(
                    TAG,
                    f"Gesture audit tap ({x},{y}) label={label!r} skip_label={skip_label_lookup} "
                    f"consent={consent_dismiss} serial={self._serial}",
                )
                if self._input_mode == "unknown":
                    self.probe_input_mode()
                d = self._ensure_u2()
                if d and self._input_mode != "accessibility":
                    if consent_action and self._coordinate_targets_legal_link(x, y):
                        return False
                    try:
                        d.click(x, y)
                        ok = True
                        return True
                    except Exception as e:
                        if self._is_inject_denied(e):
                            self._input_mode = "accessibility"
                        else:
                            SLog.w(TAG, f"u2 click failed: {e}")
                if self._input_mode == "full":
                    if consent_action and self._coordinate_targets_legal_link(x, y):
                        return False
                    self.shell(f"input tap {x} {y}")
                    ok = True
                    return True
                if self._input_mode == "accessibility":
                    allow_legal = bool(
                        label
                        and any(k in label for k in ("用户协议", "隐私条款", "隐私政策", "服务协议"))
                    )
                    if self._click_accessibility_at(x, y, allow_legal_link=allow_legal):
                        ok = True
                        return True
                if consent_action:
                    SLog.w(TAG, f"consent tap refused fallback input at ({x},{y})")
                    return False
                if self._serial:
                    self.shell(f"input tap {x} {y}")
                    ok = True
                    return True
                SLog.e(TAG, f"coordinate click failed at ({x},{y})")
                return False
            finally:
                if audit:
                    self._audit_gesture_end(audit, ok=ok)

        if label and not skip_label_lookup and self.click_by_label(label, exact_only=exact_label):
            SLog.i(TAG, f"click via label lookup {label!r}")
            return True
        if not target:
            return False

        if self._input_mode == "unknown":
            self.probe_input_mode()

        d = self._ensure_u2()
        if d and self._input_mode != "accessibility":
            try:
                d.click(x, y)
                return True
            except Exception as e:
                if self._is_inject_denied(e):
                    self._input_mode = "accessibility"
                else:
                    SLog.w(TAG, f"u2 click failed: {e}")

        if self._input_mode == "full":
            self.shell(f"input tap {x} {y}")
            return True

        if self._input_mode == "accessibility":
            if self._click_accessibility_at(x, y, allow_legal_link=False):
                return True
            if label and self.click_by_label(label, exact_only=exact_label):
                return True

        if self._serial:
            self.shell(f"input tap {x} {y}")
            if self._input_mode != "none":
                return True

        SLog.e(
            TAG,
            f"click failed at ({x},{y}) label={label!r} input_mode={self._input_mode}",
        )
        return False

    def send_keys(self, element, text):
        if element:
            self.click(element)
            time.sleep(0.5)
        safe_text = str(text).replace(" ", "%s")
        self.shell(f"input text {safe_text}")

    def drag_and_drop(self, source, target):
        if source and target:
            SLog.i(TAG, f"执行滑动: {source} -> {target}")
            d = self._ensure_u2()
            if d:
                try:
                    d.swipe(
                        int(source[0]), int(source[1]),
                        int(target[0]), int(target[1]),
                        0.5,
                    )
                    return
                except Exception:
                    pass
            self.shell(
                f"input swipe {source[0]} {source[1]} {target[0]} {target[1]} 500"
            )

    def keyevent(self, event):
        self.press_key(str(event))

    def close_window(self, target):
        self.stop_app(target)
