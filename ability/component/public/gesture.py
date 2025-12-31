# !/usr/bin/env python
# -*-coding:utf-8 -*-

import re, math
from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter
from ability.engine.vision.mImageMatching import ImageVision
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
        interaction_id = self.get_param_value("interaction_id")
        anchor_id = self.get_param_value("anchor_interaction_id")
        locator_chain = self.get_param_value("locator_chain")
        sub_type = self.get_param_value("sub_type")

        target_label, db_target_pos = None, None
        anchor_label, db_anchor_pos = None, None

        # 1. 获取数据库信息
        db = SessionLocal()
        try:
            if interaction_id:
                comp = db.query(AppComponent).filter(AppComponent.uid == interaction_id).first()
                if comp:
                    target_label = comp.label
                    db_target_pos = (comp.x + comp.width / 2, comp.y + comp.height / 2)
            if anchor_id:
                a_comp = db.query(AppComponent).filter(AppComponent.uid == anchor_id).first()
                if a_comp:
                    anchor_label = a_comp.label
                    db_anchor_pos = (a_comp.x + a_comp.width / 2, a_comp.y + a_comp.height / 2)
        finally:
            db.close()

        # 2. 识别“纯图标”模式
        # 增加对特殊字符的判定
        invalid_labels = {None, "", "new area", "icon"}
        is_pure_icon = str(target_label).strip().lower() in invalid_labels

        current_img = self.engine.screenshot()
        final_pos = None

        # --- 优先级逻辑 ---
        # 优先级 A: 如果是纯图标或无效文字，直接走图像比对
        if interaction_id and is_pure_icon:
            SLog.i(TAG, f"Label 为 '{target_label}'，启动 ImageVision 模板匹配...")
            final_pos = ImageVision.get_template_match(interaction_id, current_img)

        # 优先级 B: 走 OCR 逻辑
        if not final_pos:
            # 如果 interaction_id 没拿到 label，尝试从 locator_chain 拿
            if not target_label and locator_chain:
                for node in locator_chain:
                    target_label = node.get("text")
                    if target_label: break

            if target_label:
                SLog.i(TAG, f"执行 OCR 匹配: {target_label}")
                from ability.engine.vision.mOcr import analyze  #
                ocr_results = analyze(None, current_img)

                # 寻找实时锚点
                current_anchor_pos = None
                if anchor_label:
                    for item in ocr_results:
                        if anchor_label in item["text"]:
                            current_anchor_pos = self.calculate_sub_coords(item["text"], anchor_label,
                                                                           item["coordinates"]["box"])
                            if current_anchor_pos: break

                # 寻找目标候选
                candidates = []
                for item in ocr_results:
                    if target_label in item["text"]:
                        pos = self.calculate_sub_coords(item["text"], target_label, item["coordinates"]["box"])
                        if pos: candidates.append(pos)

                if candidates:
                    if current_anchor_pos and db_anchor_pos and db_target_pos:
                        # 偏移校准逻辑
                        dx, dy = db_target_pos[0] - db_anchor_pos[0], db_target_pos[1] - db_anchor_pos[1]
                        predicted_pos = (current_anchor_pos[0] + dx, current_anchor_pos[1] + dy)
                        final_pos = min(candidates, key=lambda p: get_distance(predicted_pos, p))
                    elif db_target_pos:
                        final_pos = min(candidates, key=lambda p: get_distance(db_target_pos, p))
                    else:
                        final_pos = candidates[0]

        # 3. 执行动作
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
                pos = self.calculate_sub_coords(text, label, box)
                if pos: candidates.append(pos)

        if not candidates:
            return None

        if ref_pos:
            # 返回离数据库坐标最近的候选者 [消除 Hardcoding 歧义的关键]
            return min(candidates, key=lambda p: get_distance(ref_pos, p))

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