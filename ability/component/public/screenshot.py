# !/usr/bin/env python
# -*-coding:utf-8 -*-

import os
import sys
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
            {"key": "path", "type": "str", "desc": "截图保存路径"},
            {"key": "url", "type": "str", "desc": "图片Web路径"}
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
        
        # ⬆️ 路径修复：不再依赖 os.getcwd()，而是基于当前文件位置回溯
        # 当前文件: ability/component/public/screenshot.py
        if getattr(sys, 'frozen', False):
            base_dir = os.path.dirname(sys.executable)
        else:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            # 回退 3 级找到项目根目录 (MiniOrangeServer)
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(current_dir)))
        
        # 修正：直接拼接 base_dir，确保在项目内的 uploads 目录
        save_dir = os.path.join(base_dir, "uploads")

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