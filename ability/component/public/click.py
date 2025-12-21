# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog

from ability.component.template import Template
from ability.component.router import BaseRouter

TAG = "CLICK"

@BaseRouter.route('public/click')
class Click(Template):
    """
        This component will click something
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
                "defaultValue": "android"
            },
            {
                "name": "sub_type",
                "type": "select",
                "desc": "点击类型",
                "defaultValue": "click",
                "options": [
                    {"value": "click", "text": "单击"},
                    {"value": "double", "text": "双击"},
                    {"value": "right-click", "text": "右键", "show_if": ["web", "windows", "mac"]},
                    {"value": "long_press", "text": "长按", "show_if": ["android", "ios"]}
                ]
            },
            {
                "name": "is_dom",
                "type": "bool",
                "desc": "是否使用DOM树",
                "defaultValue": True,
                "trueText": "DOM",  # 开启状态显示文本
                "falseText": "视觉",  # 关闭状态显示文本
                "required": False,
                "hidden": False,
                "disabled": False
            },
            {
                "name": "locator_chain",  # 对应数据中的 key
                "type": "list",  # 核心：类型为列表/数组
                "desc": "定位链",  # 显示给用户的标题
                "add_text": "添加节点",  # (可选) 前端按钮显示的文字
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
            "sub_type": "click",
            "is_dom": True,
            "locator_chain": []
        },
        "outputVars": [
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        sub_type = self.get_param_value("sub_type")
        mLocatorChain = self.get_param_value("locator_chain")
        
        # 使用统一的 find_element 接口
        element = self.engine.find_element(mLocatorChain)
        SLog.i(TAG, element)

        if element:
            if sub_type == 'double':
                self.engine.double_click(element)
            elif sub_type == 'right-click' or sub_type == 'long_press':
                self.engine.context_click(element)
            else:
                self.engine.click(element)
            self.result.success()
        else:
            SLog.e(TAG, "Element not found")
            self.result.fail()
        return self.result
