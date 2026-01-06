# !/usr/bin/env python
# -*-coding:utf-8 -*-
from script.log import SLog
from driver.tentacle.core.base_logic import LogicBase
from driver.tentacle.component.router import BaseRouter

TAG = "MIf"


@BaseRouter.route('cfs/mIf')
class MIf(LogicBase):
    """
        This component performs conditional logic checks (If-Else).
    """
    META = {
        "inputs": [],
        "defaultData": {
            "conditions": [
                {"left": "", "op": "=", "right": ""}
            ],
            "logic": "AND"
        },
        "outputVars": []
    }
    index = "else"

    def on_check(self):
        pass

    def execute(self):
        conditions = self.get_param_value("conditions")
        branches = self.get_param_value("branches")
        logic_type = self.get_param_value("logic", "AND")

        # 判定
        is_match = self.evaluate_logic(conditions, logic_type)

        # 结果映射到分支索引
        # 通常逻辑：满足为 "0"，不满足为 "else"
        target_index = "0" if is_match else "else"
        self.index = target_index

        try:
            next_code = branches.get(str(target_index))
            if next_code:
                self.info.nextCodes = [next_code]
                SLog.i(self.TAG, f"Branch routing to: {target_index} -> {next_code}")
            else:
                SLog.w(self.TAG, f"No branch code found for: {target_index}")
        except Exception as e:
            SLog.e(self.TAG, f"Routing error: {e}")

        return self.result