#!/usr/bin/env python
# -*-coding:utf-8 -*-
import time
from typing import Any, Dict, Optional
from script.log import SLog, current_node_id

# 使用常量提升性能
_MS_MULTIPLIER = 1000


class Event:
    """
    """

    # 使用__slots__减少内存占用，提升属性访问速度
    __slots__ = ('_type', '_input_vars', '_output_vars', '_timestamp')

    def __init__(self):
        # 私有属性
        self._type = ""
        self._input_vars = {}
        self._output_vars = {}
        self._timestamp = ""
        self._add_timestamp()

    # =================== 时间戳相关方法 ===================
    @staticmethod
    def _current_ms_timestamp() -> int:
        """获取当前毫秒时间戳 - 使用局部变量提升性能"""
        return int(time.time() * _MS_MULTIPLIER)

    def _add_timestamp(self) -> None:
        """添加时间戳记录"""
        self._timestamp = self._current_ms_timestamp()

    def set_type(self, message: Optional[str] = None) -> None:
        self._type = message

    def set_input_vars(self, message: Optional[Dict] = None) -> None:
        self._input_vars.update(message)

    def set_output_vars(self, message: Optional[Dict] = None) -> None:
        self._output_vars.update(message)

    # =================== 输出方法 ===================
    def to_dict(self) -> Dict[str, Any]:
        return {
            self._timestamp: {
                "type": self._type,
                "inputVars": self._input_vars,
                "outputVars": self._output_vars,
                "timestamp": self._timestamp
            }
        }