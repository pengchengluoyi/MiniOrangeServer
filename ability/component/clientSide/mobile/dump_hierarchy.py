# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter
from server.routers.rAppGraph import sync_layout


@BaseRouter.route('mobile/dump_hierarchy')
class MobileDumpHierarchy(Template):
    """
        This component will close web browser.
    """
    META = {
        "inputs": [
            {
                "name": "platform",
                "type": "select",
                "desc": "适用平台 (辅助筛选)",
                "options": [
                    {"value": "android", "text": "Android"},
                    {"value": "ios", "text": "iOS"},
                    {"value": "windows", "text": "Windows"},
                    {"value": "mac", "text": "macOS"},
                    {"value": "web", "text": "Web"}
                ],
                "defaultValue": ""
            }
        ],
        "defaultData": {
            "platform": "",
        },
        "outputVars": [
        ]
    }


    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        xml = self.engine.driver.dump_hierarchy(compressed=False, pretty=False, max_depth=50)
        self.memory.set(self.info, "dom", xml)
        self.result.success()
        return self.result

