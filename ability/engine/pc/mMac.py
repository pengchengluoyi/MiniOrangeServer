# !/usr/bin/env python
# -*-coding:utf-8 -*-
import time
import os
try:
    import Cocoa
    import Quartz
except ImportError:
    pass

from script.log import SLog
from ability.core.engine import BaseEngine

TAG = 'MacEngine'

class MacElement:
    def __init__(self, element):
        self._element = element

    def click(self):
        Quartz.AXUIElementPerformAction(self._element, "AXPress")

    def type_keys(self, text):
        Quartz.AXUIElementSetAttributeValue(self._element, Quartz.kAXValueAttribute, text)

    def get_position(self):
        # 获取位置用于模拟鼠标
        err, pos = Quartz.AXUIElementCopyAttributeValue(self._element, Quartz.kAXPositionAttribute, None)
        return pos if err == 0 else None

class MacEngine(BaseEngine):

    def init_driver(self, test_subject=None):
        SLog.i(TAG, "Init Mac Engine")
        self.driver = None

    def start_app(self, package_name=None):
        workspace = Cocoa.NSWorkspace.sharedWorkspace()
        workspace.launchApplication_(package_name)
        time.sleep(1)
        # 获取应用引用 (简化逻辑)
        for app in workspace.runningApplications():
            if app.localizedName() == package_name:
                self.driver = Quartz.AXUIElementCreateApplication(app.processIdentifier())
                return True
        return False

    def stop_app(self, package_name=None):
        workspace = Cocoa.NSWorkspace.sharedWorkspace()
        for app in workspace.runningApplications():
            if app.localizedName() == package_name:
                app.terminate()
                return True
        return False

    def _find_recursive(self, root, params, target_index=0):
        queue = [root]
        matches = []
        count = 0
        while queue and count < 5000:
            node = queue.pop(0)
            count += 1
            # 匹配逻辑 (简化)
            is_match = True
            for k, v in params.items():
                attr = None
                if k == 'title' or k == 'text': attr = Quartz.kAXTitleAttribute
                elif k == 'role' or k == 'type': attr = Quartz.kAXRoleAttribute
                elif k == 'id': attr = Quartz.kAXIdentifierAttribute
                elif k == 'desc': attr = Quartz.kAXDescriptionAttribute
                if attr:
                    err, val = Quartz.AXUIElementCopyAttributeValue(node, attr, None)
                    if err != 0 or val != v: is_match = False; break
            
            if is_match and node != root:
                matches.append(node)
                if len(matches) > target_index: return matches[target_index]

            err, children = Quartz.AXUIElementCopyAttributeValue(node, Quartz.kAXChildrenAttribute, None)
            if err == 0 and children: queue.extend(children)
        return None

    def find_element(self, locator_chain=[]):
        if not self.driver: return None
        current = self.driver
        for condition in locator_chain:
            params = {k: v for k, v in condition.items() if k not in ['index', 'not'] and v}
            index = condition.get('index', 0) or 0
            found = self._find_recursive(current, params, index)
            if found: current = found
            else: return None
        return MacElement(current)

    def end(self):
        return True

    # --- 统一动作接口 ---

    def click(self, element):
        element.click()

    def double_click(self, element):
        # Mac AX API 没有双击，通常需要模拟鼠标事件
        # 这里简化为两次点击
        element.click()
        time.sleep(0.1)
        element.click()

    def context_click(self, element):
        # 暂不支持
        pass

    def send_keys(self, element, text):
        element.type_keys(text)

    def clear(self, element):
        element.type_keys("")

    def drag_and_drop(self, source, target):
        pass

    def hover(self, element):
        pass

    def screenshot(self, path):
        # 使用系统命令截图
        os.system(f"screencapture -x {path}")
        return path

    def switch_window(self, target):
        """
        Mac: 切换应用 (激活应用)
        target: 应用名称或 Bundle ID
        """
        self.start_app(target)

    def close_window(self, target):
        self.stop_app(target)