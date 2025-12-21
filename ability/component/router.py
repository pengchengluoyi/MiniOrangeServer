# !/usr/bin/env python
# -*-coding:utf-8 -*-
import importlib
from script.log import SLog
from ability.component.map import MAP

TAG: str = "BaseRouter"


class BaseRouter:
    # 存储 路径 -> 函数/类 的映射
    routes = {}

    @classmethod
    def route(cls, path):
        """
        装饰器：用于在组件文件中注册路由
        path: 这里的 path 应该对应 MAP 中 address 的值 (例如 'web/open_url')
        """

        def decorator(func):
            cls.routes[path] = func
            return func

        return decorator

    @classmethod
    def _default_handler(cls, path, *args, **kwargs):
        SLog.e(TAG, f"404 Not Found: No route matches {path}")
        return None

    @classmethod
    def _resolve_uri_to_address(cls, uri: str) -> str:
        """
        核心逻辑：将外部 URI 映射为内部 address
        例如: 'web/open-url' -> 'web/open_url'
        """
        if not uri:
            return None

        parts = uri.strip('/').split('/')
        if len(parts) < 2:
            SLog.e(TAG, f"Invalid URI format: {uri}")
            return None

        # 1. 获取分类 (例如 web) 和 动作 (例如 open-url)
        category_key = parts[0]
        action_key = parts[1]

        # 2. 在 MAP 中查找 Category (忽略大小写)
        # 你的 Map 里有 "public" (小写) 也有 "WEB" (大写)，所以需要做一下容错匹配
        target_category = None
        if category_key in MAP:
            target_category = MAP[category_key]
        elif category_key.upper() in MAP:
            target_category = MAP[category_key.upper()]
        elif category_key.lower() in MAP:
            target_category = MAP[category_key.lower()]

        if not target_category:
            SLog.e(TAG, f"Category not found in MAP: {category_key}")
            return None

        # 3. 在 details 中查找 Action
        details = target_category.get("details", {})
        config = details.get(action_key)

        if not config or "address" not in config:
            SLog.e(TAG, f"Action not found in MAP: {uri}")
            return None

        # 4. 返回真实的内部地址 (例如 'web/open_url')
        return config["address"]

    @classmethod
    def handle_request(cls, uri, *args):
        """
        uri: 调用方传入的字符串，例如 "web/open-url"
        """
        # --- 步骤 1: 查表获取真实地址 ---
        real_address = cls._resolve_uri_to_address(uri)

        if not real_address:
            # 如果映射表中没找到，是否尝试直接使用 uri 作为地址？
            # 视你的需求而定，这里默认走 404
            return cls._default_handler(uri, *args)

        # --- 步骤 2: 动态加载模块 ---
        # real_address 是 "web/open_url"，转换为 "web.open_url" 以供 import 使用
        module_path = real_address.strip('/').replace('/', '.')

        if not module_path:
            return cls._default_handler(uri, *args)

        try:
            # 避免重复导入 (可选优化：sys.modules检查)
            # SLog.i(TAG, f'Loading module: ability.component.{module_path}')
            importlib.import_module(f'ability.component.{module_path}')
        except ModuleNotFoundError as e:
            SLog.e(TAG, f'ModuleNotFoundError: ability.component.{module_path} | Error: {e}')
            pass
        except Exception as e:
            SLog.e(TAG, f'Error importing module {module_path}: {e}')

        # --- 步骤 3: 获取并执行路由 ---
        # 注意：这里是用 real_address ("web/open_url") 去 routes 字典里查
        handler = cls.routes.get(real_address, cls._default_handler)

        if isinstance(handler, type):
            instance = handler(*args)
            return instance

        return handler(real_address, *args)