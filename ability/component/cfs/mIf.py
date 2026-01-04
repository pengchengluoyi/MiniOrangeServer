# !/usr/bin/env python
# -*-coding:utf-8 -*-
from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter

TAG = "MIf"


@BaseRouter.route('cfs/mIf')
class MIf(Template):
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
        conditions = self.get_param_value("conditions")  # 这里的 conditions 结构建议保持为一组
        branches = self.get_param_value("branches")
        logic_type = self.get_param_value("logic", "AND")

        # 判定这组条件是否成立
        is_match = self.evaluate_multi_conditions(conditions, logic_type)

        # 根据判定结果选择分支 (0 为 True 分支, else 为 False 分支)
        target_index = "0" if is_match else "else"

        try:
            self.index = target_index
            self.info.nextCodes = [branches[str(target_index)]]
            SLog.i(self.TAG, f"条件判定结果: {is_match}, 跳转分支: {target_index}")
        except KeyError:
            SLog.w(self.TAG, f"未找到对应分支: {target_index}")

        return self.result
