# !/usr/bin/env python
# -*-coding:utf-8 -*-
from script.log import SLog, current_flow_id
from script.sleep import mSleep

from driver.agent.Action.executer import Executer
from driver.agent.Memory import memory_manager
from script.mTask import report
from driver.agent.Common.ws import WS
from driver.agent.Planning.planner import Planner
from driver.agent.Perception.Vision.feedback import Feedback

TAG = "Orchestrator"


class Orchestrator:

    def __init__(self):
        self.jobMarket = []
        self.feedback = Feedback()
        self.planner = Planner(self.feedback)

    def hiring(self):
        if not self.jobMarket:
            self.jobMarket.append(Executer())
        return self.jobMarket[0]

    def completed(self):
        for employee in self.jobMarket:
            employee.offline()

    def run(self):
        memory_manager.initialize()
        memory_manager.checklist.create(WS.get_workflow_detail(current_flow_id.get())["nodes"])

        while True:
            employee = self.hiring()
            current_node = memory_manager.checklist.peek()
            if not current_node:
                self.completed()
                break

            accept_result = employee.accept_order(current_node)
            memory_manager.short_term.set_global("platform", current_node.platform)

            interaction_id = current_node.data.get("interaction_id", "")
            if interaction_id:
                if "gesture" in current_node.id:
                    # 委托给 Planner 进行视觉定位和异常处理
                    self.planner.locate_visual_target(interaction_id)

            if accept_result:
                dispatch_result = True
                if "gesture" not in current_node.id:
                    dispatch_result = employee.dispatch()
                else:
                    mtype = current_node.data.get("sub_type", "click")
                    pos = memory_manager.short_term.get_global("position")
                    if pos:
                        # 捕获执行结果，如果点击失败，dispatch_result 应为 False
                        dispatch_result = employee.tool.gesture(mtype, pos)
                    else:
                        SLog.e(TAG, f"❌ Visual target not found for {interaction_id}, skipping gesture.")
                        dispatch_result = False

                if dispatch_result:
                    self_check_result = employee.self_check(dispatch_result, self.planner, current_node)
                    if self_check_result:
                        employee.completed()

            report[current_node.id] = employee.taskResult.to_dict()
            mSleep(0.3)

        memory_manager.sync_timeline()
