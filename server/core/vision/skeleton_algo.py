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
    def train_skeleton(image_names: List[str], threshold: int = 10) -> Tuple[Optional[np.ndarray], str]:
        """
        方法1: 骨架提取训练
        输入一堆图(服务端文件名)，识别重复地方并标记为骨架。
        
        :param image_names: 服务端图片文件名列表 (至少需要2张图来对比差异)
        :param threshold: 差异阈值 (0-255)，越小越敏感。默认10，容忍轻微的JPEG压缩噪点。
        :return: (mask_image_array, error_msg)
                 mask_image_array: 0=动态内容(黑色), 255=骨架(白色)
        """
        if not image_names or len(image_names) < 1:
            return None, "Need at least 1 image to generate skeleton."

        try:
            # 1. 读取第一张图作为基准 (Base)
            base_img = SkeletonAlgo._fetch_remote_image(image_names[0])
            if base_img is None:
                return None, f"Failed to fetch base image: {image_names[0]}"
            
            h, w = base_img.shape
            
            # 初始化骨架蒙版为全白 (255)，假设一开始全是骨架
            # 逻辑：随着对比图片的增加，把有差异的地方涂黑 (0)
            final_mask = np.ones((h, w), dtype=np.uint8) * 255

            # 如果只有一张图，无法提取骨架，只能认为全图都是骨架（或者返回空）
            # 这里为了兼容性，如果只有一张图，返回全白蒙版
            if len(image_names) == 1:
                return final_mask, ""

            # 2. 遍历剩余图片进行差分
            for i in range(1, len(image_names)):
                curr_name = image_names[i]
                curr_img = SkeletonAlgo._fetch_remote_image(curr_name)
                
                if curr_img is None:
                    SLog.w(TAG, f"Skipping unreadable image: {curr_name}")
                    continue
                
                # 尺寸检查：如果尺寸不一致，尝试缩放或跳过
                if curr_img.shape != base_img.shape:
                    curr_img = cv2.resize(curr_img, (w, h))

                # 计算绝对差值: |Base - Current|
                diff = cv2.absdiff(base_img, curr_img)
                
                # 二值化：差异 > threshold 的地方设为 255 (白色，表示变动区域)
                _, diff_thresh = cv2.threshold(diff, threshold, 255, cv2.THRESH_BINARY)
                
                # 反转：变动区域设为 0 (黑色)，不变区域设为 255 (白色)
                # 因为我们需要 Mask 中白色代表"骨架/保留区"
                static_part = cv2.bitwise_not(diff_thresh)
                
                # 累积求交集：只有在所有对比中都保持不变的区域，才是最终骨架
                final_mask = cv2.bitwise_and(final_mask, static_part)

            # 3. 形态学操作 (可选)：去除噪点
            # 腐蚀+膨胀 (开运算) 去除孤立的小白点
            kernel = np.ones((3, 3), np.uint8)
            final_mask = cv2.morphologyEx(final_mask, cv2.MORPH_OPEN, kernel)

            return final_mask, ""

        except Exception as e:
            SLog.e(TAG, f"Train skeleton failed: {e}")
            return None, str(e)

    @staticmethod
    def identify_page(target_image_path: str, candidates: List[Dict]) -> Tuple[Optional[Dict], float]:
        """
        方法2: 页面识别
        输入一张本地图，识别它高概率是什么页面(对比服务端的候选集)。
        
        :param target_image_path: 本地图片路径 (Driver 截图)
        :param candidates: 服务端候选页面列表，格式如下：
                           [
                             {
                               "id": 1, "label": "首页", 
                               "master_path": "master.jpg", # 服务端文件名
                               "mask_path": "mask.png"      # 服务端文件名 (可选)
                             }, ...
                           ]
        :return: (best_candidate, confidence_score)
                 confidence_score: 0.0 - 1.0 (1.0 表示完全匹配)
        """
        # 读取本地目标图
        target_img = cv2.imread(target_image_path, cv2.IMREAD_GRAYSCALE)
        if target_img is None:
            return None, 0.0

        # 优化：降采样以提高速度和鲁棒性 (Downsampling)
        # 骨架匹配不需要高分辨率，320px 宽度足够识别页面布局，且能容忍微小位移
        process_width = 320
        th, tw = target_img.shape
        best_score = -1.0
        best_candidate = None

        for cand in candidates:
            master_path = cand.get("master_path")
            mask_path = cand.get("mask_path")
            
            if not master_path:
                continue

            # 从服务端获取主图
            master_img = SkeletonAlgo._fetch_remote_image(master_path)
            if master_img is None: continue

            # 尺寸对齐
            # 将主图和目标图都缩放到 process_width
            scale = process_width / tw
            new_h = int(th * scale)
            
            small_target = cv2.resize(target_img, (process_width, new_h))
            small_master = cv2.resize(master_img, (process_width, new_h))

            # 从服务端获取蒙版 (如果有)
            mask = None
            if mask_path:
                mask = SkeletonAlgo._fetch_remote_image(mask_path)
                if mask is not None:
                    mask = cv2.resize(mask, (process_width, new_h))
            
            # 如果没有蒙版，默认全图匹配 (全白)
            if mask is None:
                mask = np.ones((new_h, process_width), dtype=np.uint8) * 255

            # --- 核心匹配逻辑 ---
            
            # 1. 计算差异图: |Target - Master|
            diff = cv2.absdiff(small_target, small_master)
            
            # 2. 应用蒙版: 只看骨架区域 (Mask为白色的区域)
            # 蒙版中黑色(0)区域的差异会被置为0，不计入误差
            masked_diff = cv2.bitwise_and(diff, diff, mask=mask)
            
            # 3. 计算相似度
            # 误差 = 骨架区域的总差异 / 骨架区域的总像素数
            # 注意：分母不能为0
            skeleton_pixel_count = cv2.countNonZero(mask)
            if skeleton_pixel_count == 0:
                score = 0.0 # 蒙版全黑，无法匹配
            else:
                total_diff = np.sum(masked_diff)
                # 平均每个像素的差异 (0-255)
                avg_diff = total_diff / skeleton_pixel_count
                # 归一化得分 (1.0 - 差异率)
                score = 1.0 - (avg_diff / 255.0)

            if score > best_score:
                best_score = score
                best_candidate = cand

        return best_candidate, best_score