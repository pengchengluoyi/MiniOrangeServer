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
        if self.driver is not None:  # 二次检查
            return

        SLog.i(TAG, "初始化 Windows 引擎 (全功能版)")
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()

        self.windows_minimize_all()
        # 🔥 关键修复：设置标志位，防止 BaseEngine.start 重复触发
        self.driver = "Windows_Driver_Active"

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

    @staticmethod
    def windows_minimize_all():
        """通过 COM 对象直接触发最小化所有窗口"""
        try:
            # 使用 shell32 接口
            # 也可以通过 pywin32: win32com.client.Dispatch("Shell.Application").MinimizeAll()
            ctypes.windll.shell32.ShellExecuteW(None, "open", "shell:::{3080f90d-d7ad-11d9-bd98-0000947b0257}", None,
                                                None, 6)
            return True
        except Exception as e:
            SLog.e("SYS", f"最小化失败: {e}")  #
            return False

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
        if element:
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

    def find_element(self, locator_chain=None):
        """
        Windows 版控件查找：链式遍历 UI 树并返回中心坐标
        """
        ...

    def close_window(self, exe_name=None):
        return self.stop_app(exe_name)
