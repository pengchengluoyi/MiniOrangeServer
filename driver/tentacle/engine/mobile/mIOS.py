# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
iOS UI 自动化 — 多后端可插拔。

后端由 backend 字段选择（IOS_BACKENDS 注册表），组件层只调 engine.*，看不到差异：
    wda     IOSEngine       facebook-wda 直连 WDA，生命周期走 ios_runtime（会开 Xcode）
    appium  IOSAppiumEngine Appium XCUITest，全程不需要启动 Xcode

新增后端只要写一个 IOSEngine 子类并注册进 IOS_BACKENDS，调用方无需改动。
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
from driver.tentacle.engine.mobile.ios_config import assert_ddi_ready, resolve_device
from driver.tentacle.engine.mobile.ios_locator import IOSLocator
from driver.tentacle.engine.mobile.wda_touch import wda_tap

TAG = "IOSEngine"
TAG_APPIUM = "IOSAppiumEngine"

_IOS_PRESS_KEYS = frozenset({"home", "volumeUp", "volumeDown"})


def _is_dead_session_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return (
        "invalidsessionid" in name
        or "nosuchdriver" in name
        or "invalid session id" in text
        or "session is either terminated" in text
        or "session is not started" in text
    )

BACKEND_WDA = "wda"
BACKEND_APPIUM = "appium"
# 默认走 appium：全程不启动 Xcode。要回到旧的 WDA/Xcode 路径设 IOS_BACKEND=wda。
DEFAULT_IOS_BACKEND = BACKEND_APPIUM


