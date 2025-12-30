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

# 编译清洗规则，匹配各种零宽字符
_INVISIBLE_CHARS_PATTERN = re.compile(r'[\u200b-\u200d\ufeff]')

def clean_invisible_chars(data):
    """递归清洗字符串中的不可见字符"""
    if isinstance(data, str):
        return _INVISIBLE_CHARS_PATTERN.sub('', data)
    elif isinstance(data, list):
        # 如果是列表（如 locator_chain），对内部每个元素进行处理
        return [clean_invisible_chars(item) for item in data]
    elif isinstance(data, dict):
        # 如果是字典（如 locator 节点），清洗所有的值
        return {k: clean_invisible_chars(v) for k, v in data.items()}
    return data


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
        param_name = clean_invisible_chars(param_name)
        pattern = r'\{\{([^{}]+)\}\}'
        if isinstance(param_name, str) and re.match(pattern, param_name):
            return self.memory.get(param_name)


        if param_name in self.info.data:
            val = clean_invisible_chars(self.info.data[param_name])
            SLog.d(TAG, "Getting value of parameter '{}', and Parameter value is '{}'".format(param_name, val))

            if isinstance(val, str) and re.match(pattern, val):
                return self.memory.get(val)
            return val
        else:
            SLog.w(TAG, "Parameter '{}' is not defined".format(param_name))
            return False

    def execute(self):
        ...

    def build_chain(self, locator_chain):
        return self.engine.build_chain(locator_chain)

