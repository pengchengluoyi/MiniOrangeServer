# ability/engine/vision/mPositionCalculation.py
import re
import math
from script.log import SLog
from driver.agent.Perception.Vision.mImageMatching import ImageVision


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
        final_pos = None

        # 路径 A: 纯图标 -> 模板匹配
        if interaction_id:
            final_pos = ImageVision.get_template_match(interaction_id, current_img)
            return final_pos
        return final_pos