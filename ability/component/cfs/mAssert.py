from .base_logic import BaseLogic
from ability.component.router import BaseRouter
from script.log import SLog


@BaseRouter.route('cfs/mAssert')
class MAssert(BaseLogic):
    TAG = "MAssert"

    def execute(self):
        conditions = self.get_param_value("conditions")
        logic_type = self.get_param_value("logic", "AND")
        # 新增：是否阻断任务执行（默认开启）
        abort_on_fail = self.get_param_value("abortOnFail", True)

        # 执行多条件判定
        is_passed = self.evaluate_multi_conditions(conditions, logic_type)

        if is_passed:
            SLog.i(self.TAG, "断言通过")
            self.result.success("所有断言条件已满足")
        else:
            msg = f"断言失败: 条件逻辑为 {logic_type}"
            SLog.e(self.TAG, msg)

            if abort_on_fail:
                # 关键：调用 fail 会导致框架层级识别到任务异常，从而停止后续执行
                self.result.fail(msg)
            else:
                # 仅记录不通过，但不停止流程
                self.result.success(f"断言不满足(未阻断): {msg}")

        return self.result