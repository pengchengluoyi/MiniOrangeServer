# !/usr/bin/env python
# -*-coding:utf-8 -*-

import time


def mSleep(seconds):
    """阻塞等待（兼容 WebSocket / 工作流等同步执行上下文）。"""
    time.sleep(float(seconds))
