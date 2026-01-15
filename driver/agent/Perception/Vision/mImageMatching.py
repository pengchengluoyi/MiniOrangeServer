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
    def get_template_match(interaction_id, current_screenshot_np, threshold=0.85):
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

        # 4. 智能搜索策略
        return ImageVision._smart_search(current_screenshot_np, template, (x, y, w, h), threshold)

    @staticmethod
    def _smart_search(target_img, template, expected_rect, threshold):
        """
        智能搜索策略：
        1. 全局搜索
        2. 局部焦点搜索 (基于预期坐标，扩大范围)
        3. 滑动窗口搜索 (Sliding Window) - 应对局部高亮/复杂背景，替代固定网格
        4. 特征点匹配 (SIFT/ORB) - 作为兜底
        """
        # 格式标准化
        if not isinstance(target_img, np.ndarray):
            target_img = np.array(target_img)
            if len(target_img.shape) == 3:
                target_img = cv2.cvtColor(target_img, cv2.COLOR_RGB2BGR)

        if not isinstance(template, np.ndarray):
            template = np.array(template)

        # --- 策略 1: 全局搜索 ---
        global_res = ImageVision._do_robust_match(target_img, template, threshold)
        if global_res:
            return global_res
        elif isinstance(global_res, bool):
            return False

        # --- 策略 2: 焦点搜索 (Focus Search) ---
        # 在预期位置周围扩大范围搜索，排除全图干扰
        h_img, w_img = target_img.shape[:2]
        ex, ey, ew, eh = expected_rect

        margin = max(ew, eh, 200)  # 扩大范围，增加容错
        x1 = max(0, ex - margin)
        y1 = max(0, ey - margin)
        x2 = min(w_img, ex + ew + margin)
        y2 = min(h_img, ey + eh + margin)

        # 只有当 ROI 显著小于全图时才尝试，否则没意义
        if (x2 - x1) * (y2 - y1) < (w_img * h_img * 0.7):
            SLog.d(ImageVision.TAG, f"🔍 启用焦点搜索 ROI: ({x1},{y1}) -> ({x2},{y2})")
            roi_img = target_img[y1:y2, x1:x2]
            roi_res = ImageVision._do_robust_match(roi_img, template, threshold)
            if roi_res:
                return (roi_res[0] + x1, roi_res[1] + y1)

        # --- 策略 3: 滑动窗口搜索 (Sliding Window Search) ---
        # 替代原有的固定网格，使用滑动窗口覆盖全图，解决边界截断问题
        SLog.d(ImageVision.TAG, "⚠️ 全局及焦点搜索失败，启用滑动窗口搜索...")

        h_tpl, w_tpl = template.shape[:2]

        # 窗口大小：取屏幕的一半，或者模板的1.5倍，确保足够大能包含模板且有上下文
        win_w = max(int(w_img / 2), int(w_tpl * 1.5))
        win_h = max(int(h_img / 2), int(h_tpl * 1.5))

        # 步长：窗口的一半 (50% 重叠)
        stride_x = max(1, int(win_w * 0.5))
        stride_y = max(1, int(win_h * 0.5))

        for y in range(0, h_img, stride_y):
            for x in range(0, w_img, stride_x):
                # 计算当前窗口坐标 (处理边界：如果超出则向左/上回退，保持窗口大小)
                x_end = min(x + win_w, w_img)
                y_end = min(y + win_h, h_img)
                x_start = max(0, x_end - win_w)
                y_start = max(0, y_end - win_h)

                sub_img = target_img[y_start:y_end, x_start:x_end]
                # 滑动窗口时，稍微降低一点阈值，收集最佳候选
                res = ImageVision._do_robust_match(sub_img, template, threshold, return_best=False)

                if res:
                    SLog.i(ImageVision.TAG, f"✅ 滑动窗口搜索命中: Rect({x_start},{y_start},{x_end},{y_end})")
                    return (res[0] + x_start, res[1] + y_start)

                if x_end == w_img: break
            if y_end == h_img: break

        return None

    @staticmethod
    def _do_robust_match(target_img, template, threshold, return_best=False):
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

        best_match = {"max_val": -1, "max_loc": None, "scale": 1.0, "shape": template_gray.shape}

        # 扩大缩放搜索范围，应对不同分辨率和缩放比例
        # 优化: 移除 0.5/0.75/1.5 等极端缩放，减少误匹配 (False Positives)
        scales = [0.8, 0.9, 1.0, 1.1, 1.2]

        for scale in scales:
            if scale == 1.0:
                resized_tpl = template_gray
            else:
                resized_tpl = cv2.resize(template_gray, None, fx=scale, fy=scale)

            if resized_tpl.shape[0] > target_gray.shape[0] or resized_tpl.shape[1] > target_gray.shape[1]:
                continue

            res = cv2.matchTemplate(target_gray, resized_tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val > best_match["max_val"]:
                best_match = {"max_val": max_val, "max_loc": max_loc, "scale": scale, "shape": resized_tpl.shape}

        if best_match["max_val"] <= 0:
            return False

        if best_match["max_val"] >= threshold:
            # 使用匹配到的实际模板尺寸计算中心点
            h, w = best_match["shape"][:2]
            cx = best_match["max_loc"][0] + int(w / 2)
            cy = best_match["max_loc"][1] + int(h / 2)
            SLog.i(ImageVision.TAG, f"Match found: val={best_match['max_val']:.2f} (Threshold: {threshold}), scale={best_match['scale']}")
            return (cx, cy)

        SLog.w(ImageVision.TAG, f"图像匹配失败，最高相似度仅为: {best_match['max_val']:.2f} (阈值: {threshold})")

        if return_best:
            return best_match
        return None

    @staticmethod
    def _feature_match(target_img, template, min_match_count=6):
        """
        使用 SIFT/ORB 进行特征点匹配
        """
        try:
            # 初始化 ORB 检测器
            orb = cv2.ORB_create(nfeatures=1000)

            # 寻找关键点和描述符
            kp1, des1 = orb.detectAndCompute(template, None)
            kp2, des2 = orb.detectAndCompute(target_img, None)

            if des1 is None or des2 is None or len(kp1) < min_match_count or len(kp2) < min_match_count:
                SLog.d(ImageVision.TAG, f"特征点不足: kp1={len(kp1) if kp1 else 0}, kp2={len(kp2) if kp2 else 0}")
                return None

            # 创建 BFMatcher 对象
            bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)

            # 匹配描述符
            matches = bf.match(des1, des2)

            # 根据距离排序
            matches = sorted(matches, key=lambda x: x.distance)

            # 如果匹配点足够多
            if len(matches) > min_match_count:
                # 获取匹配点的坐标
                src_pts = np.float32([kp1[m.queryIdx].pt for m in matches]).reshape(-1, 1, 2)
                dst_pts = np.float32([kp2[m.trainIdx].pt for m in matches]).reshape(-1, 1, 2)

                # 计算单应性矩阵，排除异常点
                M, mask = cv2.findHomography(src_pts, dst_pts, cv2.RANSAC, 5.0)
                if M is not None:
                    h, w = template.shape[:2]
                    pts = np.float32([[0, 0], [0, h - 1], [w - 1, h - 1], [w - 1, 0]]).reshape(-1, 1, 2)
                    dst = cv2.perspectiveTransform(pts, M)

                    # 计算中心点
                    M_moments = cv2.moments(dst)
                    if M_moments['m00'] != 0:
                        cx = int(M_moments['m10'] / M_moments['m00'])
                        cy = int(M_moments['m01'] / M_moments['m00'])
                        SLog.i(ImageVision.TAG, f"✅ 特征点匹配成功: matches={len(matches)}")
                        return (cx, cy)
        except Exception as e:
            SLog.w(ImageVision.TAG, f"特征匹配异常: {e}")

        return None

    @staticmethod
    def get_golden_frame(count=3):
        """
        时序去噪：连续采集多帧，通过中值滤波生成“黄金帧”，去除动态干扰（如弹幕、滚动条）
        """
        from driver.agent.Action.tools import Tool
        from script.sleep import mSleep

        frames = []
        # SLog.d(ImageVision.TAG, f"Collecting {count} frames for denoising...")
        for i in range(count):
            img = Tool.vision()
            if img is not None:
                if not isinstance(img, np.ndarray):
                    img = np.array(img)
                    # Ensure BGR
                    if len(img.shape) == 3 and img.shape[2] == 3:
                        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
                frames.append(img)
            mSleep(0.2)

        if not frames:
            return None

        try:
            # Stack frames and calculate median along time axis
            stack = np.stack(frames, axis=0)
            golden = np.median(stack, axis=0).astype(np.uint8)
            return golden
        except Exception as e:
            SLog.e(ImageVision.TAG, f"Denoising failed: {e}")
            return frames[0]