class IOSEngine(MobileEngine):
    """WDA（facebook-wda）后端，同时承载各后端共用的解锁 / 定位 / 坐标逻辑。"""

    #: 后端标识，子类覆写；Manager 与 create_ios_engine 按此选择实现。
    BACKEND = BACKEND_WDA
    PLATFORM = "ios"

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
        self._unlock_with_passcode()
        SLog.i(
            TAG,
            f"WDA ready {self._wda_url} device={self._device.name} ({self._device.udid[:8]}...)",
        )

    def reset_session(self):
        """只关掉客户端，不杀 WDA 进程，再重新 init_driver。"""
        if self.driver:
            try:
                self.driver.close()
            except Exception as e:
                SLog.w(TAG, f"Close WDA client: {e}")
            self.driver = None
        self.init_driver(test_subject=self._test_subject)

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

    # ---------------- 后端原语：子类只需覆写这三个，解锁流程即可复用 ---------------- #
    def _is_locked(self) -> bool:
        return bool(self.driver.locked())

    def _wake_unlock(self) -> None:
        """滑动解锁到密码盘（不含输入密码）。"""
        self.driver.unlock()

    def _type_text(self, text: str) -> None:
        """向当前焦点（无元素）输入文本，用于锁屏密码盘。"""
        self.driver.send_keys(text)

    def _unlock_with_passcode(self) -> None:
        """会话建立后，用 m_device 中的锁屏密码解锁。与后端无关，仅走上面三个原语。"""
        if not self.driver:
            return
        time.sleep(1.0)
        pwd = self._device_unlock_password()
        if not pwd:
            SLog.w(
                self._tag(),
                f"未找到设备锁屏密码 sn={self._target_device_sn()}，"
                "请在前端设备管理或 POST /device/set_password 写入 m_device.password",
            )
            return
        SLog.i(self._tag(), f"使用 m_device 锁屏密码解锁 sn={self._target_device_sn()}")
        try:
            self._wake_unlock()
        except Exception as e:
            SLog.d(self._tag(), f"wake/unlock: {e}")
        time.sleep(0.5)
        SLog.i(self._tag(), f"输入锁屏密码（长度 {len(pwd)}）")
        try:
            self.swipe_norm(0.5, 0.88, 0.5, 0.28, 0.35)
            time.sleep(0.45)
        except Exception as e:
            SLog.d(self._tag(), f"swipe for passcode pad: {e}")
        try:
            self._type_text(pwd)
            time.sleep(0.6)
        except Exception as e:
            SLog.w(self._tag(), f"整串输入失败，改为逐字符: {e}")
            try:
                for ch in pwd:
                    self._type_text(ch)
                    time.sleep(0.06)
                time.sleep(0.5)
            except Exception as e2:
                SLog.w(self._tag(), f"逐字符输入锁屏密码仍失败: {e2}")
                return
        try:
            if self._is_locked():
                SLog.w(
                    self._tag(),
                    "输入密码后仍报告 locked()，请确认密码正确或需 Face ID/触控 ID 人工介入",
                )
            else:
                SLog.i(self._tag(), "锁屏已解除")
        except Exception as e:
            SLog.d(self._tag(), f"locked() after passcode: {e}")

    @classmethod
    def _tag(cls) -> str:
        return TAG_APPIUM if cls.BACKEND == BACKEND_APPIUM else TAG

    def screen_on(self):
        if not self.driver:
            return
        try:
            if self._is_locked():
                self._wake_unlock()
        except Exception as e:
            SLog.d(self._tag(), f"screen_on/unlock: {e}")

    def ensure_screen_ready(self, node_sn: Optional[str] = None) -> bool:
        """唤醒并解锁 iOS 屏幕。"""
        if not self.driver:
            return False
        try:
            if self._is_locked():
                SLog.i(self._tag(), f"screen locked, unlocking sn={self._target_device_sn()}")
                self._unlock_with_passcode()
            return not self._is_locked()
        except Exception as e:
            SLog.w(self._tag(), f"ensure_screen_ready failed: {e}")
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
        if not package_name:
            return True
        bundle = package_name.strip()
        try:
            if self.driver:
                self.driver.app_start(bundle)
        except Exception as e:
            if not _is_dead_session_error(e):
                raise
            SLog.w(TAG, f"app_start session dead, recreating: {e}")
            self.reset_session()
            self.driver.app_start(bundle)
        return True

    def stop_app(self, package_name=None):
        if package_name and self.driver:
            self.driver.app_stop(package_name.strip())
        return True

    def app_state(self, bundle_id: str) -> int:
        """
        应用状态码：0 未安装 / 1 未运行 / 2 后台挂起 / 3 后台运行 / 4 前台。
        facebook-wda 的 /wda/apps/state 返回整个响应体，取值需走 .value。
        """
        resp = self.driver.app_state(str(bundle_id).strip())
        if isinstance(resp, dict):
            value = resp.get("value")
        else:
            value = getattr(resp, "value", resp)
        return int(value)

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
            cx, cy = self._center_of(element)
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

    def element_bounds(self, element) -> tuple[int, int, int, int]:
        """元素外框 (left, top, right, bottom)。后端相关，供 IOSLocator / _center_of 使用。"""
        b = element.bounds
        return int(b.x), int(b.y), int(b.x2), int(b.y2)

    def _center_of(self, obj) -> tuple[int, int]:
        if isinstance(obj, (tuple, list)) and len(obj) >= 2:
            return int(obj[0]), int(obj[1])
        left, top, right, bottom = self.element_bounds(obj)
        return int((left + right) / 2), int((top + bottom) / 2)

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


