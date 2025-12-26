# !/usr/bin/env python
# -*-coding:utf-8 -*-
import requests
import re
from script.singleton_meta import SingletonMeta
from script.log import SLog

TAG: str = "Memory"


class Memory(metaclass=SingletonMeta):
    # 数据结构变更: { node_id: { var_name: var_value, ... } }
    mDict = {}

    def set(self, info, var_name: str, var_value: any):
        """
        存储变量
        :param info: CaseInfo 对象，从中获取 node_id
        :param var_name: 变量名 (str)
        :param var_value: 变量值
        """
        node_id = info.id

        # 如果该节点还没有任何数据，先初始化一个空字典
        if node_id not in self.mDict:
            self.mDict[node_id] = {}

        # 存入/更新变量
        self.mDict[node_id][var_name] = var_value

        SLog.i(TAG, f"Node [{node_id}] set variable [{var_name}] = [{var_value}]")
        self._broadcast(node_id, var_name, var_value)

    @staticmethod
    def _broadcast(node_id, var_name, var_value):
        try:
            data = {
                "type": "memory_update",
                "content": {
                    "node_id": node_id,
                    "var_name": var_name,
                    "var_value": var_value
                }
            }
            # 发送 HTTP 请求给主进程 (假设运行在本地 8000 端口)
            # timeout 设置短一点(0.2s)，避免因为网络问题阻塞脚本执行
            # requests.post("http://127.0.0.1:8000/ws/internal/broadcast", json=data, timeout=0.2)
        except Exception as e:
            # 广播失败属于非关键错误，记录警告即可，不要抛出异常中断流程
            SLog.w(TAG, f"WebSocket broadcast warning: {e}")

    def get(self, node_var_name: str) -> any:
        """
        获取变量
        :param node_var_name:
        :return: 变量值 or None
        """
        if not node_var_name or not isinstance(node_var_name, str):
            return None

        pattern = r'\{\{([^{}]+)\}\}'
        if re.match(pattern, node_var_name):
            node_var_name = node_var_name[2:-2].strip()

        node_id, var_name = node_var_name.split(".")
        # 1. 检查节点是否存在
        if node_id not in self.mDict:
            SLog.w(TAG, f"Node [{node_id}] not found in store.")
            return None

        # 2. 检查该节点下是否有该变量
        node_vars = self.mDict[node_id]
        if var_name in node_vars:
            return node_vars[var_name]
        else:
            SLog.w(TAG, f"Variable [{var_name}] not found in Node [{node_id}]. Returning None.")
            return None

    def get_all_from_node(self, node_id: str) -> dict:
        """
        获取某节点下的所有变量 (辅助方法)
        """
        return self.mDict.get(node_id, {})

    def update(self, info, var_name: str, var_value: any):
        """
        更新变量 (其实逻辑和 set 是一样的，保留此方法为了兼容你的习惯)
        """
        self.set(info, var_name, var_value)
