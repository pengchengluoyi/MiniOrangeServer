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
        self.adb_base = (
            f"{self.adb_exe_path} -s {test_subject}" if test_subject else self.adb_exe_path
        )
        self.driver = "Android_Driver_Active"

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
        ms = max(int(float(duration) * 1000), 100)
        self.shell(
            f"input swipe {int(w * x1)} {int(h * y1)} {int(w * x2)} {int(h * y2)} {ms}"
        )

    def press_key(self, event: str):
        if not event:
            return
        name = str(event).lower()
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

    def find_element(self, locator_chain=None):
        locator_chain = locator_chain or []
        self.shell("uiautomator dump /sdcard/view.xml")
        try:
            xml_data = self.shell("cat /sdcard/view.xml")
            if not xml_data or "<?xml" not in xml_data:
                return None
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

    def click(self, element, position=None):
        target = position if position else element
        if target:
            self.shell(f"input tap {target[0]} {target[1]}")

    def send_keys(self, element, text):
        if element:
            self.click(element)
            time.sleep(0.5)
        safe_text = str(text).replace(" ", "%s")
        self.shell(f"input text {safe_text}")

    def drag_and_drop(self, source, target):
        if source and target:
            SLog.i(TAG, f"执行滑动: {source} -> {target}")
            self.shell(
                f"input swipe {source[0]} {source[1]} {target[0]} {target[1]} 500"
            )

    def keyevent(self, event):
        self.press_key(str(event))

    def close_window(self, target):
        self.stop_app(target)
