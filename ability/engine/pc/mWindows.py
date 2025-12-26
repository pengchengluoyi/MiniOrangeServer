# !/usr/bin/env python
# -*-coding:utf-8 -*-
import os

try:
    from pywinauto.application import Application
    from pywinauto.desktop import Desktop
    from PIL import ImageGrab
except ImportError:
    pass

from script.log import SLog
from ability.core.engine import BaseEngine

TAG = 'WindowsEngine'


class WindowsEngine(BaseEngine):
    package_path = None

    def init_driver(self, test_subject=None):
        SLog.i(TAG, "Init Windows Engine")
        self.driver = Application(backend="uia")

    def start_app(self, package_name=None):
        self.driver.start(r"{}".format(package_name))
        self.package_path = package_name
        return True

    def stop_app(self, package_name=None):
        try:
            self.driver.kill()
            if package_name is not None:
                file_name = os.path.basename(self.package_path)
                os.system("taskkill /F /IM {}".format(file_name))
        except:
            pass
        return True

    def find_element(self, locator_chain=[]):
        current = self.driver
        is_root = True

        for condition in locator_chain:
            locator_params = {}
            for key, value in condition.items():
                if key in ['index', 'not']: continue
                if value:
                    # --- 参数映射 ---
                    if key == 'id':
                        locator_params['auto_id'] = value
                    elif key == 'text':
                        locator_params['title'] = value
                    elif key == 'type':
                        locator_params['control_type'] = value
                    else:
                        locator_params[key] = value

            index = condition.get('index', None)
            if index is not None and isinstance(index, int):
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

    def click(self, element):
        # click_input 模拟鼠标点击，比 click() 消息更可靠
        element.click_input()

    def double_click(self, element):
        element.double_click_input()

    def context_click(self, element):
        element.right_click_input()

    def send_keys(self, element, text):
        element.type_keys(text, with_spaces=True)

    def clear(self, element):
        # 全选 + 删除
        element.type_keys("^a{DELETE}")

    def drag_and_drop(self, source, target):
        element = source
        # 需要获取目标坐标
        dst_rect = target.rectangle()
        element.drag_mouse_input(dst=(dst_rect.mid_point().x, dst_rect.mid_point().y))

    def hover(self, element):
        element.move_mouse_input()

    def dump_hierarchy(self, compressed=False, pretty=False, max_depth=None):
        """
        Dumps the UI hierarchy of the current window.
        Falls back to the active desktop window if the app window is not available.
        """
        try:
            # pywinauto's print_control_identifiers returns a list of strings
            hierarchy_list = self.driver.top_window().print_control_identifiers(depth=max_depth)
            return "\n".join(hierarchy_list)
        except Exception as e:
            SLog.w(TAG, f"Dump hierarchy for app window failed: {e}, fallback to active desktop window.")
            try:
                desktop = Desktop(backend="uia")
                active_window = desktop.active()
                if active_window:
                    hierarchy_list = active_window.print_control_identifiers(depth=max_depth)
                    return "\n".join(hierarchy_list)
                else:
                    SLog.w(TAG, "No active window found on desktop.")
                    return ""
            except Exception as e2:
                SLog.e(TAG, f"Fallback to dump active window hierarchy also failed: {e2}")
                return ""

    def screenshot(self, path=None):
        # 截取当前操作的窗口
        img = None
        try:
            img = self.driver.top_window().capture_as_image()
        except Exception as e:
            SLog.w(TAG, f"Capture app window failed: {e}, fallback to fullscreen.")
            img = ImageGrab.grab()

        if path:
            img.save(path)
            return path
        return img

    def switch_window(self, target):
        """
        Windows: 切换到指定标题的窗口
        target: 窗口标题 (支持正则)
        """
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
        try:
            win = Desktop(backend="uia").window(title_re=target)
            if win.exists():
                win.close()
        except Exception as e:
            SLog.e(TAG, f"Close window failed: {e}")
