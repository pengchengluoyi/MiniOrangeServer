# !/usr/bin/env python
# -*-coding:utf-8 -*-
from script.log import SLog, current_flow_id
from script.sleep import mSleep

from driver.agent.Action.executer import Executer
from driver.agent.Memory import memory_manager
from script.mTask import report
from driver.agent.Common.ws import WS
from driver.agent.Perception.Vision.mPositionCalculation import PositionManager

TAG = "Orchestrator"


class Orchestrator:

    def __init__(self):
        self.jobMarket = []

    def hiring(self):
        if not self.jobMarket:
            self.jobMarket.append(Executer())
        return self.jobMarket[0]

    def completed(self):
        for employee in self.jobMarket:
            employee.offline()

    def run(self):
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
                    for i in range(len(interaction_id)):
                        img = employee.tool.vision()
                        position = PositionManager.find_visual_target(interaction_id, None, None, img)
                        memory_manager.short_term.set_global("position", position)
                        break


            if accept_result:
                dispatch_result = True
                if "gesture" not in current_node.id:
                    dispatch_result = employee.dispatch()
                else:
                    mtype = current_node.data.get("sub_type", "click")
                    employee.tool.gesture(mtype, memory_manager.short_term.get_global("position"))
                if dispatch_result:
                    self_check_result = employee.self_check()
                    if self_check_result:
                        employee.completed()
            report[current_node.id] = employee.taskResult.to_dict()
            mSleep(0.3)

        memory_manager.sync_timeline()
