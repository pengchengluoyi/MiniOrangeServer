# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
iOS UI 自动化 — WebDriverAgent（facebook-wda）。
生命周期由 Manager.online/offline 调度，与 MAdbEngine 共用组件层 API。
"""
from __future__ import annotations

import base64
import builtins
import io
import os
import time
from typing import Any, Optional

import wda

from script.log import SLog
from driver.tentacle.engine.mobile.mobile_engine import MobileEngine
from driver.tentacle.engine.mobile.ios_config import resolve_device
from driver.tentacle.engine.mobile.ios_locator import IOSLocator
from driver.tentacle.engine.mobile.wda_touch import wda_tap

TAG = "IOSEngine"

_IOS_PRESS_KEYS = frozenset({"home", "volumeUp", "volumeDown"})


class IOSEngine(MobileEngine):
    def __init__(self):
        super().__init__()
        self._device = None
        self._wda_url: Optional[str] = None
        self._wda_started_here = False

    def init_driver(self, test_subject=None):
        if self.driver is not None:
            return

        self._test_subject = test_subject or self._test_subject

        from driver.tentacle.engine.mobile.ios_runtime import start_environment

        SLog.i(TAG, "Starting WDA environment ...")
        self._wda_url = start_environment(
            test_subject=self._test_subject, start_wda_flag=True
        )
        self._wda_started_here = True

        self._device = resolve_device(self._test_subject)
        client = wda.Client(self._wda_url)
        client.session()
        self.driver = client
        self._unlock_after_wda_session()
        SLog.i(
            TAG,
            f"WDA ready {self._wda_url} device={self._device.name} ({self._device.udid[:8]}...)",
        )

    def _target_device_sn(self) -> Optional[str]:
        """与前端 run_workflow / Manager 一致：m_device.sn 一般为 iOS 的 UDID。"""
        sn = getattr(builtins, "TARGET_DEVICE_SN", None)
        if sn:
            return str(sn)
        if self._test_subject:
            return str(self._test_subject)
        try:
            from driver.agent.Memory import memory_manager

            mem_sn = memory_manager.short_term.get_global("run_device_sn")
            if mem_sn:
                return str(mem_sn)
        except Exception:
            pass
        return None

    def _device_unlock_password(self) -> Optional[str]:
        """使用服务端 m_device.password（get_device_password / Memory 已加载的缓存）。"""
        sn = self._target_device_sn()
        if not sn:
            return None
        from driver.agent.Common.ws import WS

        pd = WS.get_device_password(sn)
        if pd and pd.get("password"):
            return str(pd["password"]).strip()
        try:
            from driver.agent.Memory import memory_manager

            mem_pw = memory_manager.short_term.get_global(f"{sn}_password")
            if mem_pw is not None and str(mem_pw).strip():
                return str(mem_pw).strip()
        except Exception:
            pass
        return None

    def _unlock_after_wda_session(self) -> None:
        """WDA 会话建立后，用 m_device 中的锁屏密码解锁（unlock 手势 + /wda/keys）。"""
        d = self.driver
        if not d:
            return
        time.sleep(1.0)
        pwd = self._device_unlock_password()
        if not pwd:
            SLog.w(
                TAG,
                f"未找到设备锁屏密码 sn={self._target_device_sn()}，"
                "请在前端设备管理或 POST /device/set_password 写入 m_device.password",
            )
            return
        SLog.i(TAG, f"使用 m_device 锁屏密码解锁 sn={self._target_device_sn()}")
        try:
            d.unlock()
        except Exception as e:
            SLog.d(TAG, f"wda.unlock(): {e}")
        time.sleep(0.5)
        SLog.i(TAG, f"通过 WDA 输入锁屏密码（长度 {len(pwd)}）")
        try:
            w, h = d.window_size()
            d.swipe(w // 2, int(h * 0.88), w // 2, int(h * 0.28), 0.35)
            time.sleep(0.45)
        except Exception as e:
            SLog.d(TAG, f"swipe for passcode pad: {e}")
        try:
            d.send_keys(pwd)
            time.sleep(0.6)
        except Exception as e:
            SLog.w(TAG, f"send_keys 整串失败，改为逐字符: {e}")
            try:
                for ch in pwd:
                    d.send_keys(ch)
                    time.sleep(0.06)
                time.sleep(0.5)
            except Exception as e2:
                SLog.w(TAG, f"逐字符输入锁屏密码仍失败: {e2}")
                return
        try:
            if d.locked():
                SLog.w(TAG, "输入密码后仍报告 locked()，请确认密码正确或需 Face ID/触控 ID 人工介入")
            else:
                SLog.i(TAG, "锁屏已解除")
        except Exception as e:
            SLog.d(TAG, f"locked() after passcode: {e}")

    def screen_on(self):
        if not self.driver:
            return
        try:
            if self.driver.locked():
                self.driver.unlock()
        except Exception as e:
            SLog.d(TAG, f"screen_on/unlock: {e}")

    def ensure_screen_ready(self, node_sn: Optional[str] = None) -> bool:
        """唤醒并解锁 iOS 屏幕。"""
        if not self.driver:
            return False
        try:
            if self.driver.locked():
                SLog.i(TAG, f"screen locked, unlocking sn={self._target_device_sn()}")
                self._unlock_after_wda_session()
            return not self.driver.locked()
        except Exception as e:
            SLog.w(TAG, f"ensure_screen_ready failed: {e}")
            return False

    def swipe_norm(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        duration: float = 0.5,
    ):
        if not self.driver:
            return
        w, h = self.driver.window_size()
        self.driver.swipe(
            int(w * x1),
            int(h * y1),
            int(w * x2),
            int(h * y2),
            float(duration),
        )

    def screen_size(self) -> tuple[int, int]:
        if not self.driver:
            raise RuntimeError("WDA client not initialized")
        w, h = self.driver.window_size()
        return int(w), int(h)

    def close_window(self, target):
        self.stop_app(target)

    def press_key(self, event: str):
        if not self.driver or not event:
            return
        name = str(event).lower()
        if name == "home":
            self.driver.home()
            return
        if name == "volume_up":
            name = "volumeUp"
        elif name == "volume_down":
            name = "volumeDown"
        if name in _IOS_PRESS_KEYS:
            self.driver.press(name)
        else:
            SLog.w(TAG, f"iOS 不支持按键: {event}")

    def start_app(self, package_name=None):
        if package_name and self.driver:
            self.driver.app_start(package_name.strip())
        return True

    def stop_app(self, package_name=None):
        if package_name and self.driver:
            self.driver.app_stop(package_name.strip())
        return True

    def find_element(self, locator_chain=None):
        locator_chain = locator_chain or []
        if not self.driver:
            return None
        kwargs: dict[str, Any] = {"timeout": 3.0}
        for condition in locator_chain:
            kwargs.update(self._condition_to_wda(condition))
        try:
            el = self.driver(**kwargs).get()
            if el is not None:
                SLog.d(TAG, f"WDA element found: {locator_chain}")
            return el
        except Exception as e:
            SLog.d(TAG, f"WDA element not found {locator_chain}: {e}")
            return None

    @staticmethod
    def _condition_to_wda(condition: dict) -> dict:
        out: dict[str, Any] = {}
        if "predicate" in condition:
            out["predicate"] = condition["predicate"]
        if "classChain" in condition:
            out["classChain"] = condition["classChain"]
        if "xpath" in condition or "XPATH" in condition:
            out["xpath"] = condition.get("xpath") or condition.get("XPATH")
        if "text" in condition:
            out["label"] = condition["text"]
        if "desc" in condition or "description" in condition:
            out["label"] = condition.get("desc") or condition.get("description")
        if "id" in condition or "name" in condition or "resourceId" in condition:
            out["name"] = (
                condition.get("id")
                or condition.get("name")
                or condition.get("resourceId")
            )
        if "type" in condition or "className" in condition or "classname" in condition:
            out["className"] = (
                condition.get("type")
                or condition.get("className")
                or condition.get("classname")
            )
        if "index" in condition and isinstance(condition["index"], int):
            out["index"] = condition["index"]
        return out

    def build_chain(self, locator_chain):
        return IOSLocator(self, locator_chain)

    def click(self, element, position=None):
        if position:
            wda_tap(self.driver, int(position[0]), int(position[1]))
        elif element is not None:
            element.click()

    def double_click(self, element, position=None):
        self.click(element, position)
        time.sleep(0.1)
        self.click(element, position)

    def context_click(self, element, position=None):
        self.long_press(element, position, duration=1.5)

    def long_press(self, element, position=None, duration=2.0):
        if position:
            x, y = int(position[0]), int(position[1])
            self.driver.swipe(x, y, x, y, duration)
        elif element is not None:
            bounds = element.bounds
            cx = int((bounds.x + bounds.x2) / 2)
            cy = int((bounds.y + bounds.y2) / 2)
            self.driver.swipe(cx, cy, cx, cy, duration)

    def send_keys(self, element, text):
        if element is None:
            raise ValueError("send_keys requires element")
        try:
            element.clear_text()
        except Exception:
            pass
        element.set_text(str(text))

    def clear(self, element):
        if element is not None:
            element.clear_text()

    def drag_and_drop(self, source, target):
        if not self.driver:
            return
        sx, sy = self._center_of(source)
        tx, ty = self._center_of(target)
        self.driver.swipe(sx, sy, tx, ty, 0.5)

    @staticmethod
    def _center_of(obj) -> tuple[int, int]:
        if isinstance(obj, (tuple, list)) and len(obj) >= 2:
            return int(obj[0]), int(obj[1])
        bounds = obj.bounds
        return int((bounds.x + bounds.x2) / 2), int((bounds.y + bounds.y2) / 2)

    def screenshot(self, path=None):
        if not self.driver:
            return None
        img = self.driver.screenshot()
        if path:
            img.save(path)
            return path
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")

    def end(self):
        if self.driver:
            try:
                self.driver.close()
            except Exception as e:
                SLog.w(TAG, f"Close WDA client: {e}")
            self.driver = None

        if self._wda_started_here:
            from driver.tentacle.engine.mobile.ios_runtime import stop_environment

            stop_environment(quit_xcode=True)
            self._wda_started_here = False

        SLog.i(TAG, "WDA session ended")
