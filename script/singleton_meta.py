# script/singleton_meta.py
# !/usr/bin/env python
# -*-coding:utf-8 -*-

import threading


# class SingletonMeta(type):
#     _instances = {}
#
#     def __call__(cls, *args, **kwargs):
#         if cls not in cls._instances:
#             instance = super().__call__(*args, **kwargs)
#             cls._instances[cls] = instance
#         return cls._instances[cls]
#

class SingletonMeta(type):
    _instances = {}
    _lock = threading.Lock() # 增加线程锁

    def __call__(cls, *args, **kwargs):
        # 双重检查锁定 (Double-Checked Locking)
        if cls not in cls._instances:
            with cls._lock:
                if cls not in cls._instances:
                    instance = super().__call__(*args, **kwargs)
                    cls._instances[cls] = instance
        return cls._instances[cls]