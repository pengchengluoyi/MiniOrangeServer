# !/usr/bin/env python
# -*-coding:utf-8 -*-

from ability.component.template import Template
from ability.component.router import BaseRouter

PID = "pid"
VERSION_NAME = "versionName"
VERSION_CODE = "versionCode"

@BaseRouter.route('public/stop')
class St(Template):
    """
        This component will stop something
    """
    META = {
        "type": 200,  # 前端根据这个来渲染特殊的条件构造器UI
        "name": "关闭/杀死",
        "icon": "shield-x",  # 对应前端 iconMap 里的图标
        "inputs": [
            {
                "name": "test_subject",
                "type": "str",
                "desc": "应用包名 (例如 com.tencent.mm)",
                "placeholder": "com.tencent.mm",
            }
        ],
        "defaultData": {
            "test_subject": "",  # 对应上面的 name
        },
        "outputVars": [
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        test_subject = self.get_param_value("test_subject")
        result = self.engine.app_stop(test_subject)
        self.result.success() if result else self.result.fail()
        return self.result

