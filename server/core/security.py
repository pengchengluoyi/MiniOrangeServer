import os
import json
import uuid
from server.core.database import APP_DATA_DIR

CONFIG_PATH = os.path.join(APP_DATA_DIR, "server_config.json")

class SecurityManager:
    _config = {}

    @classmethod
    def load(cls):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cls._config = json.load(f)
            except Exception:
                cls._config = {}
        
        save_needed = False
        # 如果没有 Token，自动生成一个强 Token
        if "access_token" not in cls._config:
            cls._config["access_token"] = uuid.uuid4().hex
            save_needed = True
            
        if save_needed:
            cls.save()

    @classmethod
    def save(cls):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cls._config, f, indent=4)

    @classmethod
    def get_token(cls):
        if not cls._config: cls.load()
        return cls._config.get("access_token")

    @classmethod
    def get_external_url(cls):
        if not cls._config: cls.load()
        return cls._config.get("external_url")

    @classmethod
    def set_external_url(cls, url):
        cls._config["external_url"] = url
        cls.save()