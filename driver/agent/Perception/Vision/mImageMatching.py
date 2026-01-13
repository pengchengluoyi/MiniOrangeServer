# ability/engine/vision/mImageMatching.py

import cv2
import numpy as np
import os
import builtins
import base64
from PIL import Image

from script.log import SLog
from driver.agent.Memory import memory_manager


class ImageVision:
    TAG = "ImageVision"

    @staticmethod
    def get_template_match(interaction_id, current_screenshot_np, threshold=0.35): # threshold 较低后续需要优化这里的算法
        # 1. 通过 WS 代理从服务端获取数据
        query_func = getattr(builtins, "SERVER_QUERY", None)
        if not query_func:
            SLog.e(ImageVision.TAG, "SERVER_QUERY not available")
            return None

        comp_data = query_func("get_component", {"uid": interaction_id})
        
        if not comp_data or not comp_data.get("screenshot_b64"):
            SLog.e(ImageVision.TAG, f"未找到热区或原始截图数据: {interaction_id}")
            return None

        # 2. 解码 Base64 图片
        try:
            img_bytes = base64.b64decode(comp_data["screenshot_b64"])
            orig_img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        except Exception as e:
            SLog.e(ImageVision.TAG, f"Base64 decode failed: {e}")
            return None
        
        if orig_img is None:
            return None

        # 3. 实时裁剪模板
        x, y, w, h = int(comp_data["x"]), int(comp_data["y"]), int(comp_data["width"]), int(comp_data["height"])
        template = orig_img[y:y + h, x:x + w]

        # 保存模板图片到时间线 (类型为 screen)
        try:
            # OpenCV (BGR) -> PIL (RGB)
            template_rgb = cv2.cvtColor(template, cv2.COLOR_BGR2RGB)
            memory_manager.short_term.set_timeline_scope("screen", Image.fromarray(template_rgb))
        except Exception as e:
            SLog.w(ImageVision.TAG, f"Failed to save template to timeline: {e}")

        # 4. 执行多尺度匹配
        return ImageVision._do_robust_match(current_screenshot_np, template, threshold)

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