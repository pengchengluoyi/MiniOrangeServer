# !/usr/bin/env python
# -*-coding:utf-8 -*-
from script.log import SLog
from ability.component.template import Template


class BaseLogic(Template):
    TAG = "BaseLogic"

    def _get_actual_value(self, value):
        """获取实际值：处理模板变量 {{var}}、布尔值和数字"""
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("{{") and stripped.endswith("}}"):
                var_name = stripped[2:-2].strip()
                actual_value = self.memory.get(var_name)
                SLog.d(self.TAG, f"变量解析: {value} -> {actual_value}")
                return actual_value
        return self._convert_type(value)

    def _convert_type(self, value):
        """转换原生类型"""
        if not isinstance(value, str):
            return value

        low_val = value.lower()
        if low_val == 'true': return True
        if low_val == 'false': return False
        if low_val in ['null', 'none', '']: return None

        try:
            return float(value) if '.' in value else int(value)
        except (ValueError, TypeError):
            return value

    def _compare(self, left, op, right):
        """核心比较逻辑"""
        l_val = self._get_actual_value(left)
        r_val = self._get_actual_value(right)

        try:
            if op in ['=', '==']: return str(l_val) == str(r_val)
            if op == '!=': return str(l_val) != str(r_val)
            if op == '>':  return float(l_val) > float(r_val)
            if op == '>=': return float(l_val) >= float(r_val)
            if op == '<':  return float(l_val) < float(r_val)
            if op == '<=': return float(l_val) <= float(r_val)
            if op in ['contains', 'in']: return str(r_val) in str(l_val)
            if op == 'not contains': return str(r_val) not in str(l_val)
            return False
        except Exception as e:
            SLog.e(self.TAG, f"比较出错: {l_val} {op} {r_val} -> {e}")
            return False

    def evaluate_multi_conditions(self, conditions, logic_type="AND"):
        """
        处理多个表达式的判定逻辑
        :param conditions: 条件列表
        :param logic_type: "AND" 或 "OR"
        """
        if not conditions:
            return True

        results = [self._compare(c.get('left'), c.get('op'), c.get('right')) for c in conditions]

        if logic_type.upper() == "OR":
            return any(results)
        return all(results)  # 默认为 AND