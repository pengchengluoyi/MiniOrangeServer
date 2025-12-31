# ability/engine/vision/mPositionCalculation.py
import re
import math
from script.log import SLog
from ability.engine.vision.mOcr import analyze
from ability.engine.vision.mImageMatching import ImageVision
from server.core.database import SessionLocal
from server.models.AppGraph.app_component import AppComponent


class PositionManager:
    TAG = "VisionPos"

    @staticmethod
    def get_distance(p1, p2):
        """计算两点间的欧几里得距离"""
        return math.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2)

    @staticmethod
    def calculate_sub_coords(full_text, pattern, box):
        """通过正则匹配子串中心像素"""
        try:
            match = re.search(pattern, full_text)
        except:
            match = re.search(re.escape(pattern), full_text)
        if not match: return None

        start_idx, end_idx = match.span()

        def get_w(c):
            return 2 if '\u4e00' <= c <= '\u9fff' else 1

        weights = [get_w(c) for c in full_text]
        total_w = sum(weights)
        pre_w = sum(weights[:start_idx])
        target_w = sum(weights[start_idx:end_idx])

        x_min, x_max = min(p[0] for p in box), max(p[0] for p in box)
        y_min, y_max = min(p[1] for p in box), max(p[1] for p in box)
        width = x_max - x_min

        sub_x_start = x_min + (width * (pre_w / total_w))
        sub_x_end = x_min + (width * ((pre_w + target_w) / total_w))
        return (int((sub_x_start + sub_x_end) / 2), int((y_min + y_max) / 2))

    @classmethod
    def find_visual_target(cls, interaction_id, anchor_id, locator_chain, current_img):
        """
        统一视觉定位入口：判定模式并返回最终坐标
        优先级：数据库 ID(Label判定) -> 图像比对/OCR -> 锚点校准
        """
        target_label, db_target_pos = None, None
        anchor_label, db_anchor_pos = None, None

        # 1. 检索数据库
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

        # 2. 模式判定
        invalid_labels = {None, "", "new area", "icon"}
        is_pure_icon = str(target_label).strip().lower() in invalid_labels

        # 3. 执行匹配
        final_pos = None

        # 路径 A: 纯图标 -> 模板匹配
        if interaction_id and is_pure_icon:
            final_pos = ImageVision.get_template_match(interaction_id, current_img)
            if final_pos: return final_pos

        # 路径 B: 有文字 -> OCR
        if not target_label and locator_chain:
            target_label = locator_chain[0].get("text") if locator_chain else None

        if target_label:
            ocr_results = analyze(None, current_img)

            # 寻找当前屏幕锚点
            curr_anchor_pos = None
            if anchor_label:
                for item in ocr_results:
                    if anchor_label in item["text"]:
                        curr_anchor_pos = cls.calculate_sub_coords(item["text"], anchor_label,
                                                                   item["coordinates"]["box"])
                        if curr_anchor_pos: break

            # 寻找目标候选
            candidates = []
            for item in ocr_results:
                if target_label in item["text"]:
                    pos = cls.calculate_sub_coords(item["text"], target_label, item["coordinates"]["box"])
                    if pos: candidates.append(pos)

            if candidates:
                if curr_anchor_pos and db_anchor_pos and db_target_pos:
                    dx, dy = db_target_pos[0] - db_anchor_pos[0], db_target_pos[1] - db_anchor_pos[1]
                    pred_pos = (curr_anchor_pos[0] + dx, curr_anchor_pos[1] + dy)
                    final_pos = min(candidates, key=lambda p: cls.get_distance(pred_pos, p))
                elif db_target_pos:
                    final_pos = min(candidates, key=lambda p: cls.get_distance(db_target_pos, p))
                else:
                    final_pos = candidates[0]

        return final_pos