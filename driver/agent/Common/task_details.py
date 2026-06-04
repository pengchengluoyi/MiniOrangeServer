# !/usr/bin/env python
# -*-coding:utf-8 -*-
from driver.tentacle.common import platform
from driver.agent.Common.task_result import TaskResult


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
        self.id = case_info["id"] if case_info.get("id") else "node_id_null"
        self.nodeType = case_info.get("nodeType") if case_info.get("nodeCode") else None
        self.nodeCode = case_info["nodeCode"] if case_info.get("nodeCode") else None
        self.platform = case_info["platform"] if case_info.get("platform") else platform.COMMON
        self.displayName = case_info["displayName"] if case_info.get("displayName") else "displayName"
        self.lastCodes = case_info["lastCodes"] if case_info.get("lastCodes") else []
        self.nextCodes = case_info["nextCodes"] if case_info.get("nextCodes") else []
        self.data = case_info["data"] if case_info.get("data") else {}
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
