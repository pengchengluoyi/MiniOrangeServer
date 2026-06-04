# !/usr/bin/env python
# -*-coding:utf-8 -*-

import cv2
import numpy as np
import os
import base64
from typing import List, Tuple, Optional, Dict
from script.log import SLog
from driver.agent.Common.ws import WS
from server.core.database import APP_DATA_DIR

TAG = "SkeletonAlgo"

class SkeletonAlgo:
    """
    骨架识别与页面匹配算法库
    """

    @staticmethod
    def _normalize_filename(path: str) -> str:
        if not path:
            return ""
        if "/static/" in path:
            path = path.split("/static/")[-1]
        return os.path.basename(path)

    @staticmethod
    def detect_system_bars(gray: np.ndarray) -> Dict[str, int]:
        """
        自动识别移动端系统栏（非应用逻辑区域）：
        - 顶部状态栏：时间、电量、信号等
        - 底部系统导航栏：返回 / Home / 多任务（Android）或 Home 指示条（iOS）
        """
        if gray is None or gray.size == 0:
            return {"top": 0, "bottom": 0}
        top = SkeletonAlgo._detect_status_bar_height(gray)
        bottom = SkeletonAlgo._detect_navigation_bar_height(gray)
        SLog.i(TAG, f"Auto system bars: top={top}px, bottom={bottom}px (screen_h={gray.shape[0]})")
        return {"top": top, "bottom": bottom}

    @staticmethod
    def _status_bar_bounds(h: int) -> Tuple[int, int, int]:
        """按屏幕高度给出状态栏区域的合理像素范围。"""
        return int(h * 0.045), int(h * 0.058), int(h * 0.11)

    @staticmethod
    def _system_nav_bounds(h: int) -> Tuple[int, int, int]:
        """底部系统导航（非应用 Tab）的合理高度：仅 Home 指示条或 Android 三键。"""
        return max(16, int(h * 0.008)), max(20, int(h * 0.014)), max(28, int(h * 0.035))

    @staticmethod
    def _has_app_bottom_tab_bar(gray: np.ndarray) -> bool:
        """
        检测应用底 Tab（首页 / 我的 等）：底部区域有 3+ 个横向均匀分布的峰，
        且横跨大部分屏宽 —— 这与 Android 三键（集中在中间）不同。
        """
        h, w = gray.shape[:2]
        strip_h = int(h * 0.14)
        strip = gray[h - strip_h:, :]
        if strip.size == 0:
            return False

        edges = cv2.Canny(strip, 40, 110)
        col_energy = np.sum(edges, axis=0).astype(np.float32)
        smoothed = np.convolve(col_energy, np.ones(max(7, w // 80)) / max(7, w // 80), mode="same")
        threshold = float(np.max(smoothed)) * 0.28
        peaks = []
        min_dist = w * 0.12
        for i in range(1, len(smoothed) - 1):
            if smoothed[i] >= threshold and smoothed[i] >= smoothed[i - 1] and smoothed[i] >= smoothed[i + 1]:
                if not peaks or i - peaks[-1] >= min_dist:
                    peaks.append(i)

        if len(peaks) < 3:
            return False

        span = peaks[-1] - peaks[0]
        return span > w * 0.45 and len(peaks) >= 3

    @staticmethod
    def _detect_navigation_bar_height(gray: np.ndarray) -> int:
        """
        仅涂黑系统导航（Android 三键 / iOS Home 条），不涂黑应用底 Tab。
        """
        h, w = gray.shape[:2]
        min_bottom, default_bottom, max_bottom = SkeletonAlgo._system_nav_bounds(h)
        has_app_tabs = SkeletonAlgo._has_app_bottom_tab_bar(gray)

        android_h = SkeletonAlgo._detect_android_three_button_nav(gray)
        if android_h > 0 and not has_app_tabs:
            return int(np.clip(android_h, min_bottom, max_bottom))

        # 有应用底 Tab 时，只排除最底部 Home 指示条（约 1.2%–1.8% 屏高）
        if has_app_tabs:
            gesture_h = max(18, int(h * 0.012))
            return int(np.clip(gesture_h, min_bottom, max(24, int(h * 0.02))))

        # 无应用底 Tab：在屏幕最底部 2.5% 内找 Home 指示条
        strip_h = max(24, int(h * 0.025))
        strip = gray[h - strip_h:, :]
        edges = cv2.Canny(strip, 30, 90)
        row_energy = np.sum(edges, axis=1).astype(np.float32)
        if row_energy.size == 0:
            return default_bottom

        search_from = int(strip_h * 0.55)
        tail = row_energy[search_from:]
        if tail.size == 0:
            return default_bottom

        peak_local = int(np.argmax(tail))
        peak_global = search_from + peak_local
        if row_energy[peak_global] < w * 0.06:
            return default_bottom

        nav_h = strip_h - peak_global + max(6, int(h * 0.004))
        return int(np.clip(nav_h, min_bottom, max_bottom))

    @staticmethod
    def _detect_status_bar_height(gray: np.ndarray) -> int:
        """
        检测顶部系统状态栏高度（时间 / 信号 / 电量等）。
        特征：图标集中在左右两侧，中间相对空；下方应用 Tab 栏横跨全宽。
        """
        h, w = gray.shape[:2]
        min_top, default_top, max_top = SkeletonAlgo._status_bar_bounds(h)
        max_scan = min(max_top + 20, int(h * 0.14))

        edges = cv2.Canny(gray[: max_scan + 4, :], 35, 110)
        corner_ratios = []
        center_ratios = []
        row_stds = []

        for y in range(max_scan):
            row_stds.append(float(np.std(gray[y, :])))
            if y >= edges.shape[0]:
                corner_ratios.append(0.0)
                center_ratios.append(0.0)
                continue
            e = edges[y, :]
            left_e = float(np.sum(e[: int(w * 0.24)]))
            right_e = float(np.sum(e[int(w * 0.76):]))
            center_e = float(np.sum(e[int(w * 0.28): int(w * 0.72)]))
            total_e = left_e + right_e + center_e + 1.0
            corner_ratios.append((left_e + right_e) / total_e)
            center_ratios.append(center_e / total_e)

        corner_ratios = np.array(corner_ratios, dtype=np.float32)
        center_ratios = np.array(center_ratios, dtype=np.float32)
        row_stds = np.array(row_stds, dtype=np.float32)

        probe_end = min(max_top, len(center_ratios) - 1)
        base_center = float(np.percentile(center_ratios[: max(6, probe_end // 4)], 70))
        base_std = float(np.median(row_stds[: max(6, probe_end // 4)]))

        cut = default_top
        for y in range(int(h * 0.02), probe_end):
            app_header_row = (
                center_ratios[y] > base_center + 0.06
                and row_stds[y] > base_std * 1.12
            )
            wide_content_row = row_stds[y] > base_std * 1.35
            if app_header_row or wide_content_row:
                cut = y
                break

        for y in range(cut - 1, int(h * 0.02) - 1, -1):
            if corner_ratios[y] > 0.42 or row_stds[y] < base_std * 1.05:
                cut = y + 1
            else:
                break

        return int(np.clip(max(cut, min_top), min_top, max_top))

    @staticmethod
    def _detect_android_three_button_nav(gray: np.ndarray) -> int:
        """仅在屏幕最底部窄条内检测 Android 三键（返回 / Home / 多任务）。"""
        h, w = gray.shape[:2]
        strip_h = max(36, int(h * 0.038))
        strip = gray[h - strip_h:, :]
        if strip.size == 0:
            return 0

        edges = cv2.Canny(strip, 35, 100)
        col_energy = np.sum(edges, axis=0).astype(np.float32)
        if col_energy.size == 0:
            return 0

        center = col_energy[int(w * 0.18): int(w * 0.82)]
        smoothed = np.convolve(center, np.ones(7) / 7, mode="same")
        threshold = float(np.max(smoothed)) * 0.4
        peaks = []
        for i in range(1, len(smoothed) - 1):
            if smoothed[i] >= threshold and smoothed[i] >= smoothed[i - 1] and smoothed[i] >= smoothed[i + 1]:
                if not peaks or i - peaks[-1] > len(smoothed) * 0.12:
                    peaks.append(i)

        if len(peaks) != 3:
            return 0

        row_energy = np.sum(edges[int(strip_h * 0.45):, :], axis=1).astype(np.float32)
        if row_energy.size == 0 or float(np.max(row_energy)) < w * 0.08:
            return 0

        active_rows = np.where(row_energy > w * 0.08)[0]
        if active_rows.size == 0:
            return max(28, int(h * 0.028))
        nav_top = int(active_rows[0]) + int(strip_h * 0.45)
        return strip_h - nav_top + max(6, int(h * 0.004))

    @staticmethod
    def apply_ignored_areas(mask: np.ndarray, ignored_areas: Optional[Dict] = None) -> np.ndarray:
        """将系统栏区域涂黑，不参与骨架训练与匹配。"""
        if mask is None or not ignored_areas:
            return mask
        top = int(ignored_areas.get("top") or 0)
        bottom = int(ignored_areas.get("bottom") or 0)
        h, _ = mask.shape[:2]
        top = max(0, min(top, h))
        bottom = max(0, min(bottom, h - top))
        if top > 0:
            mask[:top, :] = 0
        if bottom > 0:
            mask[h - bottom:, :] = 0
        return mask

    @staticmethod
    def _zero_system_bars(img: np.ndarray, system_bars: Optional[Dict] = None) -> np.ndarray:
        """将系统栏区域像素清零，使其不参与差分/匹配。"""
        if img is None or not system_bars:
            return img
        out = img.copy()
        top = int(system_bars.get("top") or 0)
        bottom = int(system_bars.get("bottom") or 0)
        h = out.shape[0]
        top = max(0, min(top, h))
        bottom = max(0, min(bottom, h - top))
        if top > 0:
            out[:top, :] = 0
        if bottom > 0:
            out[h - bottom:, :] = 0
        return out

    @staticmethod
    def _fetch_remote_image(filename: str) -> Optional[np.ndarray]:
        """
        从服务端获取图片流并转为 OpenCV 灰度图
        """
        if not filename:
            return None
        
        # 处理 URL 路径，只取文件名
        # 假设服务端存储的是文件名，或者 /static/filename
        if "/static/" in filename:
            filename = filename.split("/static/")[-1]
        else:
            filename = os.path.basename(filename)

        # 1. 尝试读取本地文件 (服务端逻辑)
        upload_dir = os.path.join(APP_DATA_DIR, "uploads")
        local_path = os.path.join(upload_dir, filename)
        if os.path.exists(local_path):
            return cv2.imread(local_path, cv2.IMREAD_GRAYSCALE)

        # 2. 尝试通过 WS 获取 (客户端逻辑)
        try:
            # 通过 WS 获取文件 (返回 base64)
            resp = WS.fetch_get_file(filename)
            if resp and resp.get("code") == 200:
                data = resp.get("data", {})
                b64_content = data.get("content")
                if b64_content:
                    img_data = base64.b64decode(b64_content)
                    np_arr = np.frombuffer(img_data, np.uint8)
                    return cv2.imdecode(np_arr, cv2.IMREAD_GRAYSCALE)
            
            SLog.w(TAG, f"Fetch remote image failed: {filename}, resp: {resp}")
            return None
        except Exception as e:
            SLog.e(TAG, f"Error fetching remote image {filename}: {e}")
            return None

    @staticmethod
    def train_skeleton(
        image_names: List[str],
        threshold: int = 10,
        ignored_areas: Optional[Dict] = None,
    ) -> Tuple[Optional[np.ndarray], str, Optional[Dict]]:
        """
        方法1: 骨架提取训练
        输入一堆图(服务端文件名)，识别重复地方并标记为骨架。
        
        :param image_names: 服务端图片文件名列表 (至少需要2张图来对比差异)
        :param threshold: 差异阈值 (0-255)，越小越敏感。默认10，容忍轻微的JPEG压缩噪点。
        :return: (mask_image_array, error_msg)
                 mask_image_array: 0=动态内容(黑色), 255=骨架(白色)
        """
        if not image_names or len(image_names) < 1:
            return None, "Need at least 1 image to generate skeleton.", None

        try:
            # 1. 读取第一张图作为基准 (Base)
            base_img = SkeletonAlgo._fetch_remote_image(image_names[0])
            if base_img is None:
                return None, f"Failed to fetch base image: {image_names[0]}", None

            system_bars = ignored_areas or SkeletonAlgo.detect_system_bars(base_img)
            
            h, w = base_img.shape
            
            # 系统栏先置黑，且差分时不参与「静态骨架」判定
            final_mask = np.ones((h, w), dtype=np.uint8) * 255
            final_mask = SkeletonAlgo.apply_ignored_areas(final_mask, system_bars)

            if len(image_names) == 1:
                return final_mask, "", system_bars

            # 2. 遍历剩余图片进行差分
            for i in range(1, len(image_names)):
                curr_name = image_names[i]
                curr_img = SkeletonAlgo._fetch_remote_image(curr_name)
                
                if curr_img is None:
                    SLog.w(TAG, f"Skipping unreadable image: {curr_name}")
                    continue
                
                if curr_img.shape != base_img.shape:
                    curr_img = cv2.resize(curr_img, (w, h))

                diff = cv2.absdiff(base_img, curr_img)
                # 系统栏像素不参与差分（时间/电量等即使不变也不应成为骨架）
                diff = SkeletonAlgo._zero_system_bars(diff, system_bars)
                
                _, diff_thresh = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
                static_part = cv2.bitwise_not(diff_thresh)
                static_part = SkeletonAlgo.apply_ignored_areas(static_part, system_bars)
                
                final_mask = cv2.bitwise_and(final_mask, static_part)

            # 3. 形态学操作 (可选)：去除噪点
            # 腐蚀+膨胀 (开运算) 去除孤立的小白点
            kernel = np.ones((3, 3), np.uint8)
            final_mask = cv2.morphologyEx(final_mask, cv2.MORPH_OPEN, kernel)
            final_mask = SkeletonAlgo.apply_ignored_areas(final_mask, system_bars)

            return final_mask, "", system_bars

        except Exception as e:
            SLog.e(TAG, f"Train skeleton failed: {e}")
            return None, str(e), None

    @staticmethod
    def _to_gray(target_img) -> Optional[np.ndarray]:
        if target_img is None:
            return None
        if isinstance(target_img, str):
            if not os.path.exists(target_img):
                return None
            return cv2.imread(target_img, cv2.IMREAD_GRAYSCALE)
        arr = np.asarray(target_img)
        if arr.ndim == 3:
            if arr.shape[2] == 3:
                return cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
            return cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        return arr

    @staticmethod
    def score_page_match(
        target_img,
        master_path: str,
        mask_path: Optional[str] = None,
        ignored_areas: Optional[Dict] = None,
        process_width: int = 320,
    ) -> float:
        target_gray = SkeletonAlgo._to_gray(target_img)
        if target_gray is None or not master_path:
            return 0.0

        master_img = SkeletonAlgo._fetch_remote_image(master_path)
        if master_img is None:
            return 0.0

        th, tw = target_gray.shape[:2]
        scale = process_width / max(tw, 1)
        new_h = max(1, int(th * scale))
        small_target = cv2.resize(target_gray, (process_width, new_h))
        small_master = cv2.resize(master_img, (process_width, new_h))

        bars = ignored_areas or {}
        if bars:
            scale_bars = {
                "top": max(1, int((bars.get("top") or 0) * new_h / th)),
                "bottom": max(1, int((bars.get("bottom") or 0) * new_h / th)),
            }
            small_target = SkeletonAlgo._zero_system_bars(small_target, scale_bars)
            small_master = SkeletonAlgo._zero_system_bars(small_master, scale_bars)
            ignored_areas = scale_bars

        mask = None
        if mask_path:
            mask = SkeletonAlgo._fetch_remote_image(mask_path)
        if mask is None:
            mask = np.ones((new_h, process_width), dtype=np.uint8) * 255
        else:
            mask = cv2.resize(mask, (process_width, new_h))
        mask = SkeletonAlgo.apply_ignored_areas(mask, ignored_areas)

        diff = cv2.absdiff(small_target, small_master)
        masked_diff = cv2.bitwise_and(diff, diff, mask=mask)
        skeleton_pixel_count = cv2.countNonZero(mask)
        if skeleton_pixel_count == 0:
            return 0.0
        avg_diff = np.sum(masked_diff) / skeleton_pixel_count
        return max(0.0, 1.0 - (avg_diff / 255.0))

    @staticmethod
    def skeleton_config_paths(skeleton_config: Optional[Dict]) -> Tuple[Optional[str], Optional[str], Dict]:
        sk = skeleton_config or {}
        images = sk.get("images") or []
        master_path = sk.get("master_path") or (images[0] if images else None)
        mask_path = sk.get("mask_path") or sk.get("mask_url") or sk.get("filename")
        ignored_areas = sk.get("system_bars") or sk.get("ignored_areas") or {}
        if master_path:
            master_path = SkeletonAlgo._normalize_filename(master_path)
        if mask_path:
            mask_path = SkeletonAlgo._normalize_filename(mask_path)
        return master_path, mask_path, ignored_areas

    @staticmethod
    def score_node_match(target_img, node_data: Dict) -> float:
        sk = node_data.get("skeleton_config") or {}
        master_path, mask_path, ignored_areas = SkeletonAlgo.skeleton_config_paths(sk)
        if not master_path or not mask_path:
            return 0.0
        return SkeletonAlgo.score_page_match(
            target_img, master_path, mask_path, ignored_areas=ignored_areas
        )

    @staticmethod
    def identify_page(target_image_path: str, candidates: List[Dict]) -> Tuple[Optional[Dict], float]:
        """
        方法2: 页面识别
        输入一张本地图，识别它高概率是什么页面(对比服务端的候选集)。
        """
        return SkeletonAlgo.identify_page_from_image(target_image_path, candidates)

    @staticmethod
    def load_image_from_payload(data: Dict) -> Tuple[Optional[np.ndarray], str]:
        """从 WS/HTTP 载荷加载灰度图：content(base64) 或 image_name/filename。"""
        content = data.get("content")
        if content:
            try:
                if "," in str(content):
                    content = str(content).split(",", 1)[1]
                raw = base64.b64decode(content)
                arr = np.frombuffer(raw, np.uint8)
                gray = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
                if gray is None:
                    return None, "Invalid image content"
                return gray, ""
            except Exception as e:
                return None, f"Decode image failed: {e}"

        name = data.get("image_name") or data.get("filename") or data.get("path")
        if name:
            gray = SkeletonAlgo._fetch_remote_image(name)
            if gray is None:
                return None, f"Image not found: {name}"
            return gray, ""
        return None, "Missing image content or image_name"

    @staticmethod
    def rank_page_candidates(
        target_img,
        candidates: List[Dict],
        top_k: Optional[int] = None,
    ) -> List[Dict]:
        """对候选页面按骨架相似度排序，返回带 score 的列表。"""
        ranked: List[Dict] = []
        for cand in candidates:
            master_path = cand.get("master_path")
            if not master_path:
                continue
            score = SkeletonAlgo.score_page_match(
                target_img,
                master_path,
                cand.get("mask_path"),
                ignored_areas=cand.get("ignored_areas"),
            )
            item = dict(cand)
            item["score"] = round(max(0.0, float(score)), 4)
            ranked.append(item)
        ranked.sort(key=lambda c: c["score"], reverse=True)
        if top_k is not None and top_k > 0:
            return ranked[:top_k]
        return ranked

    @staticmethod
    def identify_page_from_image(
        target_img,
        candidates: List[Dict],
        min_score: float = 0.0,
    ) -> Tuple[Optional[Dict], float]:
        ranked = SkeletonAlgo.rank_page_candidates(target_img, candidates)
        if not ranked:
            return None, 0.0
        best = ranked[0]
        score = best.get("score", 0.0)
        if min_score > 0 and score < min_score:
            return None, score
        return best, score