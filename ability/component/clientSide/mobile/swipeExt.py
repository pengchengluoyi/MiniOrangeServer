# !/usr/bin/env python
# -*-coding:utf-8 -*-

from ability.component.template import Template
from ability.component.router import BaseRouter


@BaseRouter.route('mobile/swipeExt')
class SwipeExt(Template):
    """
        4选1 "left", "right", "up", "down"
    """
    META = {
        "type": 200,  # 前端根据这个来渲染特殊的条件构造器UI
        "name": "滑动扩展-swipeEXT",
        "icon": "gallery-vertical",  # 对应前端 iconMap 里的图标
        "inputs": [
            {
                "name": "orientation",
                "type": "select",
                "desc": "滑动方向",
                "options": [
                    {"label": "右", "value": "right"},
                    {"label": "左", "value": "left"},
                    {"label": "上", "value": "up"},
                    {"label": "下", "value": "down"}
                ],
                "defaultValue": "right",
                "multiple": False,  # 是否多选
                "clearable": True   # 是否可清空
            }
        ],
        "defaultData": {
            "orientation": "right"
        },
        "outputVars": [
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        mOrientation = self.get_param_value("orientation")
        self.engine.driver.screen_on()
        self.engine.driver.swipe_ext(mOrientation["value"], scale=0.8)

        self.result.success()
        return self.result
