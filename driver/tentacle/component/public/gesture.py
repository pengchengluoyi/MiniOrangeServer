# driver/tentacle/component/public/gesture.py
# !/usr/bin/env python
# -*-coding:utf-8 -*-

from driver.tentacle.component.template import Template
from driver.tentacle.component.router import BaseRouter
from script.log import SLog

TAG = "GESTURE"


@BaseRouter.route("public/gesture")
class Gesture(Template):
    """
    跨平台手势：点击 / 双击 / 长按 / 拖拽等，由引擎抹平 iOS / Android 差异。
  坐标点击：data.position + normalized；热区点击：interaction_id（由 Orchestrator + Planner 定位）。
    """

    META = {
        "inputs": [
            {
                "name": "platform",
                "type": "select",
                "desc": "适用平台",
                "options": [
                    {"value": "android", "text": "Android"},
                    {"value": "ios", "text": "iOS"},
                    {"value": "windows", "text": "Windows"},
                    {"value": "mac", "text": "macOS"},
                    {"value": "web", "text": "Web"},
                ],
                "defaultValue": "",
            },
            {
                "name": "sub_type",
                "type": "select",
                "desc": "动作类型",
                "defaultValue": "click",
                "options": [
                    {"value": "click", "text": "单击"},
                    {"value": "double", "text": "双击"},
                    {
                        "value": "right-click",
                        "text": "右键",
                        "show_if": ["web", "windows", "mac"],
                    },
                    {"value": "long_press", "text": "长按"},
                    {
                        "value": "hover",
                        "text": "悬停",
                        "show_if": ["windows", "mac", "web"],
                    },
                    {"value": "drag", "text": "拖拽/滑动"},
                ],
            },
            {
                "name": "position",
                "type": "list",
                "desc": "坐标 [x, y]；normalized=true 时为 0~1（0.5,0.5=屏幕中心）",
                "placeholder": "[0.5, 0.5]",
            },
            {
                "name": "normalized",
                "type": "bool",
                "desc": "position 是否为归一化坐标",
                "defaultValue": False,
            },
            {
                "name": "interaction_id",
                "type": "interaction_select",
                "desc": "热区锚点（与 position 二选一，优先 position）",
                "placeholder": "从热区列表选择",
            },
            {
                "name": "anchor_interaction_id",
                "type": "interaction_select",
                "desc": "辅助定位热区",
                "placeholder": "可选",
            },
        ],
        "defaultData": {
            "platform": "",
            "sub_type": "click",
            "position": [],
            "normalized": False,
            "interaction_id": "",
            "anchor_interaction_id": "",
        },
        "outputVars": [],
    }

    def execute(self):
        try:
            self.get_engine()
            sub_type = self.get_param_value("sub_type") or "click"
            platform = self.get_param_value("platform") or self.info.platform
            position = self.get_param_value("position")
            normalized = bool(self.get_param_value("normalized"))

            pixel_pos = None
            if position and len(position) >= 2:
                if hasattr(self.engine, "position_to_pixels"):
                    pixel_pos = self.engine.position_to_pixels(
                        float(position[0]),
                        float(position[1]),
                        normalized=normalized,
                    )
                else:
                    pixel_pos = (int(float(position[0])), int(float(position[1])))

            if hasattr(self.engine, "screen_on"):
                self.engine.screen_on()
            SLog.i(TAG, f"{sub_type} @ {pixel_pos or position} platform={platform}")
            self._do_action(sub_type, pixel_pos)
            return self.result
        except Exception as e:
            SLog.e(TAG, str(e))
            self.result.fail(str(e))
            return self.result

    def _do_action(self, sub_type, position):
        try:
            if sub_type == "click":
                self.engine.click(None, position=position)
            elif sub_type == "double":
                self.engine.double_click(None, position=position)
            elif sub_type == "long_press":
                self.engine.long_press(None, position=position)
            elif sub_type == "drag":
                self.result.fail("drag 需起点终点，请用热区或后续扩展")
                return
            else:
                self.engine.click(None, position=position)
            self.result.success()
        except Exception as e:
            SLog.e(TAG, f"执行异常: {e}")
            self.result.fail(str(e))
