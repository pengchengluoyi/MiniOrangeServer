# !/usr/bin/env python
# -*-coding:utf-8 -*-
import os, sys, io

try:
    from pywinauto.application import Application
    from pywinauto.desktop import Desktop
    from pywinauto import mouse
except ImportError:
    Application = None
    Desktop = None
    mouse = None

try:
    from PIL import ImageGrab
except ImportError:
    ImageGrab = None

from script.log import SLog
from ability.core.engine import BaseEngine

TAG = 'WindowsEngine'


class WindowsEngine(BaseEngine):
    package_path = None

    def init_driver(self, test_subject=None):
        SLog.i(TAG, "Init Windows Engine")
        if Application:
            # 仅初始化对象，不绑定进程
            self.driver = Application(backend="uia")
        else:
            SLog.e(TAG, "pywinauto not installed or failed to load.")
            self.driver = None

    def start_app(self, package_name=None):
        if not package_name or not self.driver:
            return False
        try:
            self.driver.start(r"{}".format(package_name))
            self.package_path = package_name
            return True
        except Exception as e:
            SLog.e(TAG, f"Start app failed: {e}")
            return False

    def stop_app(self, package_name=None):
        """
        修复逻辑：优先关闭当前 driver 绑定的进程，
        如果指定了 package_name 或有缓存路径，则执行强制杀进程。
        """
        try:
            # 1. 尝试正常关闭
            if self.driver and hasattr(self.driver, 'kill'):
                try:
                    self.driver.kill()
                except:
                    pass

            # 2. 强制杀进程（处理残留）
            # 修复：优先使用传入的 package_name，否则使用缓存的 self.package_path
            target = package_name or self.package_path
            if target:
                file_name = os.path.basename(target)
                if file_name:
                    os.system("taskkill /F /IM {} /T".format(file_name))
        except Exception as e:
            SLog.w(TAG, f"Stop app warning: {e}")
        return True

    def find_element(self, locator_chain=None):
        # 修复：如果 driver 尚未连接到窗口，尝试从 Desktop 开始找
        try:
            current = self.driver.top_window()
        except:
            if Desktop:
                current = Desktop(backend="uia")
            else:
                SLog.e(TAG, "No driver or Desktop available for find_element")
                return None

        is_root = True
        for condition in locator_chain:
            locator_params = {}
            for key, value in condition.items():
                if key in ['index', 'not'] or value is None:
                    continue
                # 参数映射
                mapping = {'id': 'auto_id', 'text': 'title', 'type': 'control_type'}
                locator_params[mapping.get(key, key)] = value

            index = condition.get('index', None)
            if isinstance(index, int):
                locator_params['found_index'] = index

            if is_root:
                current = current.window(**locator_params)
                is_root = False
            else:
                current = current.child_window(**locator_params)
        return current

    def end(self):
        self.stop_app()

    # --- 统一动作接口 ---

    def click(self, element, position=None):
        # click_input 模拟鼠标点击，比 click() 消息更可靠
        if position:
            if mouse:
                mouse.click(coords=(position[0], position[1]))
            else:
                SLog.e(TAG, "Mouse module not available for click")
        else:
            element.click_input()

    def double_click(self, element, position=None):
        if position:
            if mouse:
                mouse.double_click(coords=(position[0], position[1]))
            else:
                SLog.e(TAG, "Mouse module not available for double_click")
        else:
            element.double_click_input()

    def context_click(self, element, position=None):
        if position:
            if mouse:
                mouse.right_click(coords=(position[0], position[1]))
            else:
                SLog.e(TAG, "Mouse module not available for context_click")
        else:
            element.right_click_input()

    def send_keys(self, element, text):
        element.type_keys(text, with_spaces=True)

    def clear(self, element):
        # 全选 + 删除
        element.type_keys("^a{DELETE}")

    def drag_and_drop(self, source, target):
        # WindowSpecification 需要先获取 wrapper_object
        src_wrapper = source.wrapper_object() if hasattr(source, "wrapper_object") else source
        tgt_wrapper = target.wrapper_object() if hasattr(target, "wrapper_object") else target
        
        # 需要获取目标坐标
        dst_rect = tgt_wrapper.rectangle()
        src_wrapper.drag_mouse_input(dst=(dst_rect.mid_point().x, dst_rect.mid_point().y))

    def hover(self, element):
        if hasattr(element, "wrapper_object"):
            element.wrapper_object().move_mouse_input()
        else:
            element.move_mouse_input()

    def dump_hierarchy(self, compressed=False, pretty=False, max_depth=None):
        """
        修复：pywinauto 的 print_control_identifiers 会直接打印到 stdout，返回值为 None。
        必须通过重定向 stdout 来捕获字符串。
        """
        if not Desktop:
            return "Desktop module not available"

        output = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = output
        try:
            target = None
            try:
                target = self.driver.top_window()
            except:
                target = Desktop(backend="uia").active()

            if target:
                target.print_control_identifiers(depth=max_depth)
        except Exception as e:
            print(f"Dump failed: {e}")
        finally:
            sys.stdout = old_stdout

        result = output.getvalue()
        output.close()
        return result

    def screenshot(self, path=None):
        # 截取当前操作的窗口
        img = None
        try:
            if self.driver:
                img = self.driver.top_window().capture_as_image()
            else:
                raise Exception("Driver is None")
        except Exception as e:
            SLog.w(TAG, f"Capture app window failed: {e}, fallback to fullscreen.")
            if ImageGrab:
                img = ImageGrab.grab()
            else:
                SLog.e(TAG, "ImageGrab not available for screenshot")

        if path and img:
            img.save(path)
            return path
        return img

    def switch_window(self, target):
        """
        Windows: 切换到指定标题的窗口
        target: 窗口标题 (支持正则)
        """
        if not Desktop:
            SLog.e(TAG, "Desktop module not available for switch_window")
            return

        try:
            # 使用 Desktop 可以查找所有运行中程序的窗口
            win = Desktop(backend="uia").window(title_re=target)
            if win.exists():
                win.set_focus()
                # 更新当前的 driver 指向，以便后续操作基于新窗口
                # 注意：如果跨进程，self.driver (Application) 可能无法直接接管，
                # 这里仅做视觉切换，后续 find_element 可能需要动态获取 active_window
                SLog.i(TAG, f"Switched to window: {target}")
            else:
                SLog.w(TAG, f"Window not found: {target}")
        except Exception as e:
            SLog.e(TAG, f"Switch window failed: {e}")

    def close_window(self, target):
        if not Desktop:
            SLog.e(TAG, "Desktop module not available for close_window")
            return

        try:
            win = Desktop(backend="uia").window(title_re=target)
            if win.exists():
                win.close()
        except Exception as e:
            SLog.e(TAG, f"Close window failed: {e}")
