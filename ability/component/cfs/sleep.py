# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.sleep import mSleep
from ability.component.template import Template
from ability.component.router import BaseRouter

TAG = "sleep"


@BaseRouter.route('cfs/sleep')
class Sleep(Template):
    """
        This component will
    """
    META = {
        "type": 200,  # 前端根据这个来渲染特殊的条件构造器UI
        "name": "等待",
        "icon": "bed",  # 对应前端 iconMap 里的图标
        "inputs": [
            {
                "name": "seconds",
                "type": "float",
                "desc": "暂停几秒",
                "placeholder": "1.0s",
            }
        ],
        "defaultData": {
            "seconds": 1.0
        },
        "outputVars": [
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        mSleep(float(self.get_param_value("seconds")))


