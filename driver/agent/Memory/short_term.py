# memory/short_term.py
import time

_MS_MULTIPLIER = 1000

class ShortTermMemory:
    def __init__(self):
        # 1. 节点私有记忆: { "node_id": { "var_name": "value" } }
        self.node_scope = {}

        # 2. 全局运行记忆: { "latest_ocr": "...", "screen_width": 1080 }
        self.global_scope = {}

        # 3.时间线记忆
        self.timeline_scope = {}

    @staticmethod
    def _current_ms_timestamp() -> int:
        """获取当前毫秒时间戳 - 使用局部变量提升性能"""
        return str(int(time.time() * _MS_MULTIPLIER))

    def set_node_data(self, node_id, key, value):
        """写入节点私有数据"""
        if node_id not in self.node_scope:
            self.node_scope[node_id] = {}
        self.node_scope[node_id][key] = value

    def get_node_data(self, node_id, key):
        """读取节点私有数据"""
        return self.node_scope.get(node_id, {}).get(key)

    def set_global(self, key, value):
        """写入全局数据"""
        self.global_scope[key] = value

    def get_global(self, key):
        """读取全局数据"""
        return self.global_scope.get(key)

    def set_timeline_scope(self, mType, scope):
        self.timeline_scope[self._current_ms_timestamp()] =  {
            "type": mType,
            "data": scope
        }

    def clear(self):
        self.node_scope.clear()
        self.global_scope.clear()