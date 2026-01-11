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
                "displayName": "视觉感知(获取)",
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
                "displayName": "视觉感知(点击)",
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
        return event.execute()
