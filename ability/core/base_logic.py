# !/usr/bin/env python
# -*-coding:utf-8 -*-
from script.log import SLog
from ability.component.template import Template


class LogicBase(Template):
    """
    逻辑判定基类：提供变量解析、类型转换及多条件组合判定功能
    """
    TAG = "LogicBase"

    def _get_value(self, value):
        """
        获取真实值：支持 {{var}} 模板语法解析
        """
        if not isinstance(value, str):
            return self._convert_type(value)

        val_stripped = value.strip()
        if val_stripped.startswith("{{") and val_stripped.endswith("}}"):
            var_name = val_stripped[2:-2].strip()
            # 从上下文中获取变量
            actual_val = self.memory.get(var_name)
            SLog.d(self.TAG, f"Variable parse: {var_name} = {actual_val}")
            return self._convert_type(actual_val)

        return self._convert_type(value)

    def _convert_type(self, value):
        """
        鲁棒的类型转换：处理布尔、数字、None
        """
        if value is None: return None
        if not isinstance(value, str): return value

        lower_val = value.lower()
        if lower_val == 'true': return True
        if lower_val == 'false': return False
        if lower_val in ['null', 'none', '']: return None

        try:
            if '.' in value: return float(value)
            return int(value)
        except (ValueError, TypeError):
            return value

    def _compare(self, left_raw, op, right_raw):
        """
        执行单个表达式的比较
        """
        l_val = self._get_value(left_raw)
        r_val = self._get_value(right_raw)

        try:
            # 字符串类比较
            if op in ['=', '==']: return str(l_val) == str(r_val)
            if op == '!=': return str(l_val) != str(r_val)
            if op in ['contains', 'in']: return str(r_val) in str(l_val)
            if op == 'not contains': return str(r_val) not in str(l_val)

            # 数值类比较（强制转 float 保证鲁棒性）
            if op == '>': return float(l_val) > float(r_val)
            if op == '>=': return float(l_val) >= float(r_val)
            if op == '<': return float(l_val) < float(r_val)
            if op == '<=': return float(l_val) <= float(r_val)

            SLog.w(self.TAG, f"Unknown operator: {op}")
            return False
        except Exception as e:
            SLog.e(self.TAG, f"Comparison error ({l_val} {op} {r_val}): {e}")
            return False

    def evaluate_logic(self, conditions, logic_type="AND"):
        """
        核心方法：多表达式判定逻辑实现
        """
        if not conditions:
            return True

        # 计算所有条件的结果列表
        results = []
        for cond in conditions:
            res = self._compare(cond.get('left'), cond.get('op'), cond.get('right'))
            results.append(res)

        # 处理 AND/OR 逻辑组合
        if logic_type.upper() == "OR":
            final_res = any(results)
        else:
            final_res = all(results)

        SLog.i(self.TAG, f"Logic Check [{logic_type}]: {results} -> Final: {final_res}")
        return final_res