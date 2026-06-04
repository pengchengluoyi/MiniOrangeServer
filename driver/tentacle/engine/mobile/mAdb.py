# !/usr/bin/env python
# -*-coding:utf-8 -*-
import re
import time
import subprocess
import xml.etree.ElementTree as ET
from io import BytesIO

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
        if self.driver is not None:
            return
        self.adb_exe_path = get_adb_path()
        self._serial = test_subject
        self._u2 = None
        self._input_mode = "unknown"  # full | accessibility | none
        self.adb_base = (
            f"{self.adb_exe_path} -s {test_subject}" if test_subject else self.adb_exe_path
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
        d = self._ensure_u2()
        if d and self._input_mode != "accessibility":
            try:
                d.swipe(fx, fy, tx, ty, float(duration))
                return
            except Exception as e:
                if self._is_inject_denied(e):
                    self._input_mode = "accessibility"
                    SLog.w(TAG, "坐标 swipe 被拒绝，已切换无障碍滚动")
                elif not self._is_inject_denied(e):
                    SLog.w(TAG, f"u2 swipe failed: {e}")
        if self._input_mode in ("accessibility", "full", "unknown"):
            dx, dy = tx - fx, ty - fy
            if abs(dx) >= abs(dy):
                direction = "left" if dx < 0 else "right"
            else:
                direction = "up" if dy < 0 else "down"
            if self._accessibility_scroll(direction):
                return
        if self._input_mode == "full":
            ms = max(int(float(duration) * 1000), 100)
            self.shell(f"input swipe {fx} {fy} {tx} {ty} {ms}")

    def press_key(self, event: str):
        if not event:
            return
        name = str(event).lower()
        d = self._ensure_u2()
        if d and name in ("back", "home", "menu", "power"):
            try:
                d.press(name)
                return
            except Exception as e:
                SLog.w(TAG, f"u2 press {name} failed: {e}")
        code = _ANDROID_KEY_MAP.get(name)
        if code is not None:
            self.shell(f"input keyevent {code}")
        elif str(event).isdigit():
            self.shell(f"input keyevent {event}")
        else:
            self.shell(f"input keyevent {str(event).upper()}")

    def build_chain(self, locator_chain):
        return AdbLocator(self, locator_chain)

    def start_app(self, package_name=None):
        if not package_name:
            return False
        package_name = package_name.strip().replace("\u200c", "")
        p_name = "".join(c for c in package_name if c.isprintable()).strip()
        d = self._ensure_u2()
        if d:
            try:
                d.app_start(p_name, stop=True)
                return True
            except Exception as e:
                SLog.w(TAG, f"u2 app_start failed, fallback monkey: {e}")
        self.shell(f"monkey -p {p_name} -c android.intent.category.LAUNCHER 1")
        return True

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

    def click_by_label(self, label: str) -> bool:
        """优先用无障碍节点点击（不依赖 INJECT_EVENTS）。"""
        if not label:
            return False
        d = self._ensure_u2()
        if not d:
            return False
        try:
            for text in self._label_variants(label):
                for sel in (
                    d(text=text),
                    d(textContains=text),
                    d(description=text),
                    d(descriptionContains=text),
                ):
                    if sel.exists(timeout=1.0):
                        sel.click()
                        return True
        except Exception as e:
            SLog.w(TAG, f"accessibility click '{label}' failed: {e}")
        return False

    def _click_accessibility_at(self, x: int, y: int) -> bool:
        """无障碍模式下：找包含该坐标的 clickable 节点再点。"""
        try:
            xml_data = self.dump_hierarchy_xml() or ""
            if not xml_data or "<?xml" not in xml_data:
                self.shell("uiautomator dump /sdcard/view.xml")
                xml_data = self.shell("cat /sdcard/view.xml")
            if not xml_data or "<?xml" not in xml_data:
                return False
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
                return False
            nums = re.findall(r"\d+", best.get("bounds") or "")
            if len(nums) != 4:
                return False
            x1, y1, x2, y2 = map(int, nums)
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            text = (best.get("text") or best.get("content-desc") or "").strip()
            if text and self.click_by_label(text):
                return True
            d = self._ensure_u2()
            if d:
                d.click(cx, cy)
                return True
        except Exception as e:
            SLog.w(TAG, f"accessibility click at ({x},{y}) failed: {e}")
        return False

    def click(self, element, position=None, label: str = ""):
        target = position if position else element
        if label and self.click_by_label(label):
            return
        if not target:
            return
        x, y = int(target[0]), int(target[1])
        d = self._ensure_u2()
        if d and self._input_mode != "accessibility":
            try:
                d.click(x, y)
                return
            except Exception as e:
                if self._is_inject_denied(e):
                    self._input_mode = "accessibility"
                elif not self._is_inject_denied(e):
                    SLog.w(TAG, f"u2 click failed: {e}")
        if self._input_mode == "full":
            self.shell(f"input tap {x} {y}")
            return
        if self._input_mode == "accessibility":
            self._click_accessibility_at(x, y)

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
