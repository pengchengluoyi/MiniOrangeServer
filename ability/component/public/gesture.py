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
        interaction_id = self.get_param_value("interaction_id")
        locator_chain = self.get_param_value("locator_chain")
        anchor_chain = self.get_param_value("anchor_locator_chain")
        sub_type = self.get_param_value("sub_type")

        # 1. 获取目标文本 (优先级: 数据库 > 配置链)
        target_label = None
        db_ref_pos = None

        if interaction_id:

            SLog.i(TAG, "interaction_id")
            SLog.i(TAG, f"({interaction_id})")
            db = SessionLocal()
            try:
                comp = db.query(AppComponent).filter(AppComponent.uid == interaction_id).first()
                SLog.i(TAG, str(comp.label))
                SLog.i(TAG, str(comp.label))
                if comp:
                    target_label = comp.label
                    # 数据库存的是原始截图下的中心点
                    db_ref_pos = (comp.x + comp.width / 2, comp.y + comp.height / 2)
            finally:
                db.close()

        if not target_label and locator_chain:
            for node in locator_chain:
                target_label = node.get("text") or node.get("desc")
                if target_label: break

        if not target_label:
            SLog.e(TAG, "未指定目标点击文本")
            self.result.fail()
            return self.result

        # 2. 执行 OCR 获取全屏结果
        from ability.component.public.ocr import analyze
        img = self.engine.screenshot()
        ocr_results = analyze(None, img)

        # 3. 确定“参考坐标” (Reference Point)
        ref_pos = None

        # 优先级 A: 如果有 interaction_id，参考点是数据库坐标
        if db_ref_pos:
            ref_pos = db_ref_pos
            SLog.i(TAG, f"使用数据库坐标作为参考点: {ref_pos}")

        # 优先级 B: 如果提供了锚点元素，通过 OCR 找到锚点的位置作为参考点
        elif anchor_chain:
            anchor_text = None
            for node in anchor_chain:
                anchor_text = node.get("text") or node.get("desc")
                if anchor_text: break

            if anchor_text:
                SLog.i(TAG, f"正在寻找锚点: {anchor_text}...")
                for item in ocr_results:
                    if anchor_text in item["text"]:
                        # 找到锚点，将其坐标设为参考点
                        ref_pos = self.calculate_sub_coords(item["text"], anchor_text, item["coordinates"]["box"])
                        if ref_pos:
                            SLog.i(TAG, f"锚点定位成功: {anchor_text} -> {ref_pos}")
                            break

        # 4. 寻找目标文本的候选坐标
        candidates = []
        for item in ocr_results:
            text = item.get("text", "")
            box = item.get("coordinates", {}).get("box")
            if target_label in text:
                pos = self.calculate_sub_coords(text, target_label, box)
                if pos: candidates.append(pos)

        # 5. 决策与执行
        final_pos = None
        if candidates:
            if ref_pos:
                # 关键：寻找离参考点（数据库坐标或锚点坐标）最近的那个目标
                final_pos = min(candidates, key=lambda p: get_distance(ref_pos, p))
                SLog.i(TAG, f"基于参考点匹配到最近目标: {target_label}")
            else:
                # 没有任何参考，默认选第一个
                final_pos = candidates[0]
                SLog.w(TAG, f"无参考点，默认点击第一个匹配项: {target_label}")

        if final_pos:
            self._do_action(sub_type, final_pos)
            self.result.success()
            return self.result



        # --- 优先级 3: 走传统的 locator_chain (DOM 定位) ---
        SLog.w(TAG, "OCR 路径全部失败，尝试 DOM 路径兜底...")
        source_el = self.engine.find_element(locator_chain)
        if source_el:
            try:
                # 针对不同动作类型执行 DOM 操作
                self.engine.click(source_el)  # 示例逻辑
                self.result.success()
                return self.result
            except Exception as e:
                SLog.e(TAG, f"DOM 执行失败: {e}")

        self.result.fail()
        return self.result

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