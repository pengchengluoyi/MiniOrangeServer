# !/usr/bin/env python
# -*-coding:utf-8 -*-
import time
import io
from PIL import Image

from requests import options
from selenium import webdriver
from selenium.webdriver import ActionChains
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.desired_capabilities import DesiredCapabilities

from selenium.webdriver.common.by import By
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from script.log import SLog
from script.singleton_meta import SingletonMeta
from ability.core.engine import BaseEngine

TAG = 'ChromeEngine'


class ChromeEngine(BaseEngine):

    def __init__(self):
        super().__init__()
        self.service = None
        self.proxy = None
        self.server = None

    def init_driver(self):
        SLog.i(TAG, "Create an instance of Chrome browser")
        self.service = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=self.service)

    def start_app(self, url=None):
        if not url.startswith("http"):
            # 自动补全 http，防止 selenium 报错
            url = "https://" + url
        self.driver.get(url)
        self.driver.maximize_window()
        return "None"

    def stop_app(self, package_name=None):
        self.driver.close()
        return True

    def end(self):
        SLog.i(TAG, "Close Chrome browser instance")
        self.driver.quit()
        return True

    def find_element(self, locator_chain, timeout=10):
        """
            根据 locator_chain 智能查找元素
            优先级: XPath > ID > CSS Selector (Class) > Link Text
            """
        if not locator_chain or len(locator_chain) == 0:
            # raise Exception("定位数据为空")
            return False

        # 取出第一个定位信息对象
        info = locator_chain[0]

        element = None
        last_exception = None

        # --- 策略 1: XPath (最准确，由前端优化过) ---
        if info.get("XPATH"):
            try:
                SLog.d(TAG, f"🕵️ 尝试 XPath: {info['XPATH']}")
                # 使用显式等待，增加稳定性
                element = WebDriverWait(self.driver, timeout).until(
                    EC.presence_of_element_located((By.XPATH, info["XPATH"]))
                )
                return element
            except Exception as e:
                SLog.d(TAG, "   ❌ XPath 失败，尝试下一策略...")
                last_exception = e

        # --- 策略 2: Resource ID (非常可靠) ---
        if info.get("resourceId"):
            try:
                SLog.d(TAG, f"🕵️ 尝试 ID: {info['resourceId']}")
                element = self.driver.find_element(By.ID, info["resourceId"])
                return element
            except Exception:
                SLog.d(TAG, "   ❌ ID 失败，尝试下一策略...")

        # --- 策略 3: ClassName (需要处理空格) ---
        # 你的数据中 classname 是 "chat-input-textarea chat-input-scroll-style"
        # Selenium 的 By.CLASS_NAME 不支持带空格的复合类，必须转为 CSS Selector
        if info.get("classname"):
            try:
                # 将 "class1 class2" 转换为 ".class1.class2"
                class_str = info["classname"].strip()
                if class_str:
                    css_selector = "." + class_str.replace(" ", ".")
                    SLog.d(TAG, f"🕵️ 尝试 CSS Class: {css_selector}")
                    element = self.driver.find_element(By.CSS_SELECTOR, css_selector)
                    return element
            except Exception:
                SLog.d(TAG, "   ❌ ClassName 失败，尝试下一策略...")

        # --- 策略 4: Text (作为最后的兜底) ---
        if info.get("text"):
            try:
                text = info["text"]
                # 简单的文本全匹配 XPath
                xpath_text = f"//*[text()='{text}']"
                SLog.d(TAG, f"🕵️ 尝试 Text: {text}")
                element = self.driver.find_element(By.XPATH, xpath_text)
                return element
            except Exception:
                SLog.d(TAG, "   ❌ Text 失败")

        # 如果所有策略都失败
        # raise NoSuchElementException(f"无法定位元素，已尝试所有策略。信息: {info}")
        return False

    # --- 统一动作接口 ---

    def click(self, element, position=None):
        if position:
            ActionChains(self.driver).move_to_location(position[0], position[1]).click().perform()
        else:
            element.click()

    def double_click(self, element, position=None):
        if position:
            ActionChains(self.driver).move_to_location(position[0], position[1]).double_click().perform()
        else:
            ActionChains(self.driver).double_click(element).perform()

    def context_click(self, element, position=None):
        if position:
            ActionChains(self.driver).move_to_location(position[0], position[1]).context_click().perform()
        else:
            ActionChains(self.driver).context_click(element).perform()

    def send_keys(self, element, text):
        element.send_keys(text)

    def clear(self, element):
        element.clear()

    def drag_and_drop(self, source, target):
        ActionChains(self.driver).drag_and_drop(source, target).perform()

    def hover(self, element):
        ActionChains(self.driver).move_to_element(element).perform()

    def screenshot(self, path=None):
        if path:
            self.driver.save_screenshot(path)
            return path
        png_data = self.driver.get_screenshot_as_png()
        return Image.open(io.BytesIO(png_data))

    def execute_script(self, script):
        return self.driver.execute_script(script)

    def switch_window(self, handle_index=0):
        handles = self.driver.window_handles
        if len(handles) > handle_index:
            self.driver.switch_to.window(handles[handle_index])

    def switch_frame(self, frame_reference):
        self.driver.switch_to.frame(frame_reference)
