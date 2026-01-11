# driver/tentacle/core/memory.py
# !/usr/bin/env python
# -*-coding:utf-8 -*-
from script.singleton_meta import SingletonMeta
from script.log import SLog
from driver.agent.Memory import memory_manager

class Memory(metaclass=SingletonMeta):
    """
    [代理模式]
    触角内存本身不再存储任何数据，它只是海马体在肢体末端的代理接口。
    """

    def set(self, info, var_name: str, var_value: any):
        """
        [写操作]：ocr.py 调用这个方法时，数据直接流入大脑
        """
        if memory_manager:
            # 从 info 对象中提取节点 ID，确保记忆有迹可循
            node_id = getattr(info, 'id', 'unknown')
            # 调用海马体的写入接口
            memory_manager.save_node_result(node_id, var_name, var_value)
        else:
            SLog.e("Memory", "海马体未连接！数据丢失！")

    def get(self, var_name: str) -> any:
        """
        [读操作]：原子能力需要参数时，从大脑读取
        """
        if memory_manager:
            return memory_manager.recall(var_name)
        return None