# driver/tentacle/manager.py
# !/usr/bin/env python
# -*-coding:utf-8 -*-
import driver.tentacle.common.platform as platform_code
from driver.tentacle.component.router import BaseRouter
from script.singleton_meta import SingletonMeta
from driver.brain.common.task_details import TaskDetails


class TaskInfo:
    def __init__(self, **kwargs):
        self.platform = None
        self.nodeCode = None
        self.__dict__.update(kwargs)


class Manager(metaclass=SingletonMeta):
    def __init__(self):
        self.router = BaseRouter()
        self.PCEngine = None
        self.WebEngine = None
        self.MobileEngine = None

    def online(self, info):
        self.apply_engine(info)
        if self.PCEngine: self.PCEngine.start()
        if self.WebEngine: self.WebEngine.start()
        if self.MobileEngine: self.MobileEngine.start()

    def execute_interface(self, data: dict):
        info = TaskDetails(case_info=data)
        self.online(info)
        return self.register_router(info, True)

    def register_router(self, info, channel=None):
        # 1. 获取执行组件
        execute_router = self.router.handle_request(info.nodeCode, info)

        if not channel:
            return execute_router

        # 🔥🔥🔥 [修复] 防御空指针：如果找不到路由，直接忽略或报错 🔥🔥🔥
        if not execute_router:
            # 对于 unknown 节点 (如开始节点)，静默跳过是最好的选择
            print(f"[Manager] Warning: No router found for nodeCode '{info.nodeCode}', skipping.")
            return None

        # 2. 执行组件逻辑
        try:
            result = execute_router.execute()
        except TypeError as e:
            if 'stat: path should be string' in str(e) and 'NoneType' in str(e):
                raise Exception("No device connected or device not responding")
            raise e
        return result

    def apply_engine(self, info):
        if info.platform in platform_code.MMOBILE:
            if self.MobileEngine: return True
            if info.platform == platform_code.IOS:
                from driver.tentacle.engine.mobile.mIOS import IOSEngine
                self.MobileEngine = IOSEngine()
            else:
                from driver.tentacle.engine.mobile.mAdb import MAdbEngine
                self.MobileEngine = MAdbEngine()

        elif info.platform in platform_code.MWEB:
            if self.WebEngine: return True
            from driver.tentacle.engine.web.mChrome import ChromeEngine
            self.WebEngine = ChromeEngine()

        elif info.platform in platform_code.MPC:
            if self.PCEngine: return True
            if info.platform == platform_code.MACOS:
                from driver.tentacle.engine.pc.mMac import MacEngine
                self.PCEngine = MacEngine()
            elif info.platform == platform_code.WINDOWS:
                from driver.tentacle.engine.pc.mWindows import WindowsEngine
                self.PCEngine = WindowsEngine()
        return True

    def offline(self):
        if self.PCEngine: self.PCEngine.end()
        if self.WebEngine: self.WebEngine.end()
        if self.MobileEngine: self.MobileEngine.end()