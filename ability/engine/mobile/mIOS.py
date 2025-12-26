# !/usr/bin/env python
# -*-coding:utf-8 -*-
import time
try:
    import wda
except ImportError:
    pass

from script.log import SLog
from ability.core.engine import BaseEngine

TAG = 'IOSEngine'

class IOSEngine(BaseEngine):

    def init_driver(self, test_subject=None):
        SLog.i(TAG, "Init iOS Engine")
        try:
            self.driver = wda.USBClient()
        except Exception as e:
            SLog.w(TAG, f"USB connection failed, trying default localhost:8100. Error: {e}")
            self.driver = wda.Client()
        self.session = None

    def start_app(self, package_name=None):
        self.session = self.driver.session(package_name)
        return True

    def stop_app(self, package_name=None):
        self.driver.app_terminate(package_name)
        return True

    def find_element(self, locator_chain=[]):
        current = self.session if self.session else self.driver
        child = False
        
        for condition in locator_chain:
            locator_params = {}
            for key, value in condition.items():
                if key in ['index', 'not']: continue
                if value:
                    # 映射通用参数到 WDA 参数
                    if key == 'text': locator_params['label'] = value
                    elif key in ['resourceId', 'id']: locator_params['name'] = value
                    elif key == 'className': locator_params['className'] = value
                    elif key == 'description': locator_params['value'] = value
                    else: locator_params[key] = value

            index = condition.get('index', None)
            if child:
                current = current.child(**locator_params)
            else:
                current = current(**locator_params)

            if index is not None and isinstance(index, int):
                current = current[index]
            child = True

        return current

    def end(self):
        if self.session: self.session.close()
        return True

    # --- 统一动作接口 ---

    def click(self, element, position=None):
        if position:
            (self.session or self.driver).click(position[0], position[1])
        else:
            element.click()

    def double_click(self, element, position=None):
        if position:
            (self.session or self.driver).double_tap(position[0], position[1])
        else:
            element.double_tap()

    def context_click(self, element, position=None):
        if position:
            (self.session or self.driver).tap_hold(position[0], position[1], 1.0)
        else:
            element.hold(duration=1.0)

    def send_keys(self, element, text):
        element.set_text(text)

    def clear(self, element):
        element.clear_text()

    def drag_and_drop(self, source, target):
        # WDA drag 比较复杂，这里简化为 scroll
        # source.scroll(direction='down')
        SLog.w(TAG, "Drag not fully implemented for iOS")

    def hover(self, element):
        SLog.w(TAG, "Hover not supported on iOS")

    def screenshot(self, path=None):
        if path:
            self.driver.screenshot(path)
            return path
        return self.driver.screenshot()