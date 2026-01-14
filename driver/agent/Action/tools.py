# !/usr/bin/env python
# -*-coding:utf-8 -*-
from driver.tentacle.manager import Manager
from driver.agent.Common.event import Event
from driver.agent.Common.task_details import TaskDetails
from driver.agent.Memory import memory_manager

tentacle = Manager()

class Tool:
    @staticmethod
    def vision():
        order_info = {
                "id": "sys_vision_observe",
                "nodeCode": "tools/vision",
                "nodeType": 200,
                "displayName": "虚拟视觉",
                "platform": memory_manager.short_term.get_global("platform"),
                "data": {},
                "lastCodes": [],
                "nextCodes": []
            }
        event = tentacle.register_router(TaskDetails(order_info))
        img = event.execute()
        memory_manager.save_perception(img)
        return img

    @staticmethod
    def gesture(mtype, position):
        order_info = {
                "id": "sys_gesture",
                "nodeCode": "public/gesture",
                "nodeType": 200,
                "displayName": "虚拟手",
                "platform": memory_manager.short_term.get_global("platform"),
                "data": {
                    "platform": memory_manager.short_term.get_global("platform"),
                    "position": position,
                    "sub_type": mtype
                },
                "lastCodes": [],
                "nextCodes": []
            }
        event = tentacle.register_router(TaskDetails(order_info))
        memory_manager.short_term.set_timeline_scope("gesture", {"mtype": mtype, "position": position})
        return event.execute()

    @staticmethod
    def windows(operation, target):
        order_info = {
                "id": "sys_window",
                "nodeCode": "public/window",
                "nodeType": 200,
                "displayName": "应用进程管理",
                "platform": memory_manager.short_term.get_global("platform"),
                "data": {
                    "platform": memory_manager.short_term.get_global("platform"),
                    "operation": operation,
                    "target": target
                },
                "lastCodes": [],
                "nextCodes": []
            }
        event = tentacle.register_router(TaskDetails(order_info))
        memory_manager.short_term.set_timeline_scope("window", {"operation": operation, "target": target})
        return event.execute()


    @staticmethod
    def keyevent(key_code=None):
        """
        专门处理设备级操作：如点亮屏幕、按电源键、按Home键
        """
        order_info = {
            "id": "sys_device_control",
            "nodeCode": "tools/keyevent",
            "nodeType": 200,
            "displayName": "设备控制",
            "platform": memory_manager.short_term.get_global("platform"),
            "data": {
                "event": key_code  # e.g., "POWER", "HOME"
            }
        }
        event = tentacle.register_router(TaskDetails(order_info))
        return event.execute()

    @staticmethod
    def input_text(text):
        """
        输入密码或文本
        """
        order_info = {
            "id": "sys_input",
            "nodeCode": "public/input",
            "nodeType": 200,
            "displayName": "文本输入",
            "platform": memory_manager.short_term.get_global("platform"),
            "data": {"text": text}
        }
        event = tentacle.register_router(TaskDetails(order_info))
        return event.execute()