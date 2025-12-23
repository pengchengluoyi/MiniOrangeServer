# !/usr/bin/env python
# -*-coding:utf-8 -*-

import os
import sys
import time
import cv2
from rapidocr_onnxruntime import RapidOCR
import numpy as np

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter

TAG = "OCR"

def add_suffix_before_ext(filepath, suffix):
    """
    在文件扩展名之前添加后缀
    """
    base, ext = os.path.splitext(filepath)
    return base + suffix + ext


@BaseRouter.route('public/ocr')
class FastOCR(Template):
    """
        This component will
    """
    META = {
        "inputs": [
            {
                "name": "path",
                "type": "str",
                "desc": "文件路径",
                "defaultValue": "screenshot",
                "placeholder": "screenshot"
            },
        ],
        "defaultData": {
            "path": "",
        },
        "outputVars": [
            {"key": "ocr_result", "type": "json", "desc": "图片识别结果"},
            {"key": "ocr_image_path", "type": "str", "desc": "图片识别路径"}
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        # OCR 组件通常不需要获取自动化 Engine (self.get_engine())，除非需要截图
        # 这里直接处理文件路径
        image_path = self.get_param_value("path")

        try:
            results = self.analyze(image_path)
            self.memory.set(self.info, "ocr_result", results)

            if results:
                # 修复变量名错误: img_path -> image_path
                write_path = self.visualize(image_path, results)
                self.memory.set(self.info, "ocr_image_path", write_path)

        except Exception as e:
            SLog.e(TAG, f"程序运行出错: {e}")
            import traceback
            traceback.print_exc()

    def analyze(self, image_path):
        # 1. 读取图片
        img = cv2.imread(image_path)
        if img is None:
            SLog.e(TAG, "❌ 无法读取图片")
            return []

        # 2. 图片放大处理 (提高精度)
        h, w = img.shape[:2]
        if w < 2000:
            SLog.d(TAG, f">> 图片较小 ({w}x{h})，正在放大 2 倍以提高精度...")
            img = cv2.resize(img, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)

        # 3. 运行识别
        # 修复: self.engine 是自动化驱动，不是 OCR 引擎。需要实例化 RapidOCR
        ocr_engine = RapidOCR()
        result, elapse = ocr_engine(img)

        output_data = []

        # --- 修复点：增加对 None 的判断 ---
        total_time = 0.0
        if elapse is None:
            total_time = 0.0
        elif isinstance(elapse, (list, tuple)):
            total_time = sum(elapse)
        else:
            try:
                total_time = float(elapse)
            except:
                total_time = 0.0
        # -------------------------------

        SLog.d(TAG, f">> 识别耗时: {total_time:.4f}s")

        # 检查是否有结果
        if not result:
            SLog.d(TAG, f">> 未检测到文字")
            return []

        # 4. 解析结果
        for item in result:
            coords = item[0]
            text = item[1]
            score = item[2]

            # 还原坐标 (因为之前放大了2倍，现在要除以2)
            scale_factor = 0.5 if w < 2000 else 1.0

            real_coords = []
            for p in coords:
                real_coords.append([int(p[0] * scale_factor), int(p[1] * scale_factor)])

            # 计算中心点
            xs = [p[0] for p in real_coords]
            ys = [p[1] for p in real_coords]
            cx = int(sum(xs) / len(xs))
            cy = int(sum(ys) / len(ys))

            data = {
                "text": text,
                "confidence": round(float(score), 2),
                "coordinates": {
                    "center": (cx, cy),
                    "box": real_coords
                }
            }
            output_data.append(data)

        return output_data

    def visualize(self, image_path, results):
        img = cv2.imread(image_path)
        if img is None: return

        for item in results:
            box = item['coordinates']['box']
            box_np = np.array(box).astype(np.int32).reshape((-1, 1, 2))
            cv2.polylines(img, [box_np], True, (0, 0, 255), 2)
        write_path = add_suffix_before_ext(image_path, "_ocr_result")
        cv2.imwrite(write_path, img)
        return write_path
