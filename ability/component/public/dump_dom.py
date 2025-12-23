# !/usr/bin/env python
# -*-coding:utf-8 -*-

import sys
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
                # Hotfix: 修复 Windows Engine 中可能缺失 Desktop 引用导致的 NameError
                # 报错提示: name 'Desktop' is not defined，说明引擎模块缺少 from pywinauto import Desktop
                try:
                    engine_mod = sys.modules[self.engine.__module__]
                    
                    # 1. 确保 Desktop 类存在 (解决 NameError)
                    if hasattr(engine_mod, 'Desktop'):
                        Desktop = getattr(engine_mod, 'Desktop')
                    else:
                        from pywinauto import Desktop
                        setattr(engine_mod, 'Desktop', Desktop)

                    # 2. Hotfix: 修复 Desktop.active() 不存在导致的 Fallback 失败 (解决 wrapper method 'active' not found)
                    # 引擎尝试调用 .active()，但 pywinauto 原生不支持，这里手动打补丁模拟该方法
                    if not hasattr(Desktop, 'active'):
                        # 修正：原先使用 active_only=True 如果找不到活动窗口会报错 ElementNotFoundError
                        # 改为：直接返回 self (Desktop对象本身)，即获取整个桌面的布局
                        # 这样即使没有"活动窗口"，也能 Dump 出整个屏幕的结构，避免崩溃
                        setattr(Desktop, 'active', lambda self: self)
                except Exception:
                    pass

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
