# driver/tentacle/component/std/screenshot.py
# !/usr/bin/env python
# -*-coding:utf-8 -*-

import os
import time
import random

from script.log import SLog
from driver.tentacle.component.template import Template
from driver.tentacle.component.router import BaseRouter
from driver.tentacle.engine.vision.mPositionCalculation import PositionManager
from server.core.database import APP_DATA_DIR

TAG = "Vision"


@BaseRouter.route('tools/vision')
class Vision(Template):
    """
    [纯视觉传感器]
    只负责：全屏截图 + 视觉分析 (OCR)
    不再包含任何 DOM 元素查找或定位逻辑。
    """

    META = {
        "inputs": [
        ],
        "defaultData": {
        },
        "outputVars": [
            {"key": "img", "type": "str", "desc": "x: xxx, y: yyy"},
        ]
    }

    def execute(self):
        try:
            self.get_engine()
            current_img = self.engine.screenshot()
            self.memory.set(self.info, "img", current_img)
            return current_img
        except Exception as e:
            SLog.w(TAG, e)
