# !/usr/bin/env python
# -*-coding:utf-8 -*-

from ability.core.step_result import StepResult
from ability.component.template import Template
from ability.component.router import BaseRouter

import script.constPath.component_code as MCode

@BaseRouter.route('cfs/mFor')
class MFor(Template):
    """
        This component will close web browser.
    """
    META = {
        "name": "M",
        "inputs": [{"key": "path", "type": "string"}]
    }

    def on_check(self):
        ...

    def execute(self):
        if self.info.nodeType == MCode.MCFS_FOR_RANDOM:
            if self.memory.get(self.info.id):
                self.memory.set(self.info, int(self.memory.get(self.info.id)) + 1)
                if int(self.memory.get(self.info.id)) > int(self.get_param_value("index")):
                    self.result.success()
            else:
                self.memory.set(self.info, 1)
        return StepResult()


