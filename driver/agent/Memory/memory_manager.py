# memory/manager.py
import io
import base64
import threading
import re
from script.singleton_meta import SingletonMeta
from script.log import SLog, current_run_id
from driver.agent.Memory.short_term import ShortTermMemory
from driver.agent.Memory.long_term import LongTermMemory
from driver.agent.Memory.checklist import Checklist
from driver.agent.Common.bridge import ServerBridge


class MemoryManager(metaclass=SingletonMeta):
    def __init__(self):
        self.short_term = ShortTermMemory()
        # self.long_term = LongTermMemory()
        self.long_term = LongTermMemory()
        self.checklist = Checklist()

        # 全局大锁，防止读写冲突
        self._lock = threading.RLock()
        self.initialized = True

    def initialize(self, flow_id=None):
        """初始化加载"""
        self.short_term.clear()
        # self.long_term.load_async(flow_id)

    # ================= 存数据接口 =================

    def save_node_result(self, node_id, key, value):
        """保存节点执行结果 (对应 consolidate)"""
        with self._lock:
            # 存两份：一份给节点私有，一份给全局方便查
            self.short_term.set_node_data(node_id, key, value)
            self.short_term.set_global(key, value)

    def save_perception(self, perception_data):
        """保存视觉感知数据"""
        with self._lock:
            self.short_term.set_global("latest_perception", perception_data)
            self.short_term.set_timeline_scope("screenshot", perception_data)

    # ================= 取数据接口 =================
    def recall(self, query):
        """
        智能检索：支持 {{ node_id.key }} 或 {{ global_key }} 语法
        """
        if not isinstance(query, str): return query

        pattern = r'\{\{([^{}]+)\}\}'
        match = re.search(pattern, query)

        if match:
            path = match.group(1).strip()
            value = None

            with self._lock:
                # 1. 尝试解析 "node_id.key"
                if "." in path:
                    parts = path.split(".", 1)
                    value = self.short_term.get_node_data(parts[0], parts[1])

                # 2. 如果没找到，查全局短期记忆
                if value is None:
                    value = self.short_term.get_global(path)

                # 3. 如果还没找到，查长期记忆 (配置)
                if value is None:
                    value = self.long_term.get_config(path)

            # 替换逻辑
            if value is not None:
                if query == match.group(0):
                    return value
                return query.replace(match.group(0), str(value))

        return query

    def get_latest_perception(self):
        with self._lock:
            return self.short_term.get_global("latest_perception")

    def sync_timeline(self):
        """
        处理并上传时间线数据到服务端
        1. 上传截图获取 URL
        2. 格式化数据
        3. 调用 sync_timeline 接口入库
        """
        with self._lock:
            # 复制一份数据防止并发修改
            raw_timeline = self.short_term.timeline_scope.copy()

        if not raw_timeline:
            return

        processed_timeline = {}

        run_id = current_run_id.get()

        for ts, info in raw_timeline.items():
            item_type = info.get("type")
            item_data = info.get("data")
            
            # 构造新的记录对象
            record = {"type": item_type, "data": item_data}

            # 1. 处理截图: 转 Base64 -> 上传 -> 替换为 URL
            if item_type in ["screenshot", "screen"] and hasattr(item_data, "save"):
                try:
                    buf = io.BytesIO()
                    # 转换为 RGB 并保存为 JPEG 以减小体积，防止 WebSocket 传输超时
                    save_img = item_data
                    if save_img.mode in ("RGBA", "P"):
                        save_img = save_img.convert("RGB")
                    save_img.save(buf, format="JPEG", quality=70)
                    b64_str = base64.b64encode(buf.getvalue()).decode("utf-8")
                    
                    filename = f"timeline_{run_id}_{ts}.jpg"
                    # 增加超时时间到 60秒，防止网络波动或图片过大导致超时 (虽然服务端可能已经保存成功)
                    res = ServerBridge.query("upload", {"name": filename, "content": b64_str}, timeout=60)
                    SLog.i("sync_timeline", res)
                    
                    if res and res.get("url"):
                        # 兼容返回格式: 优先取根节点的 filename，其次取 data.url
                        record["data"] = res.get("filename") or res.get("path")
                    else:
                        record["data"] = "upload_failed"
                        SLog.e("MemoryManager", f"Timeline upload failed. Res: {res}")
                except Exception as e:
                    SLog.e("MemoryManager", f"Timeline upload error: {e}")
                    record["data"] = "error"
            
            # 2. 处理点击坐标: Tuple 转 List (JSON兼容)
            elif item_type == "click" and isinstance(item_data, tuple):
                record["data"] = list(item_data)

            processed_timeline[ts] = record

        # 调用服务端接口同步数据
        ServerBridge.query("sync_timeline", {"run_id": run_id, "timeline": processed_timeline})
        SLog.i("MemoryManager", f"Timeline synced: {len(processed_timeline)} records")