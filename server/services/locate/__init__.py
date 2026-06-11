# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""统一 UI 定位：多通道打分仲裁 + 页面类型配置 + 空间约束 + 通用无字图标行。"""
from server.services.locate.resolver import resolve_locate_target
from server.services.locate.types import LocateResult

__all__ = ["resolve_locate_target", "LocateResult"]
