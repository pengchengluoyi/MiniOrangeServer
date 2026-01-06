# !/usr/bin/env python
# -*-coding:utf-8 -*-
import time
from script.constPath.component_code import *


class StepResult:
    """
        {
          "success": true,
          "code": 200,
          "message": "成功",
          "data": {...},
          "timestamp": "2023-10-25 14:30:45"
        }
    """

    def __init__(self):
        self._success = False
        self._code = 200  # 建议给个默认初始值
        self._message = ""
        self._data = None
        self.start_timestamp = int(time.time() * 1000)
        self.end_timestamp = None

    def success(self, message="成功", code=200):
        self.end_timestamp = int(time.time() * 1000)
        self._success = True
        self._message = message
        self._code = code

    def fail(self, message="失败", code=500):
        self.end_timestamp = int(time.time() * 1000)
        self._success = False
        self._message = message
        self._code = code

    def is_success(self):
        return self._success

    def result_data(self, data):
        self._data = data

    def to_dict(self):
        """将对象转换为字典，以便于 FastAPI 直接返回 JSON"""
        current_end = self.end_timestamp if self.end_timestamp else int(time.time() * 1000)

        # 格式化时间为：2023-10-25 14:30:45
        formatted_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(current_end / 1000))

        return {
            "success": self._success,
            "code": self._code,
            "message": self._message,
            "data": self._data,
            "timestamp": formatted_time,
            "duration_ms": current_end - self.start_timestamp  # 额外增加一个耗时统计，对自动化工具很有用
        }