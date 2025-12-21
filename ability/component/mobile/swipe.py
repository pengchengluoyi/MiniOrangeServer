# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.sleep import mSleep
from ability.component.template import Template
from ability.component.router import BaseRouter


@BaseRouter.route('mobile/swipe')
class Swipe(Template):
    """
        a.driver.swipe(0.5, 0.99, 0.5, 0.5, 0.5)
    """
    META = {
        "type": 200,  # 前端根据这个来渲染特殊的条件构造器UI
        "name": "滑动-swipe",
        "icon": "gallery-horizontal-end",  # 对应前端 iconMap 里的图标
        "inputs": [
            {
                "name": "shortcutKey",
                "type": "select",
                "desc": "快捷键",
                "options": [
                    {"label": "回到首页", "value": "home"},
                    {"label": "打开控制中心", "value": "open_control_center"},
                    {"label": "打开后台", "value": "open_the_backend"}
                ],
                "defaultValue": "home",
                "multiple": False,  # 是否多选
                "clearable": True   # 是否可清空
            }
        ],
        "defaultData": {
            "shortcutKey": "home"
        },
        "outputVars": [
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        mPosition = self.get_param_value("position")
        mShortcutKey = self.get_param_value("shortcutKey")

        self.engine.driver.screen_on()
        if mShortcutKey:
            if mShortcutKey == "home":
                self.engine.driver.swipe(0.5, 0.99, 0.5, 0.5, 0.2)
            elif mShortcutKey == "open_control_center":
                self.engine.driver.swipe(0.7, 0.01, 0.7, 0.3, 0.2)
            elif mShortcutKey == "open_the_backend":
                self.engine.driver.swipe(0.5, 0.99, 0.5, 0.6, 0.1)
        else:
            self.engine.driver.swipe(float(mPosition[0]), float(mPosition[1]), float(mPosition[2]), float(mPosition[3]), float(mPosition[4]))
        mSleep(1)

        self.result.success()
        return self.result