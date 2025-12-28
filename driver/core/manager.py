# !/usr/bin/env python
# -*-coding:utf-8 -*-
from script.log import SLog
from script.sleep import mSleep

from driver.core.executer import Executer
from driver.core.memory.checklist import Checklist
from script.mTask import report
from ability.component.router import BaseRouter

TAG = "Manager"


class  Manager:

    def __init__(self, case_data=None):
        self.case_data = case_data
        self.checklist = Checklist()
        self.jobMarket = []

    def hiring(self):
        if not self.jobMarket:
            self.jobMarket.append(Executer())
        return self.jobMarket[0]

    def completed(self):
        for employee in self.jobMarket:
            employee.offline()

    def run(self):
        if self.case_data:
            self.checklist.create(self.case_data["nodes"])
            while True:
                employee = self.hiring()
                current_node = self.checklist.peek()
                if not current_node:
                    self.completed()
                    break
                accept_result = employee.accept_order(current_node)
                if accept_result:
                    dispatch_result = employee.dispatch()
                    if dispatch_result:
                        self_check_result = employee.self_check()
                        if self_check_result:
                            employee.completed()
                report[current_node.id] = employee.taskResult.to_dict()
                mSleep(0.3)
        else:
            SLog.i(TAG, "run end")

    def execute_interface(self, data: dict):
        uri = data.get("nodeCode")
        if not uri:
            return {"code": 400, "msg": "nodeCode is required"}
        
        return BaseRouter.handle_request(uri, data)
