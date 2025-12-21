# !/usr/bin/env python
# -*-coding:utf-8 -*-

from ability.component.template import Template
from ability.component.router import BaseRouter


@BaseRouter.route('mobile/press')
class Press(Template):
    """
        # 按键事件
        d.press("home")        # 首页键
        d.press("back")        # 返回键
        d.press("menu")        # 菜单键
        d.press("power")       # 电源键
        d.press("volume_up")   # 音量+
        d.press("volume_down") # 音量-
        d.press("enter")       # 回车键
    """

    META = {
        "type": 200,  # 前端根据这个来渲染特殊的条件构造器UI
        "name": "模拟手机按键",
        "icon": "keyboard",  # 对应前端 iconMap 里的图标
        "inputs": [
            {
                "name": "event",
                "type": "select",
                "desc": "快捷键",
                "options": [
                    {"label": "Home键", "value": "home"},
                    {"label": "返回键", "value": "back"},
                    {"label": "菜单键", "value": "menu"},
                    {"label": "电源键", "value": "power"},
                    {"label": "音量+", "value": "volume_up"},
                    {"label": "音量-", "value": "volume_down"},
                    {"label": "回车键", "value": "enter"},
                ],
                "defaultValue": "home",
                "multiple": False,  # 是否多选
                "clearable": True   # 是否可清空
            }
        ],
        "defaultData": {
            "event": "home"
        },
        "outputVars": [
        ]
    }


    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        mEvent = self.get_param_value("event")

        self.engine.driver.screen_on()
        self.engine.driver.press(str(mEvent))

        self.result.success()
        return self.result
