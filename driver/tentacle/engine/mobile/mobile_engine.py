# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
移动端引擎基类：组件只调用 engine.*，不直接碰 driver，抹平 iOS / Android 差异。
"""
from __future__ import annotations

from typing import Optional

from driver.tentacle.core.engine import BaseEngine


class MobileEngine(BaseEngine):
    _test_subject: Optional[str] = None

    def start(self):
        if self.driver is None:
            self.init_driver(test_subject=self._test_subject)

    def screen_on(self):
        """点亮/唤醒屏幕（平台实现可选）。"""
        return None

    def swipe_norm(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        duration: float = 0.5,
    ):
        """归一化坐标滑动 (0~1)，与历史 uiautomator2 组件参数一致。"""
        raise NotImplementedError

    def swipe_ext(self, direction: str, scale: float = 0.8):
        """方向滑动：left / right / up / down。"""
        direction = (direction or "right").lower()
        scale = float(scale or 0.8)
        margin = (1.0 - scale) / 2.0
        if direction == "left":
            self.swipe_norm(1.0 - margin, 0.5, margin, 0.5, duration=0.3)
        elif direction == "right":
            self.swipe_norm(margin, 0.5, 1.0 - margin, 0.5, duration=0.3)
        elif direction == "up":
            self.swipe_norm(0.5, 1.0 - margin, 0.5, margin, duration=0.3)
        elif direction == "down":
            self.swipe_norm(0.5, margin, 0.5, 1.0 - margin, duration=0.3)
        else:
            raise ValueError(f"Unknown swipe direction: {direction}")

    def press_key(self, event: str):
        raise NotImplementedError

    def screen_size(self) -> tuple[int, int]:
        """屏幕宽高（像素）。"""
        raise NotImplementedError

    def position_to_pixels(self, x: float, y: float, *, normalized: bool = False) -> tuple[int, int]:
        if normalized or (0 <= x <= 1 and 0 <= y <= 1):
            w, h = self.screen_size()
            return int(w * x), int(h * y)
        return int(x), int(y)
