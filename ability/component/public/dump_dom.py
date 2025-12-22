# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter
from server.routers.rAppGraph import sync_layout


@BaseRouter.route('public/dump_dom')
class PublicDumpHierarchy(Template):
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
        content = ""

        try:
            # 1. Windows (Engine 封装方法)
            if hasattr(self.engine, 'dump_hierarchy'):
                content = self.engine.dump_hierarchy(max_depth=50)

            # 2. Web / Mac / Appium (Driver 属性: page_source)
            elif hasattr(self.engine.driver, 'page_source'):
                content = self.engine.driver.page_source

            # 3. Android (uiautomator2 Driver 方法)
            elif hasattr(self.engine.driver, 'dump_hierarchy'):
                try:
                    content = self.engine.driver.dump_hierarchy(compressed=False, pretty=False, max_depth=50)
                except TypeError:
                    # 兼容标准 uiautomator2 不带 max_depth 参数的情况
                    content = self.engine.driver.dump_hierarchy(compressed=False, pretty=False)

            # 4. iOS (WDA Driver 方法: source)
            elif hasattr(self.engine.driver, 'source'):
                content = self.engine.driver.source()

        except Exception as e:
            SLog.e("DumpHierarchy", f"Get hierarchy failed: {e}")

        self.memory.set(self.info, "dom", str(content) if content else "")
        self.result.success()
        return self.result
