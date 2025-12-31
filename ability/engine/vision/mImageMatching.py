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
    def get_template_match(interaction_id, current_screenshot_np, threshold=0.35): # threshold 较低后续需要优化这里的算法
        db = SessionLocal()
        try:
            # 1. 获取组件及其所属 Node
            comp = db.query(AppComponent).filter(AppComponent.uid == interaction_id).first()
            # 修正点：AppNode 里的字段名是 screenshot
            if not comp or not comp.node or not comp.node.screenshot:
                SLog.e(ImageVision.TAG, f"未找到热区或原始截图数据: {interaction_id}")
                return None

            # 2. 读取原始大图 (使用 comp.node.screenshot)
            orig_path = get_final_path(comp.node.screenshot)
            if not os.path.exists(orig_path):
                SLog.e(ImageVision.TAG, f"原始截图文件不存在: {orig_path}")
                return None

            orig_img = cv2.imdecode(np.fromfile(orig_path, dtype=np.uint8), cv2.IMREAD_COLOR)
            if orig_img is None:
                return None

            # 3. 实时裁剪模板
            # 转换为 int 像素值
            x, y, w, h = int(comp.x), int(comp.y), int(comp.width), int(comp.height)
            template = orig_img[y:y + h, x:x + w]

            # 4. 执行多尺度匹配以提高鲁棒性
            return ImageVision._do_robust_match(current_screenshot_np, template, threshold)
        finally:
            db.close()

    @staticmethod
    def _do_robust_match(target_img, template, threshold):
        """多尺度匹配：应对桌面窗口缩放"""
        if not isinstance(target_img, np.ndarray):
            target_img = np.array(target_img)
            # 如果是 RGB (PIL 默认)，转换为 BGR (OpenCV 默认)
            if len(target_img.shape) == 3:
                target_img = cv2.cvtColor(target_img, cv2.COLOR_RGB2BGR)

            # 同样确保模板也是 Numpy 数组
        if not isinstance(template, np.ndarray):
            template = np.array(template)

        target_gray = cv2.cvtColor(target_img, cv2.COLOR_BGR2GRAY)
        template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)

        best_match = {"max_val": -1, "max_loc": None, "scale": 1.0}
        # 尝试 0.9, 1.0, 1.1 三个比例
        for scale in [0.9, 1.0, 1.1]:
            resized_tpl = cv2.resize(template_gray, None, fx=scale, fy=scale) if scale != 1.0 else template_gray
            if resized_tpl.shape[0] > target_gray.shape[0] or resized_tpl.shape[1] > target_gray.shape[1]:
                continue

            res = cv2.matchTemplate(target_gray, resized_tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best_match["max_val"]:
                best_match = {"max_val": max_val, "max_loc": max_loc, "scale": scale}

        if best_match["max_val"] >= threshold:
            tw, th = template_gray.shape[::-1]
            scale = best_match["scale"]
            cx = best_match["max_loc"][0] + int(tw * scale / 2)
            cy = best_match["max_loc"][1] + int(th * scale / 2)
            return (cx, cy)
        # mImageMatching.py
        if max_val > best_match["max_val"]:
            best_match = {"max_val": max_val, "max_loc": max_loc, "scale": scale}
        # 在循环结束后增加
        if best_match["max_val"] < threshold:
            SLog.w(ImageVision.TAG, f"图像匹配失败，最高相似度仅为: {best_match['max_val']:.2f}")  # 这样你能看到差多少分
        return None