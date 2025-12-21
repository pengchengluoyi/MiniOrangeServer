# !/usr/bin/env python
# -*-coding:utf-8 -*-
import time

import uiautomator2 as u2

from script.log import SLog
from ability.core.engine import BaseEngine

TAG = 'AndroidEngine'


class AndroidEngine(BaseEngine):

    def init_driver(self, test_subject=None):
        SLog.i(TAG, "Create a mobile connection")
        self.driver = u2.connect()

    def start_app(self, package_name=None):
        self.driver.app_start(package_name)
        pid = self.driver.app_wait(package_name, front=True, timeout=20)
        return pid

    def stop_app(self, package_name=None):
        self.driver.app_stop(package_name)
        return True

    def find_element(self, locator_chain=[]):
        current = self.driver
        child = False

        for condition in locator_chain:
            # 过滤掉空字符串和 None 值，同时排除 index 和 not
            locator_params = {}
            for key, value in condition.items():
                if key in ['index', 'not']:
                    continue
                # 只添加非空且非空字符串的值
                if value is not None and value != '':
                    # --- 参数映射 ---
                    if key == 'id': locator_params['resourceId'] = value
                    elif key == 'type': locator_params['className'] = value
                    elif key == 'desc': locator_params['description'] = value
                    elif key == 'xpath': locator_params['xpath'] = value # u2 child 不直接支持 xpath，需注意
                    else: locator_params[key] = value

            # 如果所有定位参数都为空，跳过或抛出明确异常
            if not locator_params:
                raise ValueError(f"所有定位参数都为空，无法定位元素: {condition}")

            # 获取index，默认为None
            index = condition.get('index', None)
            method = current.child if child else current
            current = method(**locator_params)

            if index is not None and isinstance(index, int):
                current = current[index]
            child = True

        SLog.d(TAG, "Building chain for condition '{}'".format(current))
        return current

    def end(self):
        # self.driver.quit()
        SLog.i(TAG, "Close mobile instance")
        return True

    # --- 统一动作接口 ---

    def click(self, element):
        element.click()

    def double_click(self, element):
        # u2 没有直接的双击方法，通过坐标模拟
        x, y = element.center()
        self.driver.double_click(x, y)

    def context_click(self, element):
        # 移动端对应长按
        element.long_click()

    def send_keys(self, element, text):
        element.set_text(text)

    def clear(self, element):
        element.clear_text()

    def drag_and_drop(self, source, target):
        sx, sy = source.center()
        tx, ty = target.center()
        self.driver.drag(sx, sy, tx, ty)

    def hover(self, element):
        SLog.w(TAG, "Hover not supported on Android")

    def screenshot(self, path):
        self.driver.screenshot(path)
        return path

    def switch_window(self, target):
        """
        Android: 切换应用 (App Switch)
        target: 包名 (Package Name)
        """
        self.start_app(target)

    def close_window(self, target):
        self.stop_app(target)