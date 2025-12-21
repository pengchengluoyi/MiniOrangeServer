# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter
import ability.common.platform as platform_code

TAG = "WINDOW"

@BaseRouter.route('public/window')
class Window(Template):
    """
        Window/Context operations (Cross-Platform)
    """
    META = {
        "inputs": [
            {
                "name": "platform",
                "type": "select",
                "desc": "目标平台",
                "options": [
                    {"value": "android", "text": "Android"},
                    {"value": "ios", "text": "iOS"},
                    {"value": "windows", "text": "Windows"},
                    {"value": "mac", "text": "macOS"},
                    {"value": "web", "text": "Web"}
                ],
                "defaultValue": ""
            },
            {
                "name": "operation",
                "type": "select",
                "desc": "操作",
                "options": [
                    {"value": "start", "text": "启动 (Start)"},
                    {"value": "switch", "text": "切换 (Switch)"},
                    {"value": "close", "text": "关闭 (Close)"}
                ],
                "defaultValue": "start"
            },
            {
                "name": "target_mobile",
                "type": "str",
                "desc": "应用包名 (Package Name)",
                "placeholder": "com.example.app",
                "show_if": ["android", "ios"]
            },
            {
                "name": "target_pc",
                "type": "str",
                "desc": "窗口标题 (Window Title)",
                "placeholder": "记事本 / Untitled",
                "show_if": ["windows", "mac"]
            },
            {
                "name": "target_web",
                "type": "str",
                "desc": "标签页索引/标题",
                "placeholder": "0 (Index) / Title",
                "show_if": ["web"]
            }
        ],
        "defaultData": {
            "platform": "",
            "operation": "start",
            "target_mobile": "",
            "target_pc": "",
            "target_web": ""
        },
        "outputVars": []
    }

    def on_check(self):
        pass

    def execute(self):
        self.get_engine()
        operation = self.get_param_value("operation")
        platform = self.get_param_value("platform")
        target = None
        if platform in platform_code.MMOBILE:
            target = self.get_param_value("target_mobile")
        elif platform in platform_code.MPC:
            target = self.get_param_value("target_pc")
        elif platform in platform_code.MWEB:
            target = self.get_param_value("target_web")

        
        try:
            if operation == 'start':
                pid = self.engine.start_app(target)
                self.memory.set(self.info, "PID", pid)

            elif operation == 'switch':
                # 统一调用 switch_window，由引擎层去处理是切Tab、切App还是切Window
                if hasattr(self.engine, 'switch_window'):
                    self.engine.switch_window(target)
            
            elif operation == 'close':
                if hasattr(self.engine, 'close_window'):
                    self.engine.close_window(target)
            
            self.result.success()
        except Exception as e:
            SLog.e(TAG, f"Window action failed: {e}")
            self.result.fail()
            
        return self.result