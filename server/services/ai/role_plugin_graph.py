# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""兼容入口：IM 绑定改由 layer_stack 编排。"""
from __future__ import annotations

from typing import Any, Dict

from server.services.ai.layer_stack import im_roles_for_plugin, resolve_im

__all__ = ["im_roles_for_plugin", "resolve_im"]
