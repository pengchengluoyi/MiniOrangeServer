# !/usr/bin/env python
# -*-coding:utf-8 -*-

import re, math
from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter
from ability.engine.vision.mPositionCalculation import PositionManager

TAG = "GESTURE"


@BaseRouter.route('public/gesture')
class Gesture(Template):
    """
        Mouse/Touch Gesture operations (Cross-Platform)
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
            },
            {
                "name": "locator_chain",
                "type": "list",
                "desc": "源元素 (起点)",
                "add_text": "添加节点",
                "sub_inputs": [
                    {"name": "id", "type": "str", "desc": "唯一标识", "placeholder": "ResourceID/AutoID/Name"},
                    {"name": "text", "type": "str", "desc": "文本/正则", "placeholder": "显示文本或正则表达式"},
                    {"name": "type", "type": "str", "desc": "控件类型", "placeholder": "Button/TextView"},
                    {"name": "desc", "type": "str", "desc": "描述", "placeholder": "无障碍描述"},
                    {"name": "xpath", "type": "str", "desc": "XPath", "placeholder": "//...",
                     "show_if": ["web", "android", "ios"]},
                    {"name": "css", "type": "str", "desc": "CSS", "placeholder": ".class", "show_if": ["web"]},
                    {"name": "index", "type": "int", "desc": "Index", "placeholder": "0"}
                ]
            },
            {
                "name": "target_locator_chain",
                "type": "list",
                "desc": "目标元素 (终点 - 仅拖拽)",
                "add_text": "添加节点",
                "sub_inputs": [
                    {"name": "id", "type": "str", "desc": "唯一标识", "placeholder": "ResourceID/AutoID/Name"},
                    {"name": "text", "type": "str", "desc": "文本/正则", "placeholder": "Text/Label/Title"},
                    {"name": "type", "type": "str", "desc": "控件类型", "placeholder": "Class/ControlType"},
                    {"name": "desc", "type": "str", "desc": "描述", "placeholder": "ContentDesc"},
                    {"name": "xpath", "type": "str", "desc": "XPath", "placeholder": "//...",
                     "show_if": ["web", "android", "ios"]},
                    {"name": "css", "type": "str", "desc": "CSS", "placeholder": ".class #id", "show_if": ["web"]},
                    {"name": "index", "type": "int", "desc": "Index", "placeholder": "0"}
                ]
            }
        ],
        "defaultData": {
            "platform": "",
            "interaction_id": "",
            "anchor_interaction_id": "",
            "sub_type": "click",
            "locator_chain": [],
            "target_locator_chain": []
        },
        "outputVars": []
    }

    def on_check(self):
        pass

    def execute(self):
        self.get_engine()
        interaction_id = self.get_param_value("interaction_id")
        anchor_id = self.get_param_value("anchor_interaction_id")
        locator_chain = self.get_param_value("locator_chain")
        sub_type = self.get_param_value("sub_type")

        current_img = self.engine.screenshot()

        # 🔥 调用统一的视觉调度接口
        final_pos = PositionManager.find_visual_target(interaction_id, anchor_id, locator_chain, current_img)

        if final_pos:
            return self._do_action(sub_type, final_pos)

        # DOM 兜底逻辑
        SLog.w(TAG, "OCR 失败，尝试 DOM 路径...")
        source_el = self.engine.find_element(locator_chain)
        if source_el:
            try:
                # 针对不同动作类型执行 DOM 操作
                self.engine.click(source_el)  # 示例逻辑
                self.result.success()
                return self.result
            except Exception as e:
                SLog.e(TAG, f"DOM 执行失败: {e}")
        return self.result.fail()

    def _find_best_ocr_match(self, label, ref_pos=None):
        """
        在 OCR 结果中寻找匹配项。
        如果有 ref_pos，则返回距离最近的；否则返回第一个匹配项。
        """
        from ability.component.public.ocr import analyze
        img = self.engine.screenshot()
        ocr_results = analyze(None, img)

        candidates = []
        for item in ocr_results:
            text = item.get("text", "")
            box = item.get("coordinates", {}).get("box")
            if label in text:
                pos = PositionCalculator.calculate_sub_coords(text, label, box)
                if pos: candidates.append(pos)

        if not candidates:
            return None

        if ref_pos:
            # 返回离数据库坐标最近的候选者 [消除 Hardcoding 歧义的关键]
            return min(candidates, key=lambda p: PositionCalculator.get_distance(ref_pos, p))

        return candidates[0]

    def _do_action(self, sub_type, pos):
        """统一执行点击动作并返回结果"""
        if sub_type == 'double':
            self.engine.double_click(None, position=pos)
        elif sub_type in ['right-click', 'long_press']:
            self.engine.context_click(None, position=pos)
        else:
            self.engine.click(None, position=pos)
        self.result.success()
        return self.result