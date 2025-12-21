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
          "timestamp": "2023-10-25 14:30:45"   //这个没有做
        }
    """
    def __init__(self):
        self._success = False
        self._code = None
        self._message = None
        self._data = None
        self.start_timestamp = int(time.time() * 1000)
        self.end_timestamp = None


    def success(self):
        self.end_timestamp = int(time.time() * 1000)
        self._success = True

    def fail(self):
        self.end_timestamp = int(time.time() * 1000)
        self._success = False

    def is_success(self):
        return self._success

    def result_data(self, data):
        self._data = data