# ability/engine/vision/mImageMatching.py

import cv2
import numpy as np
import os
from pathlib import Path

from script.log import SLog
from script.mPath import UPLOAD_DIR, get_final_path
from server.core.database import SessionLocal
from server.models.AppGraph.app_component import AppComponent


class ImageVision:
    TAG = "ImageVision"

    @staticmethod
    def get_template_match(interaction_id, current_screenshot_np, threshold=0.8):
        """
        方案 B: 实时从原始截图裁剪模板并匹配
        """
        db = SessionLocal()
        try:
            # 1. 获取组件及其所属节点的原始截图路径
            comp = db.query(AppComponent).filter(AppComponent.uid == interaction_id).first()
            if not comp or not comp.node or not comp.node.screenshot_path:
                SLog.e(ImageVision.TAG, f"未找到热区或原始截图: {interaction_id}")
                return None

            # 2. 读取原始大图
            orig_path = get_final_path(comp.node.screenshot_path)
            orig_img = cv2.imdecode(np.fromfile(orig_path, dtype=np.uint8), cv2.IMREAD_COLOR)

            if orig_img is None:
                SLog.e(ImageVision.TAG, f"无法读取原始截图: {orig_path}")
                return None

            # 3. 根据数据库记录的 x, y, w, h 裁剪出模板小图
            x, y, w, h = int(comp.x), int(comp.y), int(comp.width), int(comp.height)
            template = orig_img[y:y + h, x:x + w]

            # 4. 执行模板匹配
            return ImageVision._do_match(current_screenshot_np, template, threshold)
        finally:
            db.close()

    @staticmethod
    def _do_match(target_img, template, threshold):
        """增强版匹配：支持多尺度以应对窗口缩放"""
        target_gray = cv2.cvtColor(target_img, cv2.COLOR_BGR2GRAY)
        template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

        tw, th = template_gray.shape[::-1]
        best_match = {"max_val": -1, "max_loc": None, "scale": 1.0}

        # 遍历三个缩放维度 (0.9, 1.0, 1.1)
        for scale in [0.9, 1.0, 1.1]:
            if scale != 1.0:
                resized_tpl = cv2.resize(template_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            else:
                resized_tpl = template_gray

            # 确保模板不大于目标图
            if resized_tpl.shape[0] > target_gray.shape[0] or resized_tpl.shape[1] > target_gray.shape[1]:
                continue

            res = cv2.matchTemplate(target_gray, resized_tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)

            if max_val > best_match["max_val"]:
                best_match = {"max_val": max_val, "max_loc": max_loc, "scale": scale}

        if best_match["max_val"] >= threshold:
            # 还原缩放后的中心点坐标
            scale = best_match["scale"]
            cx = best_match["max_loc"][0] + int(tw * scale / 2)
            cy = best_match["max_loc"][1] + int(th * scale / 2)
            SLog.i(ImageVision.TAG, f"图像匹配成功 (Scale: {scale}, Score: {best_match['max_val']:.2f})")
            return (cx, cy)

        return None