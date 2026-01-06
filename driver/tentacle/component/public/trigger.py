# !/usr/bin/env python
# -*-coding:utf-8 -*-

from driver.tentacle.component.template import Template
from driver.tentacle.component.router import BaseRouter


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

