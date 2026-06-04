# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""截图相似度（用于同页多图校验 / 是否跳转到新页）。"""
from __future__ import annotations

from typing import Optional, Union

import cv2
import numpy as np

_PROCESS_WIDTH = 320


def _to_gray(img: Union[np.ndarray, str]) -> Optional[np.ndarray]:
    if img is None:
        return None
    if isinstance(img, str):
        gray = cv2.imread(img, cv2.IMREAD_GRAYSCALE)
        return gray
    arr = np.asarray(img)
    if arr.ndim == 3:
        return cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
    return arr


def frame_similarity(
    img_a: Union[np.ndarray, str],
    img_b: Union[np.ndarray, str],
    process_width: int = _PROCESS_WIDTH,
) -> float:
    """
    返回 [0, 1]，1 表示几乎相同。
    与 skeleton score 类似：缩放到固定宽度后比较灰度差。
    """
    a = _to_gray(img_a)
    b = _to_gray(img_b)
    if a is None or b is None:
        return 0.0

    ha, wa = a.shape[:2]
    hb, wb = b.shape[:2]
    scale_a = process_width / max(wa, 1)
    scale_b = process_width / max(wb, 1)
    new_ha = max(1, int(ha * scale_a))
    new_hb = max(1, int(hb * scale_b))
    new_h = max(new_ha, new_hb)
    sa = cv2.resize(a, (process_width, new_ha))
    sb = cv2.resize(b, (process_width, new_hb))
    if new_ha < new_h:
        pad = np.zeros((new_h - new_ha, process_width), dtype=np.uint8)
        sa = np.vstack([sa, pad])
    if new_hb < new_h:
        pad = np.zeros((new_h - new_hb, process_width), dtype=np.uint8)
        sb = np.vstack([sb, pad])

    diff = cv2.absdiff(sa, sb)
    avg = float(np.mean(diff))
    return max(0.0, 1.0 - (avg / 255.0))


def is_valid_same_page_shot(
    candidate: Union[np.ndarray, str],
    prior_shots: list,
    *,
    max_similarity: float = 0.85,
    min_similarity: float = 0.50,
) -> tuple[bool, str, float]:
    """
    校验候选图是否可作为「当前页」的下一张采集图。
    - 与任一已采集图相似度 > max_similarity → 无效（需继续操作再截）
    - 与任一已采集图相似度 < min_similarity → 视为离开当前页
    返回 (ok, reason, max_sim_to_prior)
    """
    if not prior_shots:
        return True, "first_shot", 0.0

    sims = [frame_similarity(candidate, p) for p in prior_shots]
    max_sim = max(sims)
    min_sim = min(sims)

    if max_sim > max_similarity:
        return False, "too_similar", max_sim
    if min_sim < min_similarity:
        return False, "left_page", min_sim
    return True, "ok", max_sim
