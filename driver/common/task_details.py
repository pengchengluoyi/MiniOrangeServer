# !/usr/bin/env python
# -*-coding:utf-8 -*-
from ability.common import platform as PLATFORM
from driver.common.task_result import TaskResult


class TaskDetails:
    """
        {
          "id": str,            //唯一ID
          "nodeType": cfs/normal,
          "nodeCode": cfs-if/cfs-for/normal....,
          "displayName": str,
          "lastCodes": [],
          "nextCodes": [],
          "data": {...},
        }
    """
    def __init__(self, case_info: dict):
        self.id = case_info["id"]
        self.nodeType = case_info["nodeType"]
        self.nodeCode = case_info["nodeCode"]
        self.platform = case_info["platform"] if case_info.get("platform") else PLATFORM.COMMON
        self.displayName = case_info["displayName"]
        self.lastCodes = case_info["lastCodes"]
        self.nextCodes = case_info["nextCodes"]
        self.data = case_info["data"]
        self.router = None
        self.result = None

    def set_result(self, result: TaskResult):
        self.result = result

    def get_result(self):
        return self.result

    def create_route(self, var_value: TaskResult):
        # 检查属性是否存在
        setattr(self, self.id, var_value)

    def get_route(self):
        return getattr(self, self.id, None)
