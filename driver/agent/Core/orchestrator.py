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

    def _load_workflow_nodes(self) -> dict:
        """优先内联节点（HTTP /run 调试），否则经 WS 从 DB 拉取。"""
        import builtins

        inline = getattr(builtins, "WORKFLOW_INLINE_NODES", None)
        if isinstance(inline, dict) and inline:
            return inline

        flow_id = current_flow_id.get()
        detail = WS.get_workflow_detail(flow_id) if flow_id else None
        if not isinstance(detail, dict):
            SLog.e(TAG, f"无法加载工作流 nodes, flow_id={flow_id}")
            return {}

        if "nodes" in detail:
            return detail["nodes"]
        inner = detail.get("data")
        if isinstance(inner, dict) and "nodes" in inner:
            return inner["nodes"]
        SLog.e(TAG, f"工作流详情缺少 nodes: {detail.keys()}")
        return {}

    def run(self):
        import builtins

        memory_manager.initialize()
        run_sn = getattr(builtins, "TARGET_DEVICE_SN", None)
        if run_sn:
            memory_manager.short_term.set_global("run_device_sn", str(run_sn))
            SLog.i(TAG, f"执行设备 sn/udid={run_sn}")

        memory_manager.checklist.create(self._load_workflow_nodes())

        while True:
            employee = self.hiring()
            current_node = memory_manager.checklist.peek()
            if not current_node:
                self.completed()
                break

            accept_result = employee.accept_order(current_node)
            memory_manager.short_term.set_global("platform", current_node.platform)

            node_data = current_node.data or {}
            interaction_id = node_data.get("interaction_id", "")
            has_explicit_position = node_data.get("position") is not None
            # 仅「热区定位、无坐标」走 Planner；编排里写了 position 则走 public/gesture 组件
            use_planner_gesture = bool(interaction_id) and not has_explicit_position
            if use_planner_gesture and interaction_id:
                self.planner.locate_visual_target(interaction_id)

            if accept_result:
                dispatch_result = True
                if use_planner_gesture:
                    mtype = node_data.get("sub_type", "click")
                    pos = memory_manager.short_term.get_global("position")
                    if pos:
                        dispatch_result = employee.tool.gesture(mtype, pos)
                    else:
                        SLog.e(
                            TAG,
                            f"Visual target not found for {interaction_id}, skipping gesture.",
                        )
                        dispatch_result = False
                else:
                    dispatch_result = employee.dispatch()

                if dispatch_result:
                    self_check_result = employee.self_check(dispatch_result, self.planner, current_node)
                    if self_check_result:
                        employee.completed()

            report[current_node.id] = employee.taskResult.to_dict()
            mSleep(0.3)

        memory_manager.sync_timeline()
