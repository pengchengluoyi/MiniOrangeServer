# memory/long_term.py
import threading
from driver.agent.Common.ws import WS
from script.log import SLog


class LongTermMemory:
    def __init__(self):
        self.world_model = {}  # 生存法则、系统配置
        self.app_graph = {}  # 任务流程图
        self.is_loaded = False

    def load_async(self, flow_id=None):
        """启动后台线程加载数据"""
        t = threading.Thread(target=self._fetch_bg, args=(flow_id,))
        t.daemon = True
        t.start()

    def _fetch_bg(self, flow_id):
        try:
            # 加载世界模型
            wm = WS.fetch_world_model()
            if wm:
                self.world_model = wm.get("data", {})

            # 加载应用图谱
            if flow_id:
                ag = WS.fetch_app_graph(flow_id)
                if ag:
                    self.app_graph = ag.get("data", {})

            self.is_loaded = True
            SLog.i("Memory", "✅ 长期记忆(配置/图谱) 加载完毕")
        except Exception as e:
            SLog.w("Memory", f"❌ 长期记忆加载失败: {e}")

    def get_config(self, key):
        """获取配置"""
        return self.world_model.get(key)