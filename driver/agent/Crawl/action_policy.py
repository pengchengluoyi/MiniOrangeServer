# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""跑图交互策略：80% 点击 / 20% 滑动；返回键每 50 次手势最多下发 1 次。"""
from __future__ import annotations

SWIPE_EVERY_N = 5  # 每 5 次交互 1 次滑动 → 20%
BACK_AFTER_GESTURES = 50


class CrawlActionPolicy:
    def __init__(self) -> None:
        self.gesture_count = 0
        self.back_pending = False

    def should_swipe(self) -> bool:
        """当前这一次用滑动（约 20%，每 5 次里第 5 次滑）。"""
        return (self.gesture_count + 1) % SWIPE_EVERY_N == 0

    def record_gesture(self) -> None:
        self.gesture_count += 1

    def schedule_back(self) -> None:
        self.back_pending = True

    def maybe_flush_back(self, press_back_fn) -> bool:
        """累计满 50 次点击/滑动后，若曾请求过返回则执行一次。"""
        if not self.back_pending or self.gesture_count < BACK_AFTER_GESTURES:
            return False
        press_back_fn()
        self.back_pending = False
        self.gesture_count = 0
        return True

    def flush_back_end(self, press_back_fn) -> bool:
        """跑图结束：仍待返回则补一次（不受 50 限制）。"""
        if not self.back_pending:
            return False
        press_back_fn()
        self.back_pending = False
        return True
