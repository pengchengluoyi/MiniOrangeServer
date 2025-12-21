# !/usr/bin/env python
# -*-coding:utf-8 -*-
import importlib
import copy
from script.log import SLog
# 假设 BaseRouter 和 MAP 在这些路径，请根据实际情况调整导入
from ability.component.router import BaseRouter
from ability.component.map import MAP

TAG = "MapLoader"


def scan():
    """
    遍历 MAP，动态加载组件，读取 META 信息并填充回 MAP
    """
    # 1. 为了不污染原始导入的 MAP，建议深拷贝一份（如果需要直接修改原对象可省略此步）
    full_map = copy.deepcopy(MAP)

    # 2. 遍历 MAP 的每一层
    for category_key, category_data in full_map.items():
        details = category_data.get("details", {})

        for action_key, action_config in details.items():
            address = action_config.get("address")

            if not address:
                SLog.w(TAG, f"Key [{action_key}] has no address, skipping.")
                continue

            # 3. 动态导入模块
            # address 格式如 "public/start" -> 转换为 "public.start"
            # 最终拼接为 ability.component.public.start
            module_sub_path = address.strip('/').replace('/', '.')
            module_pkg = f'ability.component.{module_sub_path}'

            try:
                # 这一步非常关键：
                # 只有 import 了模块，模块内的 @BaseRouter.route 装饰器才会执行，
                # 类才会注册到 BaseRouter.routes 字典中。
                importlib.import_module(module_pkg)
            except ModuleNotFoundError:
                SLog.e(TAG, f"Module not found: {module_pkg}")
                continue
            except Exception as e:
                SLog.e(TAG, f"Error importing {module_pkg}: {e}")
                continue

            # 4. 从 BaseRouter 获取注册的类
            # 注意：BaseRouter.routes 的 key 就是 address (例如 'public/start')
            component_class = BaseRouter.routes.get(address)

            if not component_class:
                SLog.e(TAG, f"Address [{address}] imported but not registered in BaseRouter. Check @route decorator.")
                continue

            # 5. 获取 META 属性
            meta = getattr(component_class, "META", None)

            if meta:
                # 6. 将 META 中的指定字段写入 action_config
                action_config["inputs"] = meta.get("inputs", [])
                action_config["defaultData"] = meta.get("defaultData", {})
                action_config["outputVars"] = meta.get("outputVars", [])

                # SLog.i(TAG, f"Enriched metadata for {address}")
            else:
                SLog.w(TAG, f"Component {address} has no META attribute.")
                # 如果没有 META，可以设置默认空值，防止前端报错
                action_config["inputs"] = []
                action_config["defaultData"] = {}
                action_config["outputVars"] = []

    return full_map

