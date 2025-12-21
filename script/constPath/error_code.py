# !/usr/bin/env python
# -*-coding:utf-8 -*-
from enum import Enum

# 1000 - 1999 engine error
# 2000 - 2999 component error

class ErrorCode(Enum):
    ENGINE_ERROR_NOT_FOUND_CHROME                   =   (1000, "Google browser is not installed on this computer!!!")

    COMPONENT_ERROR_FOUND_ELEMENT_TIMEOUT           =   (3000, "Find element timeout Failed!")

    def __init__(self, code, message):
        self.code = code
        self.message = message

    @classmethod
    def get_message(cls, error_code):
        for error in cls:
            if error.code == error_code:
                return error.message
        return "Unknown error occurred."

    @classmethod
    def get_error(cls, error_code):
        for error in cls:
            if error.code == error_code:
                return error
        return None


class CustomException(Exception):
    def __init__(self, error: ErrorCode):
        self.error_code = error.code
        self.error_msg = error.message
        super().__init__(self.error_msg)

    def __str__(self):
        return f"[Error {self.error_code}]: {self.error_msg}"


if __name__ == '__main__':
    try:
        # 模拟发生错误
        raise CustomException(ErrorCode.COMPONENT_ERROR_FOUND_ELEMENT_TIMEOUT)
    except CustomException as e:

        print(e)