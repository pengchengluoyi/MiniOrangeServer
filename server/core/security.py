import os
import json
import tempfile
import threading
import uuid
from script.log import SLog
from server.core.database import APP_DATA_DIR

TAG = "SecurityManager"
# 🔥 [修改] 路径指向 config.json，实现 Server/Client 配置文件统一
CONFIG_PATH = os.path.join(APP_DATA_DIR, "config.json")


class SecurityManager:
    _config = {}
    _lock = threading.RLock()  # 🔥 [Fix] 引入可重入锁，防止并发读写导致配置回滚

    @classmethod
    def load(cls):
        with cls._lock:
            SLog.d(TAG, f"🔒 [Load] Acquiring lock. Config before load: {cls._config.get('access_token')}")
            # 1. 读取配置
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        new_data = json.load(f)
                        # 🔥 [Fix] 原地更新字典，保持引用一致性，防止内存状态分裂
                        cls._config.clear()
                        cls._config.update(new_data)
                    print(f"🔒 [Load] File loaded. Token: {cls._config.get('access_token')}")
                except Exception:
                    cls._config.clear()
                    print("🔒 [Load] Error loading config file, reset to empty.")
            else:
                print("🔒 [Load] Config file does not exist, config is empty.")
            # SLog.d(TAG, "🔒 [Load] Releasing lock.")

    @classmethod
    def save(cls):
        with cls._lock:
            SLog.d(TAG, f"💾 [Save] Acquiring lock. Config before merge: {cls._config.get('access_token')}")
            # 更稳健的写法是先读再合并：
            current_data = {}
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        current_data = json.load(f)
                    # SLog.d(TAG, f"💾 [Save] Read existing file for merge. Disk token: {current_data.get('access_token')}")
                except:
                    pass

            # 合并内存中的配置到文件配置中
            current_data.update(cls._config)
            
            # 🔥 [Fix] 移除值为 None 的键，确保从 JSON 文件中物理删除
            current_data = {k: v for k, v in current_data.items() if v is not None}
            
            print(f"💾 [Save] Merged data to write (access_token): {current_data.get('access_token')}")
            cls._write_atomic(current_data)
            # SLog.d(TAG, "💾 [Save] Releasing lock.")

    @classmethod
    def save_force(cls):
        """强制将当前内存状态同步到磁盘，不合并旧文件"""
        with cls._lock:
            SLog.d(TAG, f"💪 [SaveForce] Acquiring lock. Data to write (access_token): {cls._config.get('access_token')}")
            # 过滤掉 None 的值
            data_to_save = {k: v for k, v in cls._config.items() if v is not None}
            cls._write_atomic(data_to_save)
            print(f"💪 [SaveForce] Written to disk. Token should be GONE.")

    @classmethod
    def _write_atomic(cls, data):
        """内部原子写入辅助函数 (需在锁内调用)"""
        dir_name = os.path.dirname(CONFIG_PATH)
        os.makedirs(dir_name, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tmp_file:
            json.dump(data, tmp_file, indent=4)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
            os.replace(tmp_file.name, CONFIG_PATH)
            print(f"✍️ [AtomicWrite] File replaced. Written token: {data.get('access_token')}")

    @classmethod
    def get_token(cls):
        with cls._lock:
            token = cls._config.get("access_token")
            # SLog.d(TAG, f"🔑 [GetToken] Returning token: {token}")
            return token

    @classmethod
    def get_external_url(cls):
        with cls._lock:
            SLog.d(TAG, f"🌐 [GetExtURL] Acquiring lock. Config before check: {cls._config.get('access_token')}")
            if not cls._config:
                SLog.d(TAG, "🌐 [GetExtURL] _config is empty, calling load().")
                cls.load()
            url = cls._config.get("external_url")
            SLog.d(TAG, f"🌐 [GetExtURL] Returning URL: {url}. Config token: {cls._config.get('access_token')}")
            return url

    @classmethod
    def set_external_url(cls, url):
        with cls._lock:
            SLog.d(TAG, f"✏️ [SetExtURL] Acquiring lock. Config before set: {cls._config.get('access_token')}")
            cls.load()  # 先加载防止覆盖
            cls._config["external_url"] = url
            cls.save()
            SLog.d(TAG, f"✏️ [SetExtURL] Config after set: {cls._config.get('access_token')}")

    @classmethod
    def clear_cluster_config(cls):
        with cls._lock:
            print(f"🧹 [Clear] Acquiring lock. Config before clear: {cls._config.get('access_token')}")

            keys_to_clear = ["target_url", "candidate_urls", "access_token"]

            # 🔥 [核心修复] 不要用 pop！要设为 None！
            for k in keys_to_clear:
                cls._config[k] = None

            # 立即强制刷盘
            cls.save_force()
            print(f"🧹 [Clear] After save_force(). Config now: {cls._config.get('access_token')}")
            # SLog.i(TAG, "🧹 [Clear] Releasing lock.")