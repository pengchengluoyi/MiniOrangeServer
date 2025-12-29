# !/usr/bin/env python
# -*-coding:utf-8 -*-
import time
from appium import webdriver
from appium.options.android import UiAutomator2Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.actions.action_builder import ActionBuilder
from selenium.webdriver.common.actions.pointer_input import PointerInput
from selenium.webdriver.common.actions import interaction

from script.log import SLog
from ability.core.engine import BaseEngine

TAG = 'AppiumAndroidEngine'


class AppiumAndroidEngine(BaseEngine):
    """
    基于 Appium 的 Android 引擎
    保持与 mAndroid.py 一致的 locator_chain 定位风格
    """

    def init_driver(self, test_subject=None):
        SLog.i(TAG, "Create Appium mobile connection")
        # 默认配置，实际可从 test_subject 或配置文件读取
        options = UiAutomator2Options()
        options.platform_name = "Android"
        options.automation_name = "UiAutomator2"
        options.no_reset = True
        options.new_command_timeout = 3600

        # Appium Server 地址默认为本地
        self.driver = webdriver.Remote("http://127.0.0.1:4723", options=options)

    def start_app(self, package_name=None):
        # Appium 使用 mobile: startActivity
        self.driver.execute_script('mobile: startActivity', {
            'component': f"{package_name}/{package_name}.MainActivity"  # 建议配合具体的 Activity
        })
        return True

    def stop_app(self, package_name=None):
        self.driver.terminate_app(package_name)
        return True

    def find_element(self, locator_chain=[]):
        """
        深度还原你的 locator_chain 链式定位风格
        Appium 中通过父元素 find_element 实现
        """
        current = self.driver

        for condition in locator_chain:
            locator_params = {}
            by = By.ID
            value = ""

            # --- 参数映射 (兼容你的风格) ---
            if 'id' in condition:
                by, value = By.ID, condition['id']
            elif 'type' in condition:
                by, value = By.CLASS_NAME, condition['type']
            elif 'desc' in condition:
                by, value = By.ACCESSIBILITY_ID, condition['desc']
            elif 'text' in condition:
                # Appium 推荐使用 XPath 或 UIAutomator 表达式处理文本
                by, value = By.XPATH, f"//*[@text='{condition['text']}']"
            elif 'xpath' in condition:
                by, value = By.XPATH, condition['xpath']

            if not value:
                continue

            index = condition.get('index', None)

            # 执行查找
            if index is not None and isinstance(index, int):
                # 如果有 index，查找列表并取值
                elements = current.find_elements(by, value)
                current = elements[index] if len(elements) > index else None
            else:
                current = current.find_element(by, value)

            if current is None:
                SLog.e(TAG, f"Failed to find element in chain: {condition}")
                break

        SLog.d(TAG, "Successfully built Appium element chain")
        return current

    # --- 统一动作接口 ---

    def click(self, element, position=None):
        if position:
            # 使用 W3C Actions 执行坐标点击
            actions = ActionChains(self.driver)
            actions.w3c_actions = ActionBuilder(self.driver, mouse=PointerInput(interaction.POINTER_TOUCH, "touch"))
            actions.w3c_actions.pointer_action.move_to_location(position[0], position[1])
            actions.w3c_actions.pointer_action.pointer_down()
            actions.w3c_actions.pointer_action.pause(0.1)
            actions.w3c_actions.pointer_action.release()
            actions.perform()
        else:
            element.click()

    def double_click(self, element, position=None):
        # 移动端双击通常需要模拟两次点击
        SLog.w(TAG, "Simulating double click via actions")
        self.click(element, position)
        time.sleep(0.1)
        self.click(element, position)

    def context_click(self, element, position=None):
        # 对应长按 (Long Press)
        actions = ActionChains(self.driver)
        actions.w3c_actions = ActionBuilder(self.driver, mouse=PointerInput(interaction.POINTER_TOUCH, "touch"))
        if position:
            actions.w3c_actions.pointer_action.move_to_location(position[0], position[1])
        else:
            actions.w3c_actions.pointer_action.move_to(element)

        actions.w3c_actions.pointer_action.pointer_down()
        actions.w3c_actions.pointer_action.pause(2.0)  # 长按 2 秒
        actions.w3c_actions.pointer_action.release()
        actions.perform()

    def send_keys(self, element, text):
        element.send_keys(text)

    def clear(self, element):
        element.clear()

    def drag_and_drop(self, source, target):
        self.driver.drag_and_drop(source, target)

    def screenshot(self, path=None):
        if path:
            self.driver.save_screenshot(path)
            return path
        return self.driver.get_screenshot_as_base64()

    def switch_window(self, target):
        self.driver.activate_app(target)

    def close_window(self, target):
        self.stop_app(target)

    def end(self):
        if self.driver:
            self.driver.quit()
        SLog.i(TAG, "Appium driver session ended")