class IOSAppiumEngine(IOSEngine):
    """
    Appium XCUITest 后端 —— 全程不需要启动 Xcode。

    纯自管：WDA 一律由 Appium 自己 build + install + launch，不复用已跑/已装的 WDA，
    与 wda 后端零耦合。appium server 进程默认自动拉起、end() 时关闭。环境变量：

        IOS_BACKEND=wda                   切回旧的 WDA/Xcode 后端（默认已是 appium）
        IOS_APPIUM_URL / _HOST / _PORT    appium server 地址，默认 127.0.0.1:4723
        IOS_APPIUM_AUTOSTART=0            不自动拉起 appium，只探测
        IOS_APPIUM_BIN                    指定 appium 可执行文件
        IOS_XCODE_ORG_ID                  签名 Team（默认自动探测）
        IOS_XCODE_SIGNING_ID              签名身份，默认 Apple Development
        IOS_APPIUM_WDA_LOCAL_PORT         固定本地转发端口（默认 8100，被占用则自动让路）
        IOS_APPIUM_CAPS='{"...": ...}'    追加/覆盖任意 capability
        IOS_SKIP_DDI_CHECK=1              跳过 DDI 前置检查
    """

    BACKEND = BACKEND_APPIUM

    def __init__(self):
        super().__init__()
        self._server_url: Optional[str] = None
        self._appium_started_here = False

    # ------------------------------ 生命周期 ------------------------------ #
    def init_driver(self, test_subject=None):
        if self.driver is not None:
            return

        self._test_subject = test_subject or self._test_subject

        from appium import webdriver

        from driver.tentacle.engine.mobile.ios_appium_runtime import (
            build_options,
            start_environment,
        )

        SLog.i(TAG_APPIUM, "Starting Appium environment (no Xcode GUI) ...")
        self._server_url = start_environment(test_subject=self._test_subject)
        self._appium_started_here = True

        self._device = resolve_device(self._test_subject)
        # DDI 是 iOS 17+ 启动 XCTest 的前提；不就绪就快速失败，别让调用方去啃 code 70。
        assert_ddi_ready(self._device, tag=TAG_APPIUM)
        options = build_options(self._device)
        self.driver = webdriver.Remote(self._server_url, options=options)
        self._unlock_with_passcode()
        SLog.i(
            TAG_APPIUM,
            f"Appium session ready {self._server_url} "
            f"device={self._device.name} ({self._device.udid[:8]}...)",
        )

    def reset_session(self):
        """只 quit Appium session，不关 Appium server，再重新 init_driver。"""
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                SLog.w(TAG_APPIUM, f"Quit Appium session: {e}")
            self.driver = None
        self.init_driver(test_subject=self._test_subject)

    def end(self):
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                SLog.w(TAG_APPIUM, f"Quit Appium session: {e}")
            self.driver = None

        if self._appium_started_here:
            from driver.tentacle.engine.mobile.ios_appium_runtime import stop_environment

            stop_environment()
            self._appium_started_here = False

        SLog.i(TAG_APPIUM, "Appium session ended")

    def _script(self, name: str, args: Optional[dict] = None):
        """执行 XCUITest 的 `mobile:` 扩展命令。"""
        return self.driver.execute_script(name, args or {})

    # ------------------------------ 后端原语 ------------------------------ #
    def _is_locked(self) -> bool:
        return bool(self.driver.is_locked())

    def _wake_unlock(self) -> None:
        self.driver.unlock()

    def _type_text(self, text: str) -> None:
        """无元素输入：优先 `mobile: keys`，退回到当前焦点元素。"""
        try:
            self._script("mobile: keys", {"keys": list(str(text))})
            return
        except Exception as e:
            SLog.d(TAG_APPIUM, f"mobile: keys 不可用，改用 active_element: {e}")
        self.driver.switch_to.active_element.send_keys(str(text))

    # ------------------------------ 屏幕 / 手势 ------------------------------ #
    def screen_size(self) -> tuple[int, int]:
        if not self.driver:
            raise RuntimeError("Appium driver not initialized")
        size = self.driver.get_window_size()
        return int(size["width"]), int(size["height"])

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
        w, h = self.screen_size()
        self._drag(
            int(w * x1),
            int(h * y1),
            int(w * x2),
            int(h * y2),
            float(duration),
        )

    def _drag(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> None:
        """mobile: dragFromToForDuration 的 duration 是起点按住时长，取值区间 [0.5, 60] 秒。"""
        self._script(
            "mobile: dragFromToForDuration",
            {
                "duration": max(0.5, min(60.0, float(duration))),
                "fromX": x1,
                "fromY": y1,
                "toX": x2,
                "toY": y2,
            },
        )

    def press_key(self, event: str):
        if not self.driver or not event:
            return
        name = str(event).lower()
        if name == "volume_up":
            name = "volumeUp"
        elif name == "volume_down":
            name = "volumeDown"
        if name in _IOS_PRESS_KEYS:
            # pressButton 的 name 大小写不敏感：home / volumeup / volumedown
            self._script("mobile: pressButton", {"name": name})
        else:
            SLog.w(TAG_APPIUM, f"iOS 不支持按键: {event}")

    # ------------------------------ App ------------------------------ #
    def start_app(self, package_name=None):
        if not package_name:
            return True
        bundle = package_name.strip()
        if not self.driver:
            self.init_driver(test_subject=self._test_subject)
        try:
            self._script("mobile: launchApp", {"bundleId": bundle})
        except Exception as e:
            if _is_dead_session_error(e):
                SLog.w(TAG_APPIUM, f"launchApp session dead, recreating: {e}")
                self.reset_session()
                try:
                    self._script("mobile: launchApp", {"bundleId": bundle})
                    return True
                except Exception as e2:
                    raise RuntimeError(self._launch_fail_hint(bundle, e2)) from e2
            raise RuntimeError(self._launch_fail_hint(bundle, e)) from e
        return True

    def _launch_fail_hint(self, bundle: str, exc: BaseException) -> str:
        text = str(exc)
        state: Optional[int] = None
        try:
            state = self.app_state(bundle)
        except Exception:
            pass
        launch_denied = (
            "FBSOpenApplication" in text
            or "process failed to launch" in text.lower()
            or "SBMainWorkspace" in text
            or "posixerrordomain" in text.lower()
        )
        if state == 0:
            return (
                f"模拟器未安装 {bundle}。请用 Xcode / flutter 把 "
                f"iphonesimulator 构建安装到当前模拟器，不要装真机 IPA。"
            )
        if launch_denied:
            return (
                f"模拟器拒绝启动 {bundle}（SpringBoard 拒绝 spawn，常见 POSIX 163）。"
                "这不是 Appium 会话问题，也不是重装 WDA 导致的断连。"
                "常见原因：装的是真机包而非模拟器包、签名/完整性失败、或包已损坏。"
                "请在 Xcode 选中这台模拟器重新 Run 安装后再执行。"
            )
        return text

    def stop_app(self, package_name=None):
        if package_name and self.driver:
            self._script("mobile: terminateApp", {"bundleId": package_name.strip()})
        return True

    def app_state(self, bundle_id: str) -> int:
        return int(self.driver.query_app_state(str(bundle_id).strip()))

    # ------------------------------ 定位 ------------------------------ #
    def find_element(self, locator_chain=None):
        locator_chain = locator_chain or []
        if not self.driver:
            return None
        merged: dict[str, Any] = {}
        for condition in locator_chain:
            merged.update(self._condition_to_wda(condition))
        by, value = self._to_appium_selector(merged)
        if not value:
            SLog.d(TAG_APPIUM, f"无法从 {locator_chain} 构造 Appium 选择器")
            return None
        timeout = float(os.environ.get("IOS_APPIUM_FIND_TIMEOUT") or 3.0)
        deadline = time.time() + timeout
        last_err: Optional[Exception] = None
        while True:
            try:
                el = self.driver.find_element(by=by, value=value)
                if el is not None:
                    SLog.d(TAG_APPIUM, f"Appium element found: {locator_chain}")
                return el
            except Exception as e:
                last_err = e
            if time.time() >= deadline:
                break
            time.sleep(0.3)
        SLog.d(TAG_APPIUM, f"Appium element not found {locator_chain}: {last_err}")
        return None

    @staticmethod
    def _to_appium_selector(merged: dict) -> tuple[str, Optional[str]]:
        """
        复用 _condition_to_wda 的归一化结果（predicate/classChain/xpath/name/label/className/index），
        转成 (AppiumBy, selector)。name/label/className 合成 NSPredicate，index 用 XPath 下标表达。
        """
        from appium.webdriver.common.appiumby import AppiumBy

        if merged.get("predicate"):
            return AppiumBy.IOS_PREDICATE, merged["predicate"]
        if merged.get("classChain"):
            return AppiumBy.IOS_CLASS_CHAIN, merged["classChain"]
        if merged.get("xpath"):
            return AppiumBy.XPATH, merged["xpath"]

        index = merged.get("index")
        class_name = merged.get("className")
        if isinstance(index, int):
            # XCUITest 的 XPath 下标从 1 开始，locator_chain 沿用 0 基
            tag = class_name or "*"
            parts = []
            if merged.get("name"):
                parts.append(f"@name='{merged['name']}'")
            if merged.get("label"):
                parts.append(f"@label='{merged['label']}'")
            cond = f"[{' and '.join(parts)}]" if parts else ""
            return AppiumBy.XPATH, f"(//{tag}{cond})[{index + 1}]"

        clauses = []
        if merged.get("name"):
            clauses.append(f"name == '{merged['name']}'")
        if merged.get("label"):
            clauses.append(f"label == '{merged['label']}'")
        if class_name:
            clauses.append(f"type == '{class_name}'")
        if clauses:
            return AppiumBy.IOS_PREDICATE, " AND ".join(clauses)
        return AppiumBy.IOS_PREDICATE, None

    def element_bounds(self, element) -> tuple[int, int, int, int]:
        rect = element.rect
        left, top = int(rect["x"]), int(rect["y"])
        return left, top, left + int(rect["width"]), top + int(rect["height"])

    # ------------------------------ 动作 ------------------------------ #
    def click(self, element, position=None):
        if position:
            self._script("mobile: tap", {"x": int(position[0]), "y": int(position[1])})
        elif element is not None:
            element.click()

    def double_click(self, element, position=None):
        if position:
            self._script(
                "mobile: doubleTap", {"x": int(position[0]), "y": int(position[1])}
            )
        elif element is not None:
            self._script("mobile: doubleTap", {"elementId": element.id})

    def long_press(self, element, position=None, duration=2.0):
        args: dict[str, Any] = {"duration": float(duration)}
        if position:
            args.update({"x": int(position[0]), "y": int(position[1])})
        elif element is not None:
            args["elementId"] = element.id
        else:
            return
        self._script("mobile: touchAndHold", args)

    def send_keys(self, element, text):
        if element is None:
            raise ValueError("send_keys requires element")
        try:
            element.clear()
        except Exception:
            pass
        element.send_keys(str(text))

    def clear(self, element):
        if element is not None:
            element.clear()

    def drag_and_drop(self, source, target):
        if not self.driver:
            return
        sx, sy = self._center_of(source)
        tx, ty = self._center_of(target)
        self._drag(sx, sy, tx, ty, 0.5)

    def screenshot(self, path=None):
        if not self.driver:
            return None
        if path:
            self.driver.get_screenshot_as_file(path)
            return path
        return self.driver.get_screenshot_as_base64()


#: backend 字段 -> 实现类。新增 iOS 驱动方案（tidevice / go-ios / …）只需在此注册。
IOS_BACKENDS: dict[str, type[IOSEngine]] = {
    BACKEND_WDA: IOSEngine,
    BACKEND_APPIUM: IOSAppiumEngine,
}


def resolve_ios_backend(backend: Optional[str] = None) -> str:
    """
    backend 解析优先级：显式参数 > builtins.IOS_BACKEND > 环境变量 IOS_BACKEND > 默认 wda。
    未知值告警并回退默认，不抛异常（避免因配置笔误让整个 run 挂掉）。
    """
    candidate = (
        backend
        or getattr(builtins, "IOS_BACKEND", None)
        or os.environ.get("IOS_BACKEND")
        or DEFAULT_IOS_BACKEND
    )
    name = str(candidate).strip().lower()
    if name not in IOS_BACKENDS:
        SLog.w(
            TAG,
            f"未知 iOS backend={candidate}，可选 {sorted(IOS_BACKENDS)}，"
            f"回退 {DEFAULT_IOS_BACKEND}",
        )
        return DEFAULT_IOS_BACKEND
    return name


def create_ios_engine(
    backend: Optional[str] = None,
    test_subject: Optional[str] = None,
) -> IOSEngine:
    """按 backend 取对应引擎实例（各后端各自单例，互不覆盖）。"""
    name = resolve_ios_backend(backend)
    engine = IOS_BACKENDS[name]()
    if test_subject:
        engine._test_subject = test_subject
    SLog.i(TAG, f"iOS backend={name} ({type(engine).__name__})")
    return engine
