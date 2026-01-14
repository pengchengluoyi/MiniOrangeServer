# driver/tentacle/component/std/gesture.py
# !/usr/bin/env python
# -*-coding:utf-8 -*-

from driver.tentacle.component.template import Template
from driver.tentacle.component.router import BaseRouter
from script.log import SLog
TAG = "GESTURE"


@BaseRouter.route('public/gesture')
class Gesture(Template):
    """
    [纯执行手势组件]
    只负责执行，不负责思考。
    依赖 CNS 传入的 _perception 数据进行定位。
    """
    META = {
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
                "name": "sub_type",
                "type": "select",
                "desc": "动作类型",
                "defaultValue": "click",
                "options": [
                    {"value": "click", "text": "单击"},
                    {"value": "double", "text": "双击"},
                    {"value": "right-click", "text": "右键", "show_if": ["web", "windows", "mac"]},
                    {"value": "long_press", "text": "长按"},
                    {"value": "hover", "text": "悬停 (Hover)", "show_if": ["windows", "mac", "web"]},
                    {"value": "drag", "text": "拖拽/滑动 (Drag/Swipe)"}
                ]
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
            }
        ],
        "defaultData": {
            "platform": "",
            "interaction_id": "",
            "anchor_interaction_id": "",
            "sub_type": "click"
        },
        "outputVars": []
    }

    def execute(self):
        try:
            self.get_engine()
            sub_type = self.get_param_value("sub_type")
            mPosition = self.get_param_value("position")
            SLog.i(TAG, f" 点击 @ {str(mPosition)}")
            self.result.success()
            return self._do_action(sub_type, mPosition)
        except Exception as e:
            self.result.fail(str(e))


    def _do_action(self, sub_type, position):
        try:
            if sub_type == 'click':
                self.engine.click(None, position=position)
            elif sub_type == 'double':
                self.engine.double_click(None, position=position)
            elif sub_type == 'long_press':
                self.engine.long_press(None, position=position)
            else:
                self.engine.click(None, position=position)
            self.result.success()
        except Exception as e:
            SLog.e(TAG, f"执行异常: {e}")
            self.result.fail()
        return self.result.to_dict()