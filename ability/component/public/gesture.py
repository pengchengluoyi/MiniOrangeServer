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
        target_chain = self.get_param_value("target_locator_chain")

        # 1. 确定起点文本 (Source)
        src_label = None
        if interaction_id:
            db = SessionLocal()
            try:
                comp = db.query(AppComponent).filter(AppComponent.uid == interaction_id).first()
                if comp: src_label = comp.label
            finally:
                db.close()

        if not src_label and locator_chain:
            for node in locator_chain:
                src_label = node.get("text") or node.get("desc")
                if src_label: break

        # 2. 确定终点文本 (Target - 仅在拖拽模式下需要)
        dst_label = None
        if sub_type == 'drag' and target_chain:
            for node in target_chain:
                dst_label = node.get("text") or node.get("desc")
                if dst_label: break

        # 3. 尝试 OCR 路径
        if src_label:
            from ability.component.public.ocr import analyze
            img = self.engine.screenshot()
            ocr_results = analyze(None, img)

            src_pos, dst_pos = None, None

            # 在 OCR 结果中寻找起点和终点
            for item in ocr_results:
                text = item.get("text", "")
                box = item.get("coordinates", {}).get("box")

                if not src_pos:
                    src_pos = self.calculate_sub_coords(text, src_label, box)

                if sub_type == 'drag' and dst_label and not dst_pos:
                    dst_pos = self.calculate_sub_coords(text, dst_label, box)

            # OCR 执行逻辑
            if sub_type == 'drag':
                if src_pos and dst_pos:
                    SLog.i(TAG, f"OCR 拖拽成功: 从 {src_pos} 拖至 {dst_pos}")
                    self.engine.drag_and_drop(None, None, start_pos=src_pos, end_pos=dst_pos)
                    self.result.success()
                    return self.result
            elif src_pos:
                # 非拖拽动作
                if sub_type == 'double':
                    self.engine.double_click(None, position=src_pos)
                elif sub_type in ['right-click', 'long_press']:
                    self.engine.context_click(None, position=src_pos)
                else:
                    self.engine.click(None, position=src_pos)
                self.result.success()
                return self.result

        # 4. 兜底逻辑：DOM 定位
        SLog.w(TAG, "OCR 匹配失败或操作类型不支持，回退到 DOM 定位...")
        source_el = self.engine.find_element(locator_chain)
        if source_el:
            try:
                if sub_type == 'drag':
                    target_el = self.engine.find_element(target_chain)
                    if target_el:
                        self.engine.drag_and_drop(source_el, target_el)
                        self.result.success()
                        return self.result
                # 其他 DOM 点击动作...
                self.engine.click(source_el)  # 示例简写
                self.result.success()
                return self.result
            except Exception as e:
                SLog.e(TAG, f"DOM 执行失败: {e}")

        self.result.fail()
        return self.result