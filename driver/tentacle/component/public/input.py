# !/usr/bin/env python
# -*-coding:utf-8 -*-

from driver.tentacle.component.template import Template
from driver.tentacle.component.router import BaseRouter
from driver.tentacle.engine.vision.mPositionCalculation import PositionManager

TAG = "INPUT"

@BaseRouter.route('public/input')
class Input(Template):
    """
        This component will input text
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
                "name": "text",
                "type": "str",
                "desc": "输入内容",
                "placeholder": "请输入要发送的文本"
            },
            {
                "name": "clear",
                "type": "bool",
                "desc": "输入前是否清空",
                "defaultValue": False,
                "trueText": "清空",
                "falseText": "不清空"
            },
            {
                "name": "interaction_id",
                "type": "interaction_select",
                "desc": "关联热区锚点",
                "placeholder": "从当前页面的热区列表中选择"
            },
            {
                "name": "anchor_interaction_id",
                "type": "interaction_select",
                "desc": "关联热区锚点 -- 辅助定位",
                "placeholder": "从当前页面的热区列表中选择"
            },
        ],
        "defaultData": {
            "platform": "",
            "text": "",
            "clear": False,
            "interaction_id": "",
            "anchor_interaction_id": ""
        },
        "outputVars": []
    }

    def on_check(self):
        pass

    def execute(self):
        self.get_engine()
        text = self.get_param_value("text")
        clear = self.get_param_value("clear")

        self.engine.send_keys(None, str(text))
        self.result.success()
        return self.result