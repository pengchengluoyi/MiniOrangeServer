# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter

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
                "name": "locator_chain",
                "type": "list",
                "desc": "定位链",
                "add_text": "添加节点",
                "sub_inputs": [
                    {
                        "name": "id",
                        "type": "str",
                        "desc": "唯一标识 (ID/Name/AutoID)",
                        "placeholder": "ResourceID/AutoID/Name"
                    },
                    {
                        "name": "text",
                        "type": "str",
                        "desc": "文本/标题 (Text/Label/Title)",
                        "placeholder": "显示文本/Window Title"
                    },
                    {
                        "name": "type",
                        "type": "str",
                        "desc": "控件类型 (Class/ControlType/Role)",
                        "placeholder": "Button/TextView/XCUIElementTypeButton"
                    },
                    {
                        "name": "desc",
                        "type": "str",
                        "desc": "描述 (ContentDesc/Help)",
                        "placeholder": "无障碍描述",
                        "show_if": ["android", "ios"]
                    },
                    {
                        "name": "xpath",
                        "type": "str",
                        "desc": "XPath (Web/Mobile)",
                        "placeholder": "//...",
                        "show_if": ["web", "android", "ios"]
                    },
                    {
                        "name": "css",
                        "type": "str",
                        "desc": "CSS Selector (Web)",
                        "placeholder": ".class #id",
                        "show_if": ["web"]
                    },
                    {
                        "name": "index",
                        "type": "int",
                        "desc": "常用语定位列表中的第几位",
                        "placeholder": "0"
                    }
                ]
            }
        ],
        "defaultData": {
            "platform": "",
            "text": "",
            "clear": False,
            "locator_chain": []
        },
        "outputVars": []
    }

    def on_check(self):
        pass

    def execute(self):
        self.get_engine()
        text = self.get_param_value("text")
        clear = self.get_param_value("clear")
        mLocatorChain = self.get_param_value("locator_chain")

        element = self.engine.find_element(mLocatorChain)
        
        if element:
            if clear:
                self.engine.clear(element)
            
            if text is not None:
                self.engine.send_keys(element, str(text))
            
            self.result.success()
        else:
            SLog.e(TAG, "Element not found")
            self.result.fail()
            
        return self.result