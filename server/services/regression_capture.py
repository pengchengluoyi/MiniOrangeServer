# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""回归执行过程截图落盘。"""
from __future__ import annotations

import time
from typing import Any, Optional

from script.log import SLog

TAG = "RegressionCapture"


def shot_is_blank(shot: Any, *, white_threshold: float = 244.0, std_threshold: float = 14.0) -> bool:
    """检测全白/全黑过渡帧（consent 关闭后常见白屏闪一下）。"""
    if shot is None:
        return True
    try:
        import numpy as np

        arr = np.asarray(shot)
        if arr.size == 0:
            return True
        if arr.ndim == 3:
            gray = arr.mean(axis=2)
        else:
            gray = arr.astype(float)
        mean = float(gray.mean())
        std = float(gray.std())
        if mean >= white_threshold and std <= std_threshold:
            return True
        if mean <= 18.0 and std <= std_threshold:
            return True
        return False
    except Exception:
        return False


def capture_engine_screenshot(
    engine: Any,
    *,
    run_id: str = "",
    tag: str = "step",
    settle_ms: int = 0,
    max_attempts: int = 4,
    retry_interval_ms: int = 350,
) -> str:
    """从已 bootstrap 的 engine 截图；等待 UI 稳定并跳过空白过渡帧。"""
    if not engine or not hasattr(engine, "screenshot"):
        return ""
    try:
        from server.services.crawl_persistence import save_screenshot_file

        if settle_ms > 0:
            time.sleep(settle_ms / 1000.0)

        prefix = f"reg_{run_id[:8]}_{tag}" if run_id else f"reg_{tag}"
        shot = None
        attempts = max(1, int(max_attempts))
        for attempt in range(attempts):
            shot = engine.screenshot()
            if shot is not None and not shot_is_blank(shot):
                break
            if attempt < attempts - 1:
                time.sleep(max(0.15, retry_interval_ms / 1000.0))
        if shot is None:
            return ""
        return save_screenshot_file(shot, prefix=prefix)
    except Exception as e:
        SLog.w(TAG, f"capture_engine_screenshot failed: {e}")
        return ""


def capture_device_screenshot(
    sn: str,
    platform: str = "android",
    *,
    run_id: str = "",
    tag: str = "step",
    settle_ms: int = 0,
    max_attempts: int = 4,
) -> str:
    """截取当前设备画面，返回 /static/... 路径。"""
    if not sn:
        return ""
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

        builtins.TARGET_DEVICE_SN = str(sn)
        engine, _ = bootstrap_mobile_engine(sn, platform)
        return capture_engine_screenshot(
            engine,
            run_id=run_id,
            tag=tag,
            settle_ms=settle_ms,
            max_attempts=max_attempts,
        )
    except Exception as e:
        SLog.w(TAG, f"capture failed: {e}")
        return ""
