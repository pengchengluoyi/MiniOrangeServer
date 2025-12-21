# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter


@BaseRouter.route('public/trigger')
class Trigger(Template):
    """
        This component will start something
    """
    META = {
        "type": 200,  # 前端根据这个来渲染特殊的条件构造器UI
        "name": "开始",
        "icon": "play",  # 对应前端 iconMap 里的图标
        "inputs": [],
        "defaultData": {},
        "outputVars": []
    }

    def execute(self):
        self.result.success()
        return self.result

