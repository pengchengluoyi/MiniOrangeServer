# !/usr/bin/env python
# -*-coding:utf-8 -*-

import time
import random

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter

TAG = "SCREENSHOT"


@BaseRouter.route('public/screenshot')
class Screenshot(Template):
    """
        This component will take a screenshot.
    """

    META = {
        "inputs": [
            {
                "name": "platform",
                "type": "select",
                "desc": "适用平台 (辅助筛选)",
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
                "name": "filename_prefix",
                "type": "str",
                "desc": "文件名前缀",
                "defaultValue": "screenshot",
                "placeholder": "screenshot"
            },
        ],
        "defaultData": {
            "platform": "",
            "filename_prefix": "screenshot",
            "locator_chain": []
        },
        "outputVars": [
            {"key": "path", "type": "str", "desc": "截图保存路径"}
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        prefix = self.get_param_value("filename_prefix")
        locator_chain = self.get_param_value("locator_chain")

        timestamp = int(time.time())
        random_num = random.randint(1000, 9999)
        filename = f"{prefix}_{timestamp}_{random_num}.png"

        # 如果指定了元素，尝试进行元素截图（需引擎支持）
        # 目前各引擎实现不一，这里作为预留接口，若找不到元素或不支持则回退到全屏
        captured = False
        if locator_chain:
            element = self.engine.find_element(locator_chain)
            if element and hasattr(element, 'screenshot'):
                try:
                    path = element.screenshot(filename)
                    captured = True
                except:
                    pass

        if not captured:
            path = self.engine.screenshot(filename)
            SLog.i("Screenshot", f"Saved at: {path}")

        self.result.success()
        return self.result