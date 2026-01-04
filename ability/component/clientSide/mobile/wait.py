# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter


import script.constPath.component_code as m_code


@BaseRouter.route('mobile/wait')
class Wait(Template):
    """
        This component
    """
    META = {
        "type": 200,  # 前端根据这个来渲染特殊的条件构造器UI
        "name": "等待元素显示",
        "icon": "loader",  # 对应前端 iconMap 里的图标
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
            },
            {
                "name": "timeout",
                "type": "int",
                "desc": "等待超时时间(s)",
                "placeholder": "5s",
            },
        ],
        "defaultData": {
            "locator_chain": [],
            "timeout": 5
        },
        "outputVars": [
            {"key": "is_display", "type": "bool", "desc": "元素是否显示: True显示,False不显示"},
        ]
    }

    def __init__(self, *args, **kwargs):
        super(Wait, self).__init__(*args, **kwargs)
        self.result_type = m_code.MCFS_IF

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        mLocatorChain = self.get_param_value("locator_chain")
        mTimeout = self.get_param_value("timeout")

        if not mLocatorChain:
            return self.result

        try:
            build_chain = self.engine.build_chain(mLocatorChain)
            self.engine.driver.screen_on()
            element_exists = build_chain.exists(timeout=mTimeout)
            if element_exists:
                self.result.success()
                self.memory.set(self.info, "is_display", True)
            else:
                self.memory.set(self.info, "is_display", False)
            SLog.i("wait", element_exists)
            return self.result
        except (IndexError, ValueError, AttributeError) as e:
            SLog.e("wait", e)
            self.memory.set(self.info, "is_display", False)
            return self.result



