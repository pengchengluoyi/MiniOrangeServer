# !/usr/bin/env python
# -*-coding:utf-8 -*-

import re, math
from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter
from server.core.database import SessionLocal
from server.models.AppGraph.app_component import AppComponent

TAG = "GESTURE"

def get_distance(p1, p2):
    """计算两点间的欧几里得距离"""
    return math.sqrt((p1[0] - p2[0])**2 + (p1[1] - p2[1])**2)

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
                "name": "anchor_locator_chain",
                "type": "list",
                "desc": "锚点元素 (唯一参考物)",
                "add_text": "添加锚点",
                "sub_inputs": [
                    {"name": "text", "type": "str", "desc": "锚点文本", "placeholder": "如：首页/设置/头像"},
                    {"name": "desc", "type": "str", "desc": "描述", "placeholder": "无障碍描述"}
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
            "sub_type": "click",
            "locator_chain": [],
            "anchor_locator_chain": [],
            "target_locator_chain": []
        },
        "outputVars": []
    }

    def calculate_sub_coords(self, full_text, pattern, box):
        """核心：通过正则匹配子串，并根据中英权重比例计算子区域中心坐标"""
        try:
            match = re.search(pattern, full_text)
        except:
            match = re.search(re.escape(pattern), full_text)

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
        anchor_chain = self.get_param_value("anchor_locator_chain")
        locator_chain = self.get_param_value("locator_chain")

        # 1. 提取锚点和目标关键字
        anchor_text = None
        if anchor_chain:
            for node in anchor_chain:
                anchor_text = node.get("text") or node.get("desc")
                if anchor_text: break

        target_text = None
        if locator_chain:
            for node in locator_chain:
                target_text = node.get("text") or node.get("desc")
                if target_text: break

        if not target_text:
            SLog.e(TAG, "未指定目标文本")
            self.result.fail()
            return self.result

        # 2. 执行 OCR 识别
        from ability.component.public.ocr import analyze
        img = self.engine.screenshot()
        ocr_results = analyze(None, img)

        anchor_pos = None
        candidates = []

        # 3. 遍历 OCR 结果，区分锚点和候选目标
        for item in ocr_results:
            text = item.get("text", "")
            box = item.get("coordinates", {}).get("box")
            center = item.get("coordinates", {}).get("center")

            # 寻找锚点坐标
            if anchor_text and anchor_text in text:
                # 使用你原有的精确子串坐标计算
                anchor_pos = self.calculate_sub_coords(text, anchor_text, box)

            # 寻找所有潜在目标
            if target_text in text:
                target_pos = self.calculate_sub_coords(text, target_text, box)
                if target_pos:
                    candidates.append(target_pos)

        # 4. 核心决策逻辑
        final_pos = None
        if not candidates:
            SLog.w(TAG, f"页面未找到目标文本: {target_text}")
        elif anchor_pos and candidates:
            # 模式 A: 有锚点，选离锚点最近的
            final_pos = min(candidates, key=lambda p: get_distance(anchor_pos, p))
            SLog.i(TAG, f"匹配成功！找到锚点 '{anchor_text}'，点击最近的目标 '{target_text}'")
        else:
            # 模式 B: 无锚点或未找到锚点，默认选第一个（兜底）
            final_pos = candidates[0]
            SLog.w(TAG, "未找到锚点，回退到默认匹配第一个目标")

        # 5. 执行点击
        if final_pos:
            self._perform_action(sub_type, final_pos)
            self.result.success()
            return self.result

        # 6. OCR 全败，尝试 DOM 兜底 (原有逻辑)
        # ...
        self.result.fail()
        return self.result

    def _perform_action(self, sub_type, pos):
        """执行具体的点击/长按动作"""
        if sub_type == 'double':
            self.engine.double_click(None, position=pos)
        elif sub_type in ['right-click', 'long_press']:
            self.engine.context_click(None, position=pos)
        else:
            self.engine.click(None, position=pos)