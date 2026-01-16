# !/usr/bin/env python
# -*-coding:utf-8 -*-
from driver.agent.Planning.Page import Page
from driver.agent.Planning.device_doctor import DeviceDoctor
from driver.agent.Action.tools import Tool
from driver.agent.Perception.Vision.mImageMatching import ImageVision
from driver.agent.Memory import memory_manager
from script.log import SLog
from script.sleep import mSleep

TAG = "Planner"

class Planner:
    """
    Planning 层的统一决策者。
    负责协调 Perception (视觉) 和 Action (工具)，制定执行策略和自愈方案。
    """
    def __init__(self, feedback):
        self.feedback = feedback

    def locate_visual_target(self, interaction_id):
        """
        决策：如何找到目标元素
        包含策略：
        1. 视觉识别
        2. 失败重试
        3. 异常状态恢复 (如锁屏)
        """
        found_pos = None
        # 增加重试次数：应用启动可能较慢，给予更多等待时间 (10次 * 1秒 = 10秒)
        for i in range(10):
            # 直接调用 Tool 工具类，无需通过 employee
            img = Tool.vision()
            found_pos = ImageVision.get_template_match(interaction_id, img, threshold=0.8)
            
            # 处理异常状态 (如锁屏检测返回 bool)
            if isinstance(found_pos, bool): 
                SLog.w(TAG, "Visual anomaly detected (e.g. lock screen), executing unlock SOP.")
                sop = DeviceDoctor.unlock_sop()
                for step_info in sop:
                    trigger = getattr(Tool, step_info["tool"])
                    trigger(**step_info["args"])
            elif found_pos:
                memory_manager.short_term.set_global("position", found_pos)
                return found_pos

            SLog.d(TAG, f"Visual match failed for {interaction_id}, retrying... ({i+1}/10)")
            mSleep(1.0)
        
        return None

    def verify_and_heal(self, current_node):
        """
        决策：任务执行后的核验与自愈
        """
        # 路由到具体的 Planning 策略
        if "window" in current_node.id and current_node.data.get("operation") == "start":
            return Page.wait_for_app_launch(self.feedback, current_node)
        else:
            return Page.verify_step(self.feedback, current_node)