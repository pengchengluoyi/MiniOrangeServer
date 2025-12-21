#!/usr/bin/env python
# -*-coding:utf-8 -*-
import time
from typing import Any, Dict, Optional
from script.log import SLog, current_node_id

# 使用常量提升性能
_MS_MULTIPLIER = 1000


class TaskResult:
    """
    任务结果类
    示例:
    {
      "success": true,
      "code": 200,
      "message": "成功",
      "data": {...},
      "timestamp": {"start": 1704123456789, "success": 1704123456790}
    }
    """

    # 使用__slots__减少内存占用，提升属性访问速度
    __slots__ = ('_success', '_code', '_message', '_data', 'timestamp', '_token', '_TOKEN_NODE')

    def __init__(self):
        # 私有属性
        self._success = False
        self._code = None
        self._message = None
        self._data = None
        self._token = None
        self._TOKEN_NODE = None

        # 公共属性，记录时间戳
        self.timestamp = {}
        self._add_timestamp("start")

    # =================== 时间戳相关方法 ===================
    @staticmethod
    def _current_ms_timestamp() -> int:
        """获取当前毫秒时间戳 - 使用局部变量提升性能"""
        return int(time.time() * _MS_MULTIPLIER)

    def _add_timestamp(self, key: str) -> None:
        """添加时间戳记录"""
        self.timestamp[key] = self._current_ms_timestamp()
        SLog.i(str(self._token), str(self.timestamp))

    def _reset_context(self) -> None:
        """重置上下文变量"""
        if self._token:
            current_node_id.reset(self._TOKEN_NODE)
            self._token = None

    # =================== 状态设置方法 ===================
    def success(self, message: Optional[str] = None) -> None:
        """设置成功状态"""
        self._reset_context()
        self._success = True
        self._code = 3
        if message:
            self._message = message
        self._add_timestamp("success")

    def fail(self, message: Optional[str] = None) -> None:
        """设置失败状态"""
        self._reset_context()
        self._success = False
        self._code = -1
        if message:
            self._message = message
        self._add_timestamp("fail")

    def accept_order(self, order_info = None, message: Optional[str] = None) -> None:
        """接单状态"""
        self._reset_context()  # 防止重复调用导致之前的 token 丢失
        self._TOKEN_NODE = current_node_id.set(str(order_info.id))
        self._token = str(order_info.id)

        self._code = 0
        if message:
            self._message = message
        self._add_timestamp("accept_order")

    def dispatched(self, message: Optional[str] = None) -> None:
        """派发状态"""
        self._code = 1
        if message:
            self._message = message
        self._add_timestamp("dispatched")

    def self_check(self, message: Optional[str] = None) -> None:
        """自检状态"""
        self._code = 2
        if message:
            self._message = message
        self._add_timestamp("self_check")

    # =================== 属性访问器 ===================
    @property
    def success_flag(self) -> bool:
        """成功标志"""
        return self._success

    @success_flag.setter
    def success_flag(self, value: bool) -> None:
        self._success = value

    @property
    def code(self) -> Any:
        """状态码"""
        return self._code

    @code.setter
    def code(self, value: Any) -> None:
        self._code = value

    @property
    def message(self) -> Optional[str]:
        """消息"""
        return self._message

    @message.setter
    def message(self, value: str) -> None:
        self._message = value

    @property
    def data(self) -> Any:
        """数据"""
        return self._data

    @data.setter
    def data(self, value: Any) -> None:
        self._data = value

    # =================== 输出方法 ===================
    def to_dict(self) -> Dict[str, Any]:
        """
        转换为字典，移除私有属性的下划线前缀

        Returns:
            包含所有公共属性的字典
        """
        result = {}

        # 处理私有属性，移除下划线前缀
        for attr_name in self.__slots__:
            if attr_name == '_token' or attr_name == '_TOKEN_NODE':
                continue
            if attr_name.startswith('_'):
                # 移除开头的下划线
                public_name = attr_name[1:] if attr_name.startswith('_') else attr_name
                # 获取属性值
                try:
                    value = getattr(self, attr_name)
                    result[public_name] = value
                except AttributeError:
                    continue
            else:
                # 直接添加公共属性
                try:
                    value = getattr(self, attr_name)
                    result[attr_name] = value
                except AttributeError:
                    continue

        return result

    def to_json(self, include_none: bool = True) -> Dict[str, Any]:
        """
        转换为JSON友好的字典格式

        Args:
            include_none: 是否包含值为None的字段

        Returns:
            JSON格式的字典
        """
        result = self.to_dict()

        if not include_none:
            # 过滤掉值为None的字段
            result = {k: v for k, v in result.items() if v is not None}

        return result

    def __str__(self) -> str:
        """字符串表示"""
        return f"TaskResult(success={self._success}, code={self._code})"

    def __repr__(self) -> str:
        """调试表示"""
        return f"TaskResult(success={self._success}, code={self._code}, data={type(self._data).__name__})"

    # =================== 辅助方法 ===================
    def is_success(self) -> bool:
        """判断是否成功 - 保持向后兼容"""
        return self._success

    def set_result_data(self, data: Any) -> None:
        """设置结果数据 - 保持向后兼容"""
        self._data = data

    def result_data(self, data: Any) -> None:
        """设置结果数据 - 保持向后兼容"""
        self._data = data
