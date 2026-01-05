# !/usr/bin/env python
# -*-coding:utf-8 -*-
import time

from script.log import SLog
from script.sleep import mSleep
from ability.component.template import Template
from ability.component.router import BaseRouter
from ability.engine.vision.mPositionCalculation import PositionManager

TAG = "wait"


@BaseRouter.route('public/wait')
class Wait(Template):
    """
        This component will
    """
    META = {
        "type": 200,  # 前端根据这个来渲染特殊的条件构造器UI
        "name": "等待",
        "icon": "bed",  # 对应前端 iconMap 里的图标
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
                "defaultValue": ""
            },
            {
                "name": "display",
                "type": "bool",
                "desc": "等待目标显示或者隐藏",
                "defaultValue": True,
                "trueText": "目标显示时通过",
                "falseText": "目标隐藏时通过"
            },
            {
                "name": "interaction_id",
                "type": "interaction_select",
                "desc": "关联热区锚点",
                "placeholder": "从当前页面的热区列表中选择"
            },
            {
                "name": "anchor_interaction_id",
                "type": "interaction_select",
                "desc": "关联热区锚点 -- 辅助定位",
                "placeholder": "从当前页面的热区列表中选择"
            },
        ],
        "defaultData": {
            "platform": "",
            "interaction_id": "",
            "anchor_interaction_id": "",
            "display": True,
        },
        "outputVars": [
            {"key": "status", "type": "bool", "desc": "元素是否显示: True显示,False不显示"},
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        interaction_id = self.get_param_value("interaction_id")
        anchor_id = self.get_param_value("anchor_interaction_id")
        display = self.get_param_value("display")

        start_time = time.time()
        while time.time() - start_time <= 30:
            # 🔥 调用统一的视觉调度接口
            current_img = self.engine.screenshot()
            final_pos = PositionManager.find_visual_target(interaction_id, anchor_id, None, current_img)
            show_time = True if final_pos else False
            if display:
                if final_pos:
                    self.memory.set(self.info, "status", True)
                    self.result.success()
                    return True
            else:
                if not show_time:
                    self.memory.set(self.info, "status", True)
                    self.result.success()
                    return True
            mSleep(0.5)
            SLog.i(TAG, "while - end")

        self.memory.set(self.info, "status", False)
        return self.result.fail()


