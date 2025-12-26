# !/usr/bin/env python
# -*-coding:utf-8 -*-

import os
import sys
import time
from pathlib import Path

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter
from server.core.database import APP_DATA_DIR

TAG = "OCR"

# 🛡️ 容错处理：防止因缺少 OCR 依赖库导致整个模块加载失败 (Module not found)
try:
    import cv2
    from rapidocr_onnxruntime import RapidOCR
    import numpy as np
except ImportError as e:
    SLog.e(TAG, f"OCR 依赖库缺失: {e}")
    # 打印当前 Python 解释器路径，方便排查是否运行在错误的环境中
    SLog.e(TAG, f"Current Python Executable: {sys.executable}")
    cv2 = None
    RapidOCR = None
    np = None

# 2. 拼接 uploads 目录路径 (修正：上传的文件在 uploads 而不是 data)
# 保持与 main.py 中的 UPLOAD_DIR 一致
UPLOAD_DIR = os.path.join(APP_DATA_DIR, "uploads")

def add_suffix_before_ext(filepath, suffix):
    """
    在文件扩展名之前添加后缀
    """
    base, ext = os.path.splitext(filepath)
    return base + suffix + ext

def get_final_path(input_str):
    base_path = UPLOAD_DIR
    input_path = Path(input_str)

    # 检查输入是否为绝对路径
    if input_path.is_absolute():
        return str(input_path)
    else:
        # 如果是文件名或相对路径，则进行拼接
        return str(base_path / input_path)


@BaseRouter.route('public/ocr')
class FastOCR(Template):
    """
        This component will
    """
    META = {
        "inputs": [
            {
                "name": "path",
                "type": "file",
                "desc": "文件路径",
                "defaultValue": "screenshot",
                "placeholder": "screenshot"
            },
        ],
        "defaultData": {
            "path": False,
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
        if cv2 is None or RapidOCR is None or np is None:
            error_msg = f"OCR 依赖库缺失，请在 {sys.executable} 中安装依赖: pip install opencv-python rapidocr-onnxruntime"
            SLog.e(TAG, error_msg)
            self.result.fail()
            return self.result

        pre_image_path = self.get_param_value("path")
        image_path = get_final_path(pre_image_path)

        try:
            results = analyze(image_path)
            self.memory.set(self.info, "ocr_result", results)

            if results:
                # 修复变量名错误: img_path -> image_path
                write_path = visualize(image_path, results)
                self.memory.set(self.info, "ocr_image_path", write_path)

        except Exception as e:
            SLog.e(TAG, f"程序运行出错: {e}")
            import traceback
            traceback.print_exc()

def analyze(image_path, img=None):
    if not img:
        # 0. 检查文件是否存在
        if not os.path.exists(image_path):
            SLog.e(TAG, f"❌ 文件不存在: {image_path}")
            return []

        # 1. 读取图片 (使用 imdecode 兼容中文路径和特殊字符)
        # img = cv2.imread(image_path)
        # np.fromfile 读取二进制数据，cv2.imdecode 解码，比直接 imread 更健壮
        img = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            SLog.e(TAG, "❌ 无法读取图片")
            return []

    # 兼容 PIL Image 对象 (从内存传入时，如 gesture.py 的调用)
    if not isinstance(img, np.ndarray):
        img = np.array(img)
        # PIL 是 RGB，OpenCV 默认是 BGR，为了保持一致性进行转换
        if len(img.shape) == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

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

def visualize(image_path, results):
    # img = cv2.imread(image_path)
    img = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None: return

    for item in results:
        box = item['coordinates']['box']
        box_np = np.array(box).astype(np.int32).reshape((-1, 1, 2))
        cv2.polylines(img, [box_np], True, (0, 0, 255), 2)
    write_path = add_suffix_before_ext(image_path, "_ocr_result")

    # cv2.imwrite(write_path, img)
    # 使用 imencode 保存，兼容中文路径
    ext = os.path.splitext(write_path)[1]
    cv2.imencode(ext, img)[1].tofile(write_path)
    return write_path
