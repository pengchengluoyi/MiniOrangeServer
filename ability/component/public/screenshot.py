# !/usr/bin/env python
# -*-coding:utf-8 -*-

import os
import sys
import time
import random

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter
from server.core.database import APP_DATA_DIR

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
            {"key": "path", "type": "str", "desc": "截图保存路径"},
            {"key": "url", "type": "str", "desc": "图片Web路径"}
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()

        prefix = self.get_param_value("filename_prefix")
        # 修复: locator_chain 是列表类型，直接从 data 获取，避免 get_param_value 中的正则匹配导致 TypeError
        locator_chain = self.info.data.get("locator_chain", [])

        timestamp = int(time.time())
        random_num = random.randint(1000, 9999)
        filename = f"{prefix}_{timestamp}_{random_num}.png"
        
        # 修正：使用用户数据目录，确保截图持久保存
        save_dir = os.path.join(APP_DATA_DIR, "uploads")

        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        full_path = os.path.join(save_dir, filename)
        # 统一使用 /file/ 接口，与 rFile.py 保持一致
        web_path = f"/file/{filename}"

        # 如果指定了元素，尝试进行元素截图（需引擎支持）
        # 目前各引擎实现不一，这里作为预留接口，若找不到元素或不支持则回退到全屏
        captured = False
        if locator_chain:
            element = self.engine.find_element(locator_chain)
            if element and hasattr(element, 'screenshot'):
                try:
                    path = element.screenshot(full_path)
                    captured = True
                except:
                    pass

        if not captured:
            path = self.engine.screenshot(full_path)
            SLog.i("Screenshot", f"Saved at: {path}")

        # 将路径和URL写入运行内存，供后续节点或前端使用
        self.memory.set(self.info, "path", full_path)
        self.memory.set(self.info, "url", web_path)

        self.result.success()
        return self.result