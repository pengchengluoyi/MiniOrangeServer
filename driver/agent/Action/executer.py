# !/usr/bin/env python
# -*-coding:utf-8 -*-
from driver.tentacle.manager import Manager
from driver.agent.Common.task_result import TaskResult
from driver.agent.Action.tools import Tool

TAG = "Executer"


class Executer:
    def __init__(self):
        self.taskResult = TaskResult()
        self.center = Manager()
        self.task = None
        self.tool = Tool()

    def online(self):
        ...

    def accept_order(self, order_info):
        self.taskResult.accept_order(order_info)
        self.task = self.center.register_router(order_info)
        self.center.online(order_info)
        return True if self.task else None

    def dispatch(self):
        self.taskResult.dispatched()
        return self.task.execute()

    def self_check(self, result, planner, current_node):
        self.taskResult.self_check()
        
        # 增加对执行结果的校验逻辑
        if isinstance(result, dict):
            # 检查返回码，非 200 视为失败 (假设 200 为成功状态码)
            if result.get("code", 200) != 200:
                return False
            # 检查是否存在显式的错误信息
            if "error" in result:
                return False
                
        # 委托给 Planner 进行结果核验与自愈 (页面跳转、弹窗处理)
        return planner.verify_and_heal(current_node)

    def completed(self):
        self.taskResult.success()
        self.task = None
        return True

    def offline(self):
        self.center.offline()
