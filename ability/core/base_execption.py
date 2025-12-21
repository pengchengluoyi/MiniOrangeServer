# !/usr/bin/env python
# -*-coding:utf-8 -*-

from typing import Optional


class MException(Exception):
    """ exception."""

    def __init__(
        self, code: Optional[int] = None, msg: Optional[str] = None, screen: Optional[str] = None, stacktrace: Optional[Sequence[str]] = None
    ) -> None:
        super().__init__()
        self.msg = msg
        self.code = code
        self.screen = screen
        self.stacktrace = stacktrace

    def __str__(self) -> str:
        # exception_msg = f"Message: {self.msg}\n"
        # if self.screen:
        #     exception_msg += "Screenshot: available via screen\n"
        # if self.stacktrace:
        #     stacktrace = "\n".join(self.stacktrace)
        #     exception_msg += f"Stacktrace:\n{stacktrace}"
        # return exception_msg
        return "error code: {0}, error details: {1}".format(str(self.code), self.msg)