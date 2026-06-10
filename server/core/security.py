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
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        new_data = json.load(f)
                        cls._config.clear()
                        cls._config.update(new_data)
                except Exception:
                    cls._config.clear()
                    SLog.w(TAG, "config load failed, reset to empty")

    @classmethod
    def save(cls):
        with cls._lock:
            current_data = {}
            if os.path.exists(CONFIG_PATH):
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        current_data = json.load(f)
                except Exception:
                    pass

            current_data.update(cls._config)
            current_data = {k: v for k, v in current_data.items() if v is not None}
            cls._write_atomic(current_data)

    @classmethod
    def save_force(cls):
        """强制将当前内存状态同步到磁盘，不合并旧文件"""
        with cls._lock:
            data_to_save = {k: v for k, v in cls._config.items() if v is not None}
            cls._write_atomic(data_to_save)

    @classmethod
    def _write_atomic(cls, data):
        """
        跨平台原子写入实现 (需在锁内调用)
        兼容: Windows, Linux, macOS
        """
        dir_name = os.path.dirname(CONFIG_PATH)
        os.makedirs(dir_name, exist_ok=True)

        tmp_name = None
        try:
            # 1. 创建并写入临时文件
            # delete=False 是必须的：因为我们要手动关闭它，然后手动移动它。
            # 如果是 True，with 结束时 Python 会自动尝试删除，导致我们没法移动。
            with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False, encoding="utf-8") as tmp_file:
                json.dump(data, tmp_file, indent=4)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())  # 确保数据强制刷入磁盘硬件
                tmp_name = tmp_file.name

            # 🔥【关键点】🔥
            # 此时代码已退出 with 块，文件句柄 (File Handle) 已经自动 Close。
            # 只有文件关闭后，Windows 才允许执行下面的 replace 操作。

            # 2. 原子替换
            # os.replace 在 Linux 是原子的。
            # 在 Windows (Python 3.3+) 上，它会调用 MoveFileEx 覆盖目标，也是原子的。
            os.replace(tmp_name, CONFIG_PATH)

        except Exception as e:
            SLog.e(TAG, f"config atomic write failed: {e}")
            # 3. 错误清理机制
            # 如果上面的步骤失败了（比如权限不够），必须把残留的 tmp 文件删掉
            # 否则你的硬盘会被 tmp 文件填满。
            if tmp_name and os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass
            # 继续抛出异常，让上层知道保存失败
            raise e

    @classmethod
    def get_token(cls):
        with cls._lock:
            token = cls._config.get("access_token")
            # SLog.d(TAG, f"🔑 [GetToken] Returning token: {token}")
            return token

    @classmethod
    def get_external_url(cls):
        with cls._lock:
            if not cls._config:
                cls.load()
            return cls._config.get("external_url")

    @classmethod
    def set_external_url(cls, url):
        with cls._lock:
            cls.load()
            cls._config["external_url"] = url
            cls.save()

    @classmethod
    def clear_cluster_config(cls):
        with cls._lock:
            keys_to_clear = ["target_url", "candidate_urls", "access_token"]
            for k in keys_to_clear:
                cls._config[k] = None
            cls.save_force()
            SLog.i(TAG, "cluster config cleared")