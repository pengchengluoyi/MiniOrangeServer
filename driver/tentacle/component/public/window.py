# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from driver.tentacle.component.template import Template
from driver.tentacle.component.router import BaseRouter
import driver.tentacle.common.platform as platform_code

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
                    {"value": "mobile", "text": "移动端 (Android + iOS)"},
                    {"value": "android", "text": "Android"},
                    {"value": "ios", "text": "iOS"},
                    {"value": "windows", "text": "Windows"},
                    {"value": "mac", "text": "macOS"},
                    {"value": "web", "text": "Web"}
                ],
                "defaultValue": "mobile"
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
                "name": "restart",
                "type": "bool",
                "desc": "是否重启应用",
                "defaultValue": True,
                "trueText": "重启",
                "falseText": "保持"
            },
            {
                "name": "target_mobile",
                "type": "str",
                "desc": "应用标识 (Android 包名 / iOS Bundle)",
                "placeholder": "com.example.app",
                "show_if": ["mobile", "android", "ios"]
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
            "platform": "mobile",
            "operation": "start",
            "restart": False,
            "target_mobile": "{{app.mobile.target}}",
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
        platform = self.get_param_value("platform") or self.info.platform
        restart = self.get_param_value("restart")
        target = None
        if platform in platform_code.MMOBILE:
            target = self.get_param_value("target_mobile")
            if platform == platform_code.MOBILE and not target:
                target = self.memory.get("{{app.mobile.target}}")
        elif platform in platform_code.MPC:
            target = self.get_param_value("target_pc")
        elif platform in platform_code.MWEB:
            target = self.get_param_value("target_web")

        
        try:
            if restart:
                if hasattr(self.engine, 'close_window'):
                    self.engine.close_window(target)
            ok = False
            if operation == 'start':
                if not target:
                    self.result.fail()
                    SLog.e(TAG, "Window start failed: empty target")
                    return self.result
                if not self.engine:
                    self.result.fail()
                    SLog.e(TAG, "Window start failed: no mobile engine")
                    return self.result
                pid = self.engine.start_app(target)
                ok = pid is not False and pid is not None
                if ok:
                    self.memory.set(self.info, "PID", pid)
                    self.result.success()
                else:
                    self.result.fail()
                    SLog.e(TAG, f"Window start failed: {target}")

            elif operation == 'close':
                if hasattr(self.engine, 'close_window'):
                    self.engine.close_window(target)
                self.result.success()
            elif operation == 'switch':
                if hasattr(self.engine, 'switch_window'):
                    self.engine.switch_window(target)
                self.result.success()
        except Exception as e:
            SLog.e(TAG, f"Window action failed: {e}")
            self.result.fail()
            
        return self.result