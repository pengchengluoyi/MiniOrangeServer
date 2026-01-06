# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.constPath.error_code import ErrorCode

class MException(Exception):
    def __init__(self, error: ErrorCode):
        self.error_code = error.code
        self.error_msg = error.message
        super().__init__(self.error_msg)

    def __str__(self):
        return f"[Error {self.error_code}]: {self.error_msg}"