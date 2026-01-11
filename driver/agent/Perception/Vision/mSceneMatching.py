# driver/tentacle/engine/vision/mSceneMatching.py
import cv2
import numpy as np
from driver.tentacle.engine.vision.mOcr import analyze as ocr_analyze  # 假设已存在的 OCR 封装
from script.log import SLog


class SceneMatcher:
    @staticmethod
    def match(current_img, node_data):
        """
        return: score (0.0 - 1.0)
        """
        if current_img is None: return 0.0

        # 1. 应用蒙版 (Masking) - 涂黑动态区域，避免干扰 OCR
        masked_img = current_img.copy()
        h, w = masked_img.shape[:2]

        masks = node_data.get("mask_areas", [])
        for mask in masks:
            # mask['rect'] = [x, y, w, h] (绝对坐标)
            mx, my, mw, mh = map(int, mask['rect'])
            # 将该区域填为纯黑，OCR 就看不到了
            cv2.rectangle(masked_img, (mx, my), (mx + mw, my + mh), (0, 0, 0), -1)

        # 2. OCR 识别 (只识别剩下的骨架部分)
        # ocr_analyze 返回 [{'text': '首页', 'box': [[x1,y1]...]}, ...]
        ocr_res = ocr_analyze(None, img=masked_img)
        if not ocr_res: return 0.0

        # 3. 锚点匹配 (Anchor Matching)
        anchors = node_data.get("anchors", [])
        if not anchors: return 0.0  # 无锚点无法匹配

        matched_count = 0
        for anchor in anchors:
            # 检查每个锚点是否出现在 OCR 结果中
            target_val = anchor['value']
            target_rect = anchor['rect']  # [x, y, w, h]

            for item in ocr_res:
                if target_val in item['text']:
                    # 进一步校验坐标是否在允许误差范围内
                    # 这里简化为只要文字存在就算匹配 (更严格可以用 IoU)
                    matched_count += 1
                    break

        return matched_count / len(anchors)