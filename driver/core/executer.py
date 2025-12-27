# !/usr/bin/env python
# -*-coding:utf-8 -*-
from ability.manager import Manager
from driver.common.task_result import TaskResult

TAG = "Executer"


class Executer:
    def __init__(self):
        self.taskResult = TaskResult()
        self.center = Manager()
        self.task = None

    def online(self):
        ...

    def accept_order(self, order_info):
        self.taskResult.accept_order(order_info)
        self.task = self.center.register_router(order_info)
        self.center.online(order_info)
        return True if self.task else None

    def dispatch(self):
        self.taskResult.dispatched()
        self.task.execute()

    def self_check(self):
        self.taskResult.self_check()

    def completed(self):
        self.taskResult.success()
        self.task = None

    def offline(self):
        self.center.offline()






