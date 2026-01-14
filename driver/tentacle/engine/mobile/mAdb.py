# !/usr/bin/env python
# -*-coding:utf-8 -*-
import os
import re
import time
import platform
import subprocess
import xml.etree.ElementTree as ET
from io import BytesIO
from PIL import Image

from script.log import SLog
from driver.tentacle.core.engine import BaseEngine
from driver.tentacle.common.mPath import get_adb_path

TAG = 'MAdbEngine'


class MAdbEngine(BaseEngine):

    def init_driver(self, test_subject=None):
        if self.driver is not None:  # 二次检查
            return

        self.adb_exe_path = get_adb_path()
        self.adb_base = f"{self.adb_exe_path} -s {test_subject}" if test_subject else self.adb_exe_path
        # 🔥 关键修复：设置标志位，防止 BaseEngine.start 重复触发
        self.driver = "Android_Driver_Active"

    def shell(self, cmd):
        full_cmd = f"{self.adb_base} shell {cmd}"
        try:
            return subprocess.check_output(full_cmd, shell=True).decode('utf-8', errors='ignore').strip()
        except:
            return ""

    def start_app(self, package_name=None):
        if not package_name: return False
        package_name = package_name.strip().replace('\u200c', '')
        p_name = "".join(c for c in package_name if c.isprintable()).strip()
        # 启动 app 后建议在业务脚本里加一点 sleep
        self.shell(f"monkey -p {p_name} -c android.intent.category.LAUNCHER 1")
        return True

    def stop_app(self, package_name=None):
        if package_name:
            self.shell(f"am force-stop {package_name}")
        return True

    def screenshot(self, path=None):
        try:
            # exec-out 是获取二进制流最快且最稳定的方式
            cmd = f"{self.adb_base} exec-out screencap -p"
            img_bytes = subprocess.check_output(cmd, shell=True)
            if not img_bytes: return None
            img = Image.open(BytesIO(img_bytes))
            if path:
                img.save(path)
                return path
            return img
        except Exception as e:
            SLog.e(TAG, f"截图失败: {e}")
            return None

    def find_element(self, locator_chain=[]):
        """
        修复 adb pull - 报错问题：改用 cat 直接读取内容
        """
        self.shell("uiautomator dump /sdcard/view.xml")
        try:
            # 修正点：不再使用 pull - 而是使用 cat，彻底解决 unrecognized option '-' 报错
            xml_data = self.shell("cat /sdcard/view.xml")
            if not xml_data or "<?xml" not in xml_data:
                return None
            root = ET.fromstring(xml_data)
        except Exception as e:
            SLog.e(TAG, f"XML 解析失败: {e}")
            return None

        attr_map = {'id': 'resource-id', 'text': 'text', 'type': 'class', 'desc': 'content-desc'}
        target_node = None
        for condition in locator_chain:
            filters = {attr_map[k]: v for k, v in condition.items() if k in attr_map and v}
            for node in root.iter('node'):
                if all(node.get(k) == v for k, v in filters.items()):
                    target_node = node
                    break

        if target_node is not None:
            nums = re.findall(r'\d+', target_node.get('bounds'))
            if len(nums) == 4:
                x1, y1, x2, y2 = map(int, nums)
                return ((x1 + x2) // 2, (y1 + y2) // 2)
        return None

    def click(self, element, position=None):
        target = position if position else element
        if target: self.shell(f"input tap {target[0]} {target[1]}")

    def send_keys(self, element, text):
        """
        在指定位置输入文本
        """
        # 1. 先点击元素获取焦点
        if element:
            self.click(element)
            time.sleep(0.5)
        # 2. ADB 原生 text 不支持中文，空格需转义为 %s
        safe_text = str(text).replace(" ", "%s")
        self.shell(f"input text {safe_text}")

    def drag_and_drop(self, source, target):
        """
        从起点滑动到终点。source 和 target 都是 (x, y) 坐标元组
        """
        if source and target:
            # input swipe <x1> <y1> <x2> <y2> <duration_ms>
            SLog.i(TAG, f"执行滑动: {source} -> {target}")
            self.shell(f"input swipe {source[0]} {source[1]} {target[0]} {target[1]} 500")

    def keyevent(self, event):
        if event:
            # 如果event是数字（整数或字符串数字）
            if isinstance(event, int):
                key_value = str(event)
            elif event.isdigit():
                key_value = event  # 已经是数字字符串
            else:
                # 如果是字符串，转换为大写
                key_value = event.upper()

            self.shell(f"input keyevent {key_value}")



    def close_window(self, target):
        self.stop_app(target)