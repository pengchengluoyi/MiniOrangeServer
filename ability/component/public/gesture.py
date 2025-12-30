# !/usr/bin/env python
# -*-coding:utf-8 -*-

import re
from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter

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
            "sub_type": "click",
            "locator_chain": [],
            "target_locator_chain": []
        },
        "outputVars": []
    }

    def calculate_sub_coords(self, full_text, pattern, box):
        """核心：通过正则匹配子串，并根据中英权重比例计算子区域中心坐标"""
        match = re.search(pattern, full_text)
        if not match: return None

        start_idx, end_idx = match.span()

        # 权重：中文2，其他1
        def get_w(c): return 2 if '\u4e00' <= c <= '\u9fff' else 1

        weights = [get_w(c) for c in full_text]
        total_w = sum(weights)

        pre_w = sum(weights[:start_idx])
        target_w = sum(weights[start_idx:end_idx])

        x_min = min(p[0] for p in box)
        x_max = max(p[0] for p in box)
        y_min = min(p[1] for p in box)
        y_max = max(p[1] for p in box)
        width = x_max - x_min

        # 映射像素
        sub_x_start = x_min + (width * (pre_w / total_w))
        sub_x_end = x_min + (width * ((pre_w + target_w) / total_w))

        return (int((sub_x_start + sub_x_end) / 2), int((y_min + y_max) / 2))

    def on_check(self):
        pass

    def execute(self):
        self.get_engine()
        sub_type = self.get_param_value("sub_type")
        locator_chain = self.get_param_value("locator_chain")

        from ability.component.public.ocr import analyze

        # 1. 截图并 OCR 识别
        img = self.engine.screenshot()
        ocr_result = analyze(None, img)
        self.memory.set(self.info, "ocr_result", ocr_result)

        # 2. 提取定位链中的文本/正则规则
        target_pattern = None
        for node in locator_chain:
            target_pattern = node.get("text") or node.get("desc")
            if target_pattern: break

        # 3. OCR 匹配并执行比例点击
        if target_pattern:
            for item in ocr_result:
                detected_text = item.get("text", "")
                box = item.get("coordinates", {}).get("box")

                # 使用正则和权重比例算法计算坐标
                center = self.calculate_sub_coords(detected_text, target_pattern, box)

                if center:
                    SLog.i(TAG, f"OCR Regex Matched! '{target_pattern}' in '{detected_text}' at {center}.")
                    if sub_type == 'double':
                        self.engine.double_click(None, position=center)
                    elif sub_type in ['right-click', 'long_press']:
                        self.engine.context_click(None, position=center)
                    elif sub_type == 'drag':
                        SLog.w(TAG, "Drag via OCR coords requires complex logic, falling back to UI elements.")
                        break  # 跳出循环，尝试走下方传统的 find_element 逻辑
                    else:
                        self.engine.click(None, position=center)
                    self.result.success()
                    return self.result

        # 4. 兜底逻辑：传统的 UI 自动化查找元素
        source = self.engine.find_element(locator_chain)
        if not source:
            SLog.e(TAG, f"Element not found via Locator or OCR (Target: {target_pattern})")
            self.result.fail()
            return self.result

        try:
            if sub_type == 'drag':
                target_locator_chain = self.get_param_value("target_locator_chain")
                target = self.engine.find_element(target_locator_chain)
                if not target:
                    self.result.fail()
                    return self.result
                self.engine.drag_and_drop(source, target)
            elif sub_type == 'double':
                self.engine.double_click(source)
            elif sub_type == 'hover':
                self.engine.hover(source)
            elif sub_type in ['right-click', 'long_press']:
                self.engine.context_click(source)
            else:
                self.engine.click(source)
            self.result.success()
        except Exception as e:
            SLog.e(TAG, f"Gesture action failed: {e}")
            self.result.fail()

        return self.result