from ability.core.base_logic import LogicBase
from ability.component.router import BaseRouter
from script.log import SLog


@BaseRouter.route('cfs/mAssert')
class MAssert(LogicBase):

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
    TAG = "MAssert"

    def execute(self):
        conditions = self.get_param_value("conditions")
        logic_type = self.get_param_value("logic", "AND")

        # 执行判定
        is_passed = self.evaluate_logic(conditions, logic_type)

        if is_passed:
            self.result.success("Assertion Passed")
        else:
            # 关键：调用 fail 会标记任务状态为失败，并通常由引擎阻断后续步骤
            msg = f"Assertion Failed: conditions {conditions} with logic {logic_type}"
            SLog.e(self.TAG, msg)
            self.result.fail(msg)

        return self.result