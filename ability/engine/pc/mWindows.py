# !/usr/bin/env python
# -*-coding:utf-8 -*-
import os
import time
import ctypes
import subprocess
from PIL import ImageGrab
from script.log import SLog
from ability.core.engine import BaseEngine

TAG = 'WindowsEngine'

# Windows 鼠标事件常量定义
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_ABSOLUTE = 0x8000

class WindowsEngine(BaseEngine):
    def init_driver(self, test_subject=None):
        SLog.i(TAG, "初始化 Windows 引擎 (全功能版)")
        # 🛡️ 核心修复：启用高 DPI 意识。
        # 解决 Windows 系统缩放（如 150%）导致 OCR 坐标与点击坐标不一致的问题。
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()

    def start_app(self, app_path_or_link=None):
        if not app_path_or_link: return False
        try:
            SLog.i(TAG, f"启动应用: {app_path_or_link}")
            os.startfile(app_path_or_link)
            return True
        except Exception as e:
            SLog.e(TAG, f"启动失败: {e}")
            return False

    def stop_app(self, exe_name=None):
        if not exe_name: return False
        try:
            SLog.i(TAG, f"强制关闭进程: {exe_name}")
            subprocess.run(f"taskkill /F /IM {exe_name} /T", shell=True, check=False)
            return True
        except Exception as e:
            SLog.e(TAG, f"关闭失败: {e}")
            return False

    def screenshot(self, path=None):
        """全屏截图，返回 PIL 对象适配 ocr.py"""
        try:
            img = ImageGrab.grab(all_screens=True) # 抓取所有显示器
            if path:
                img.save(path)
                return path
            return img
        except Exception as e:
            SLog.e(TAG, f"截图失败: {e}")
            return None

    def end(self):
        SLog.i(TAG, "退出 Windows 引擎")
        return True

    # --- 动作实现 ---

    def _move_to(self, x, y):
        """内部方法：移动鼠标到指定坐标"""
        ctypes.windll.user32.SetCursorPos(int(x), int(y))


    # 统一接口

    def click(self, element=None, position=None):
        """左键单击"""
        if position:
            self._move_to(position[0], position[1])
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def double_click(self, element=None, position=None):
        """左键双击"""
        if position:
            self.click(position=position)
            time.sleep(0.1)
            self.click(position=position)

    def context_click(self, element=None, position=None):
        """
        右键点击。对应 gesture.py 中的 right-click 和 long_press。
        """
        if position:
            SLog.d(TAG, f"执行右键点击: {position}")
            self._move_to(position[0], position[1])
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, 0)
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_RIGHTUP, 0, 0, 0, 0)

    def long_click(self, element=None, position=None, duration=1.5):
        """
        模拟左键长按。
        """
        if position:
            SLog.d(TAG, f"执行左键长按 ({duration}s): {position}")
            self._move_to(position[0], position[1])
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(duration)
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def send_keys(self, element, text):
        """
        Windows 极简输入。
        注意：使用 ctypes 模拟按键较复杂，这里推荐使用 Windows 自带的 clip 管道实现中文支持
        """
        if not text: return
        self.click(element)  # 获取焦点
        time.sleep(0.2)

        # 极简方案：通过 PowerShell 直接发送按键指令，支持中文且无需额外库
        powershell_cmd = f"Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.SendKeys]::SendWait('{text}')"
        subprocess.run(["powershell", "-Command", powershell_cmd])

    def drag_and_drop(self, source, target):
        """
        Windows 拖拽实现：按下 -> 移动 -> 弹起
        """
        if source and target:
            SLog.i(TAG, f"Windows 执行拖拽: {source} -> {target}")
            # 1. 移到起点并按下
            self._move_to(source[0], source[1])
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.2)

            # 2. 平滑移动到终点
            self._move_to(target[0], target[1])
            time.sleep(0.2)

            # 3. 在终点弹起
            ctypes.windll.user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    def find_element(self, locator_chain=[]):
        """
        Windows 版控件查找：链式遍历 UI 树并返回中心坐标
        """
        import uiautomation as auto
        SLog.d(TAG, f"Windows 控件定位链: {locator_chain}")

        # 从根桌面开始查找
        current = auto.GetRootControl()

        try:
            for condition in locator_chain:
                # 映射参数：将你的通用 key 映射到 Windows UIA 属性
                search_params = {}
                if condition.get('id'): search_params['AutomationId'] = condition['id']
                if condition.get('text'): search_params['Name'] = condition['text']
                if condition.get('type'): search_params['ClassName'] = condition['type']
                if condition.get('desc'): search_params['Description'] = condition['desc']

                # 如果这一层没有任何过滤条件，跳过
                if not search_params:
                    continue

                # 查找匹配的子控件 (searchDepth=1 模拟链式逐级查找)
                current = current.Control(searchDepth=1, **search_params)

                # 检查是否存在，如果中途断链则返回 None
                if not current.Exists(0):
                    SLog.w(TAG, f"未找到匹配控件: {search_params}")
                    return None

            # 最终匹配成功，获取控件的矩形区域
            if current.Exists(0):
                rect = current.BoundingRectangle
                # 计算中心点坐标 (left, top, right, bottom)
                cx = (rect.left + rect.right) // 2
                cy = (rect.top + rect.bottom) // 2
                SLog.i(TAG, f"Windows 控件定位成功: ({cx}, {cy})")
                return (cx, cy)

        except Exception as e:
            SLog.e(TAG, f"Windows 控件解析出错: {e}")

        return None
