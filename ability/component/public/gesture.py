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
                    {
                        "name": "id",
                        "type": "str",
                        "desc": "唯一标识 (ID/Name/AutoID)",
                        "placeholder": "ResourceID/AutoID/Name"
                    },
                    {
                        "name": "text",
                        "type": "str",
                        "desc": "文本/标题 (Text/Label/Title)",
                        "placeholder": "显示文本/Window Title"
                    },
                    {
                        "name": "type",
                        "type": "str",
                        "desc": "控件类型 (Class/ControlType/Role)",
                        "placeholder": "Button/TextView/XCUIElementTypeButton"
                    },
                    {
                        "name": "desc",
                        "type": "str",
                        "desc": "描述 (ContentDesc/Help)",
                        "placeholder": "无障碍描述"
                    },
                    {
                        "name": "xpath",
                        "type": "str",
                        "desc": "XPath (Web/Mobile)",
                        "placeholder": "//...",
                        "show_if": ["web", "android", "ios"]
                    },
                    {
                        "name": "css",
                        "type": "str",
                        "desc": "CSS Selector (Web)",
                        "placeholder": ".class #id",
                        "show_if": ["web"]
                    },
                    {
                        "name": "index",
                        "type": "int",
                        "desc": "常用语定位列表中的第几位",
                        "placeholder": "0"
                    }
                ]
            },
            {
                "name": "target_locator_chain",
                "type": "list",
                "desc": "目标元素 (终点 - 仅拖拽)",
                "add_text": "添加节点",
                # 复用相同的定位结构
                "sub_inputs": [
                    {"name": "id", "type": "str", "desc": "唯一标识", "placeholder": "ResourceID/AutoID/Name"},
                    {"name": "text", "type": "str", "desc": "文本/标题", "placeholder": "Text/Label/Title"},
                    {"name": "type", "type": "str", "desc": "控件类型", "placeholder": "Class/ControlType"},
                    {"name": "desc", "type": "str", "desc": "描述", "placeholder": "ContentDesc"},
                    {"name": "xpath", "type": "str", "desc": "XPath", "placeholder": "//...", "show_if": ["web", "android", "ios"]},
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

    def on_check(self):
        pass

    def get_match_and_coordinates(self, full_text, pattern, box):
        """
        核心函数：通过正则匹配子串，并计算该子串在 Box 中的精确比例坐标
        """
        # 1. 执行正则搜索
        match = re.search(pattern, full_text)
        if not match:
            return None

        # 获取匹配到的起始和结束字符索引
        start_idx, end_idx = match.span()
        target_text = match.group()

        # 2. 计算权重（处理中英文宽度差异，中文计2，英文/数字计1）
        def get_w(char):
            return 2 if '\u4e00' <= char <= '\u9fff' else 1

        weights = [get_w(c) for c in full_text]
        total_weight = sum(weights)

        # 3. 计算目标子串在整体中的权重区间
        pre_weight = sum(weights[:start_idx])
        target_weight = sum(weights[start_idx: end_idx])

        # 4. 解析 Box 物理边界 (RapidOCR 返回 [[x1,y1], [x2,y2], [x3,y3], [x4,y4]])
        x_min = min(p[0] for p in box)
        x_max = max(p[0] for p in box)
        y_min = min(p[1] for p in box)
        y_max = max(p[1] for p in box)
        full_width = x_max - x_min

        # 5. 映射比例到像素坐标
        relative_start = pre_weight / total_weight
        relative_end = (pre_weight + target_weight) / total_weight

        sub_x_start = x_min + (full_width * relative_start)
        sub_x_end = x_min + (full_width * relative_end)

        center_x = int((sub_x_start + sub_x_end) / 2)
        center_y = int((y_min + y_max) / 2)

        return (center_x, center_y), target_text

    def execute(self):
        self.get_engine()
        sub_type = self.get_param_value("sub_type")
        locator_chain = self.get_param_value("locator_chain")

        from ability.component.public.ocr import analyze

        # 1. 截图并识别
        img = self.engine.screenshot()
        ocr_result = analyze(None, img)
        self.memory.set(self.info, "ocr_result", ocr_result)

        # 2. 获取目标文本或正则规则
        target_pattern = None
        for node in locator_chain:
            # 这里用户既可以输入 "搜索"，也可以输入 "搜索\d+"
            target_pattern = node.get("text") or node.get("desc")
            if target_pattern:
                break

        # 3. OCR 匹配逻辑
        if target_pattern:
            for item in ocr_result:
                detected_text = item.get("text", "")
                box = item.get("coordinates", {}).get("box")

                # --- 🔑 正则匹配 + 比例计算点 ---
                match_res = self.get_match_and_coordinates(detected_text, target_pattern, box)

                if match_res:
                    center, matched_text = match_res
                    SLog.i(TAG,
                           f"OCR Regex Matched! Pattern: '{target_pattern}' matched '{matched_text}' in '{detected_text}'. Position: {center}")

                    # 4. 执行动作
                    if sub_type == 'double':
                        self.engine.double_click(None, position=center)
                    elif sub_type in ['right-click', 'long_press']:
                        self.engine.context_click(None, position=center)
                    else:
                        self.engine.click(None, position=center)

                    self.result.success()
                    return self.result

        source = self.engine.find_element(locator_chain)
        if not source:
            SLog.e(TAG, f"Element not found via Locator or OCR (Target pattern: {target_pattern})")
            self.result.fail()
            return self.result

        try:
            if sub_type == 'drag':
                target_locator_chain = self.get_param_value("target_locator_chain")
                target = self.engine.find_element(target_locator_chain)
                
                if not target:
                    SLog.e(TAG, "Target element not found for drag")
                    self.result.fail()
                    return self.result
                
                self.engine.drag_and_drop(source, target)
            elif sub_type == 'click':
                self.engine.click(source)
            elif sub_type == 'double':
                self.engine.double_click(source)
            elif sub_type == 'right-click':
                self.engine.context_click(source)
            elif sub_type == 'long_press':
                self.engine.context_click(source)
            elif sub_type == 'hover':
                self.engine.hover(source)
            else:
                self.engine.click(source)
            self.result.success()
        except Exception as e:
            SLog.e(TAG, f"Gesture action failed: {e}")
            self.result.fail()

        return self.result