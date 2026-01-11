# ability/engine/vision/mOcr.py

import os
import sys

from script.log import SLog
from script.mPath import add_suffix_before_ext

TAG = "OCR"

# 🛡️ 容错处理：防止因缺少 OCR 依赖库导致整个模块加载失败
try:
    import cv2
    from rapidocr_onnxruntime import RapidOCR
    import numpy as np
except ImportError as e:
    SLog.e(TAG, f"OCR 依赖库缺失: {e}")
    SLog.e(TAG, f"Current Python Executable: {sys.executable}")
    cv2 = None
    RapidOCR = None
    np = None


# --- 🚀 OCR 引擎单例管理 ---
_OCR_ENGINE_INSTANCE = None


def get_ocr_engine():
    global _OCR_ENGINE_INSTANCE
    if _OCR_ENGINE_INSTANCE is None:
        if RapidOCR is None: return None

        try:
            # 尝试正常初始化
            _OCR_ENGINE_INSTANCE = RapidOCR(det_db_unclip_ratio=1.3)
        except (KeyError, Exception):
            _OCR_ENGINE_INSTANCE = RapidOCR()
    return _OCR_ENGINE_INSTANCE



def analyze(image_path, img=None):
    if not img:
        if not os.path.exists(image_path):
            SLog.e(TAG, f"❌ 文件不存在: {image_path}")
            return []
        img = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            SLog.e(TAG, "❌ 无法读取图片")
            return []

    if not isinstance(img, np.ndarray):
        img = np.array(img)
        if len(img.shape) == 3:
            img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

    # 2. 图片放大处理
    h, w = img.shape[:2]
    scale_factor = 1.0
    if w < 2000:
        scale_factor = 0.5
        SLog.d(TAG, f">> 图片较小，正在放大识别...")
        img = cv2.resize(img, (0, 0), fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)

    # 3. 运行识别 (使用单例)
    ocr_engine = get_ocr_engine()
    if not ocr_engine:
        return []

    result, elapse = ocr_engine(img)

    total_time = 0.0
    if elapse is not None:
        total_time = sum(elapse) if isinstance(elapse, (list, tuple)) else float(elapse)
    SLog.d(TAG, f">> 识别耗时: {total_time:.4f}s")

    if not result:
        return []

    output_data = []
    for item in result:
        coords = item[0]
        text = item[1]
        score = item[2]

        # 还原坐标
        real_coords = [[int(p[0] * scale_factor), int(p[1] * scale_factor)] for p in coords]

        # 计算中心点
        xs = [p[0] for p in real_coords]
        ys = [p[1] for p in real_coords]
        cx = int(sum(xs) / len(xs))
        cy = int(sum(ys) / len(ys))

        output_data.append({
            "text": text,
            "confidence": round(float(score), 2),
            "coordinates": {
                "center": (cx, cy),
                "box": real_coords
            }
        })

    return output_data


def visualize(image_path, results):
    img = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None: return
    for item in results:
        box = item['coordinates']['box']
        box_np = np.array(box).astype(np.int32).reshape((-1, 1, 2))
        cv2.polylines(img, [box_np], True, (0, 0, 255), 2)
    write_path = add_suffix_before_ext(image_path, "_ocr_result")
    ext = os.path.splitext(write_path)[1]
    cv2.imencode(ext, img)[1].tofile(write_path)
    return write_path