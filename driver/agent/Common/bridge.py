# driver/brain/common/bridge.py
# !/usr/bin/env python
# -*-coding:utf-8 -*-
import builtins
from script.log import SLog


class ServerBridge:
    """
    [神经-云端 桥梁]
    负责将 Brain 的请求通过 Client 的 IPC 通道发送给 Server。
    """

    @staticmethod
    def query(action, params=None, timeout=10):
        if params is None: params = {}

        # 获取 Client 注入的全局函数
        query_func = getattr(builtins, "SERVER_QUERY", None)

        if not query_func:
            SLog.e("ServerBridge", "❌ 无法连接到 Client 载体 (SERVER_QUERY 未注入)")
            return None

        try:
            # SLog.d("ServerBridge", f"📡 向云端发送请求: {action}")
            result = query_func(action, params, timeout)
            return result
        except Exception as e:
            SLog.e("ServerBridge", f"通讯失败: {e}")
            return None