# !/usr/bin/env python
# -*-coding:utf-8 -*-
from script.log import SLog, current_flow_id
from script.sleep import mSleep

from driver.agent.Action.executer import Executer
from driver.agent.Memory import memory_manager
from script.mTask import report
from driver.agent.Common.ws import WS
from driver.agent.Planning.device_doctor import DeviceDoctor
from driver.agent.Planning.Page import Page
from driver.agent.Perception.Vision.mImageMatching import ImageVision
from driver.agent.Perception.Vision.feedback import Feedback

TAG = "Orchestrator"


class Orchestrator:

    def __init__(self):
        self.jobMarket = []
        self.feedback = Feedback()

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
                    found_pos = None
                    # 增加重试次数：应用启动可能较慢，给予更多等待时间 (10次 * 1秒 = 10秒)
                    for i in range(10):
                        img = employee.tool.vision()
                        found_pos = ImageVision.get_template_match(interaction_id, img, threshold=0.8)
                        if isinstance(found_pos, bool):
                            sop = DeviceDoctor.unlock_sop()
                            for step_info in sop:
                                trigger = getattr(employee.tool, step_info["tool"])
                                trigger(**step_info["args"])
                        elif found_pos:
                            break

                        SLog.d(TAG, f"Visual match failed for {interaction_id}, retrying... ({i+1}/10)")
                        mSleep(1.0)
                    memory_manager.short_term.set_global("position", found_pos)


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
                    self_check_result = employee.self_check()
                    if self_check_result:
                        employee.completed()
                        
                        # --- 🚀 智能启动策略 ---
                        if "window" in current_node.id and current_node.data.get("operation") == "start":
                            Page.wait_for_app_launch(self.feedback, current_node)
                        else:
                            # --- 👁️ 常规视觉闭环 ---
                            Page.verify_step(self.feedback, current_node)

            report[current_node.id] = employee.taskResult.to_dict()
            mSleep(0.3)

        memory_manager.sync_timeline()
