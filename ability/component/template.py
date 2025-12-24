# !/usr/bin/env python
# -*-coding:utf-8 -*-
import re

from driver.agent.actuator import process_runner_wrapper
from script.log import SLog
from ability.core.memory import Memory
from ability.core.step_result import StepResult
from ability.manager import Manager
import ability.common.platform as platform_code

TAG = "Template"


class Template:

    # === 这里定义所有组件的默认配置 ===
    DEFAULT_META = {
        "type": 200,
        "name": "未命名组件",
        "icon": "default_icon",
        "inputs": [],
        "outputVars": [
            {"key": "status", "label": "执行结果", "type": "boolean"}
        ],
        "defaultData": {}
    }

    def __init__(self, info):
        self.info = info
        self.engine = None
        self.memory = Memory()
        self.result = StepResult()
        self.get_engine()

    def get_engine(self):
        if self.info.platform in platform_code.MWEB:
            self.engine = Manager().WebEngine
        elif self.info.platform in platform_code.MMOBILE:
            self.engine = Manager().MobileEngine
        elif self.info.platform in platform_code.MPC:
            self.engine = Manager().PCEngine

    def get_param_value(self, param_name):
        pattern = r'\{\{([^{}]+)\}\}'
        if re.match(pattern, param_name):
            return self.memory.get(param_name)


        if param_name in self.info.data:
            SLog.d(TAG, "Getting value of parameter '{}', and Parameter value is '{}'".format(param_name, self.info.data[param_name]))

            if re.match(pattern, self.info.data[param_name]):
                return self.memory.get(self.info.data[param_name])
            return self.info.data[param_name]
        else:
            SLog.w(TAG, "Parameter '{}' is not defined".format(param_name))
            return False

    def execute(self):
        ...

    def build_chain(self, locator_chain):
        return self.engine.build_chain(locator_chain)

if __name__ == '__main__':
    sttt = "{{public-screenshot-1766553312798.path}}"

    pattern = r'\{\{([^{}]+)\}\}'
    if re.match(pattern, sttt):
        print()