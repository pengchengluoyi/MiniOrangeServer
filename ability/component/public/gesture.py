# !/usr/bin/env python
# -*-coding:utf-8 -*-

import re
from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter
from server.core.database import SessionLocal
from server.models.AppGraph.app_component import AppComponent

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
            "sub_type": "click",
            "locator_chain": [],
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
        interaction_id = self.get_param_value("interaction_id")
        locator_chain = self.get_param_value("locator_chain")

        # 1. 确定目标文本 (ID 优先)
        target_label = None
        if interaction_id:
            db = SessionLocal()
            try:
                comp = db.query(AppComponent).filter(AppComponent.uid == interaction_id).first()
                if comp:
                    target_label = comp.label
                    SLog.i(TAG, f"通过 ID {interaction_id} 获得锚点文本: {target_label}")
            finally:
                db.close()

        # 如果没有 ID 对应的 Label，尝试从定位链获取
        if not target_label and locator_chain:
            for node in locator_chain:
                target_label = node.get("text") or node.get("desc")
                if target_label: break

        # 2. 尝试 OCR 路径 (仅限非拖拽操作，拖拽通常需要两个元素，OCR 较难处理)
        if target_label and sub_type != 'drag':
            from ability.component.public.ocr import analyze
            img = self.engine.screenshot()
            ocr_result = analyze(None, img)

            for item in ocr_result:
                center = self.calculate_sub_coords(item.get("text", ""), target_label,
                                                   item.get("coordinates", {}).get("box"))
                if center:
                    SLog.i(TAG, f"OCR 匹配成功: '{target_label}' -> '{item.get('text')}' 坐标: {center}")
                    if sub_type == 'double':
                        self.engine.double_click(None, position=center)
                    elif sub_type in ['right-click', 'long_press']:
                        self.engine.context_click(None, position=center)
                    elif sub_type == 'hover':
                        self.engine.hover(None, position=center)
                    else:
                        self.engine.click(None, position=center)

                    self.result.success()
                    return self.result
            SLog.w(TAG, f"OCR 未在屏幕找到文本 '{target_label}'，尝试回退到 DOM 定位...")

        # 3. 兜底逻辑：传统的 DOM/UI 自动化
        if locator_chain:
            source = self.engine.find_element(locator_chain)
            if source:
                try:
                    if sub_type == 'drag':
                        target_chain = self.get_param_value("target_locator_chain")
                        target = self.engine.find_element(target_chain)
                        if target:
                            self.engine.drag_and_drop(source, target)
                            self.result.success()
                            return self.result
                    elif sub_type == 'double':
                        self.engine.double_click(source)
                    elif sub_type == 'hover':
                        self.engine.hover(source)
                    elif sub_type in ['right-click', 'long_press']:
                        self.engine.context_click(source)
                    else:
                        self.engine.click(source)

                    self.result.success()
                    return self.result
                except Exception as e:
                    SLog.e(TAG, f"DOM 操作执行失败: {e}")
            else:
                SLog.e(TAG, "DOM 定位器也未能找到目标元素")

        # 4. 最终失败处理
        self.result.fail()
        return self.result