# !/usr/bin/env python
# -*-coding:utf-8 -*-

from ability.component.template import Template
from ability.component.router import BaseRouter

from ability.manager import Manager

TAG = "WebComponent"


@BaseRouter.route('web/click')
class Click(Template):
    """
        This component will open a web page with a web browser.
    """
    META = {
        "type": 200,
        "name": "点击",
        "icon": "mouse-pointer",
        "inputs": [
            {
                "name": "locator_chain",  # 对应数据中的 key
                "type": "list",  # 核心：类型为列表/数组
                "desc": "定位链",  # 显示给用户的标题
                "add_text": "添加节点",  # (可选) 前端按钮显示的文字
                "sub_inputs": [
                    {
                        "name": "resourceId",
                        "type": "str",
                        "desc": "资源ID(resourceId)",
                        "placeholder": "com.example:id/btn_submit"
                    },
                    {
                        "name": "text",
                        "type": "str",
                        "desc": "文本内容(text)",
                        "placeholder": "确定"
                    },
                    {
                        "name": "classname",
                        "type": "str",
                        "desc": "类名(classname)",
                        "placeholder": "android.widget.TextView"
                    },
                    {
                        "name": "XPATH",
                        "type": "str",
                        "desc": "XPATH路径",
                        "placeholder": "//..."
                    },
                    {
                        "name": "description",
                        "type": "str",
                        "desc": "描述(desc)",
                        "placeholder": "无障碍描述"
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
            "locator_chain": []
        },
        "outputVars": [
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        self.engine = Manager().WEBEngine
        element = self.engine.find_element(locator_chain=self.get_param_value("locator_chain"))
        if element:
            element.click()
