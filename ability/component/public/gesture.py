# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter

TAG = "GESTURE"

@BaseRouter.route('public/gesture')
class Gesture(Template):
    """
        Mouse/Touch Gesture operations (Cross-Platform)
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
            },
            {
                "name": "sub_type",
                "type": "select",
                "desc": "动作类型",
                "defaultValue": "click",
                "options": [

                    {"value": "click", "text": "单击"},
                    {"value": "double", "text": "双击"},
                    {"value": "right-click", "text": "右键", "show_if": ["web", "windows", "mac"]},
                    {"value": "long_press", "text": "长按"},
                    {"value": "hover", "text": "悬停 (Hover)", "show_if": ["windows", "mac", "web"]},
                    {"value": "drag", "text": "拖拽/滑动 (Drag/Swipe)"}
                ]
            },
            {
                "name": "locator_chain",
                "type": "list",
                "desc": "源元素 (起点)",
                "add_text": "添加节点",
                "sub_inputs": [
                    {
                        "name": "id",
                        "type": "str",
                        "desc": "唯一标识 (ID/Name/AutoID)",
                        "placeholder": "ResourceID/AutoID/Name"
                    },
                    {
                        "name": "text",
                        "type": "str",
                        "desc": "文本/标题 (Text/Label/Title)",
                        "placeholder": "显示文本/Window Title"
                    },
                    {
                        "name": "type",
                        "type": "str",
                        "desc": "控件类型 (Class/ControlType/Role)",
                        "placeholder": "Button/TextView/XCUIElementTypeButton"
                    },
                    {
                        "name": "desc",
                        "type": "str",
                        "desc": "描述 (ContentDesc/Help)",
                        "placeholder": "无障碍描述",
                        "show_if": ["android", "ios"]
                    },
                    {
                        "name": "xpath",
                        "type": "str",
                        "desc": "XPath (Web/Mobile)",
                        "placeholder": "//...",
                        "show_if": ["web", "android", "ios"]
                    },
                    {
                        "name": "css",
                        "type": "str",
                        "desc": "CSS Selector (Web)",
                        "placeholder": ".class #id",
                        "show_if": ["web"]
                    },
                    {
                        "name": "index",
                        "type": "int",
                        "desc": "常用语定位列表中的第几位",
                        "placeholder": "0"
                    }
                ]
            },
            {
                "name": "target_locator_chain",
                "type": "list",
                "desc": "目标元素 (终点 - 仅拖拽)",
                "add_text": "添加节点",
                # 复用相同的定位结构
                "sub_inputs": [
                    {"name": "id", "type": "str", "desc": "唯一标识", "placeholder": "ResourceID/AutoID/Name"},
                    {"name": "text", "type": "str", "desc": "文本/标题", "placeholder": "Text/Label/Title"},
                    {"name": "type", "type": "str", "desc": "控件类型", "placeholder": "Class/ControlType"},
                    {"name": "desc", "type": "str", "desc": "描述", "placeholder": "ContentDesc", "show_if": ["android", "ios"]},
                    {"name": "xpath", "type": "str", "desc": "XPath", "placeholder": "//...", "show_if": ["web", "android", "ios"]},
                    {"name": "css", "type": "str", "desc": "CSS", "placeholder": ".class #id", "show_if": ["web"]},
                    {"name": "index", "type": "int", "desc": "Index", "placeholder": "0"}
                ]
            }
        ],
        "defaultData": {
            "platform": "",
            "sub_type": "click",
            "locator_chain": [],
            "target_locator_chain": []
        },
        "outputVars": []
    }

    def on_check(self):
        pass

    def execute(self):
        self.get_engine()
        sub_type = self.get_param_value("sub_type")
        locator_chain = self.get_param_value("locator_chain")
        
        source = self.engine.find_element(locator_chain)
        if not source:
            SLog.d(TAG, "Source element not found. Switching to OCR now")
            from ability.component.public.ocr import analyze
            
            # 1. 截图并识别
            img = self.engine.screenshot()
            ocr_result = analyze(None, img)
            self.memory.set(self.info, "ocr_result", ocr_result)

            # 2. 从定位链中提取目标文本
            target_text = None
            for node in locator_chain:
                target_text = node.get("text") or node.get("desc")
                if target_text:
                    break
            
            # 3. 在 OCR 结果中查找文本并获取坐标
            if target_text:
                for item in ocr_result:
                    if target_text in item.get("text", ""):
                        # 提取中心点坐标，例如 (568, 265)
                        center = item.get("coordinates", {}).get("center")
                        SLog.i(TAG, f"OCR matched '{target_text}' at {center}. Performing {sub_type}.")
                        
                        # 4. 使用坐标执行动作
                        if sub_type == 'double':
                            self.engine.double_click(None, position=center)
                        elif sub_type == 'right-click' or sub_type == 'long_press':
                            self.engine.context_click(None, position=center)
                        elif sub_type == 'drag':
                            SLog.w(TAG, "Drag via OCR not supported yet")
                            self.result.fail()
                            return self.result
                        else:
                            self.engine.click(None, position=center)
                        
                        self.result.success()
                        return self.result
            
            SLog.e(TAG, f"Element not found via Locator or OCR (Target Text: {target_text})")
            self.result.fail()
            return self.result

        try:
            if sub_type == 'drag':
                target_locator_chain = self.get_param_value("target_locator_chain")
                target = self.engine.find_element(target_locator_chain)
                
                if not target:
                    SLog.e(TAG, "Target element not found for drag")
                    self.result.fail()
                    return self.result
                
                self.engine.drag_and_drop(source, target)
            else:
                # 默认悬停
                self.engine.hover(source)
            
            self.result.success()
        except Exception as e:
            SLog.e(TAG, f"Gesture action failed: {e}")
            self.result.fail()

        return self.result