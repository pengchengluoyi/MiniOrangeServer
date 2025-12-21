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

    def _normalize_value(self, value):
        """
        辅助方法：尝试将字符串转换为 Python 的原生类型以便比较
        """
        if not isinstance(value, str):
            return value

        # 处理布尔值字符串
        lower_val = value.lower()
        if lower_val == 'true':
            return True
        if lower_val == 'false':
            return False

        # 处理 None/Null
        if lower_val in ['null', 'none']:
            return None

        # 尝试处理数字 (优先转 int, 失败转 float, 再失败保持 string)
        try:
            if '.' in value:
                return float(value)
            return int(value)
        except ValueError:
            return value

    def _compare_values(self, left, op, right):
        """
        辅助方法：执行具体的比较逻辑
        """
        # 对左右值进行类型标准化
        l_val = self._normalize_value(left)
        r_val = self._normalize_value(right)

        try:
            if op == '=' or op == '==':
                # 尝试强制转换类型比较 (解决 1 == "1" 的问题)
                return str(l_val) == str(r_val)
            elif op == '!=':
                return str(l_val) != str(r_val)
            elif op == '>':
                return float(l_val) > float(r_val)
            elif op == '>=':
                return float(l_val) >= float(r_val)
            elif op == '<':
                return float(l_val) < float(r_val)
            elif op == '<=':
                return float(l_val) <= float(r_val)
            elif op == 'contains' or op == 'in':
                # 判断 left 是否包含 right
                return str(r_val) in str(l_val)
            elif op == 'not contains':
                return str(r_val) not in str(l_val)
            else:
                SLog.w(TAG, "Unknown operator: {}".format(op))
                return False
        except Exception as e:
            SLog.e(TAG, "Comparison error: {}".format(e))
            return False

    def _extract_variable_name(self, expression):
        """
        从表达式中提取变量名
        如果是 {{variable}} 格式，提取变量名
        否则返回原表达式
        """
        if not isinstance(expression, str):
            return expression

        expression = expression.strip()

        # 判断是否为模板格式
        if expression.startswith("{{") and expression.endswith("}}"):
            # 提取变量名
            variable_name = expression[2:-2].strip()
            return variable_name

        return expression

    def _get_actual_value(self, value):
        """
        获取实际值
        如果是 {{variable}} 格式，则从上下文中获取变量值
        否则直接返回值
        """
        if isinstance(value, str):
            # 检查是否为模板格式
            if value.strip().startswith("{{") and value.strip().endswith("}}"):
                variable_name = self._extract_variable_name(value)
                # 从组件上下文中获取变量值
                actual_value = self.memory.get(variable_name)
                SLog.d(TAG, f"解析模板变量: {value} -> {variable_name} = {actual_value}")
                return actual_value

        # 处理布尔值字符串和数字
        return self._convert_value_type(value)

    def _convert_value_type(self, value):
        """
        转换值类型
        """
        if isinstance(value, str):
            # 处理布尔值
            if value.lower() == 'true':
                return True
            elif value.lower() == 'false':
                return False

            # 尝试转换为数字
            try:
                if '.' in value:
                    return float(value)
                else:
                    return int(value)
            except (ValueError, TypeError):
                pass

        return value

    def execute(self):

        # 1. 获取条件列表和逻辑关系
        conditions = self.get_param_value("conditions")
        branches = self.get_param_value("branches")
        # logic_type = self.get_param_value("logic")  # 获取 'AND' 或 'OR'

        if not conditions:
            SLog.w(TAG, "No conditions found, defaulting to True")
            return self.result

        # if not logic_type:
        #     logic_type = "AND"
        # check_results = []

        # 2. 遍历所有条件进行判断
        for key, condition in enumerate(conditions):
            left = condition.get('left')
            op = condition.get('op')
            right = condition.get('right')

            # 获取实际值（自动处理模板变量）
            left_value = self._get_actual_value(left)
            right_value = self._get_actual_value(right)

            SLog.d(TAG, f"实际值: left={left_value}, right={right_value}")

            # 执行单条比较
            res = self._compare_values(left_value, op, right_value)
            if res:
                self.index = key
                break

            # check_results.append(res)
        try:
            self.info.nextCodes = [branches[str(self.index)]]
            SLog.i(TAG, branches[str(self.index)])
        except KeyError:
            ...

        return self.result