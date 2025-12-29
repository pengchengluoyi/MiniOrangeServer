# !/usr/bin/env python
# -*-coding:utf-8 -*-
from script.log import SLog
import ability.common.platform as platform_code
from ability.component.router import BaseRouter
from script.singleton_meta import SingletonMeta
from driver.common.task_details import TaskDetails



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
        if self.PCEngine:
            self.PCEngine.start()
        if self.WebEngine:
            self.WebEngine.start()
        if self.MobileEngine:
            self.MobileEngine.start()

    def execute_interface(self, data: dict):
        """
        API 统一调用入口，将字典转换为内部 Info 对象并执行
        """
        info = TaskDetails(case_info=data)
        self.online(info)
        return self.register_router(info, True)

    def register_router(self, info, channel=None):
        if not channel:
            return self.router.handle_request(info.nodeCode, info)
        execute_router = self.router.handle_request(info.nodeCode, info)
        result = execute_router.execute()
        return result

    def apply_engine(self, info):
        SLog.d('apply_engine', f"{info}")

        if info.platform in platform_code.MMOBILE:
            if info.platform == platform_code.IOS:
                from ability.engine.mobile.mIOS import IOSEngine
                self.MobileEngine = IOSEngine()
            else:
                from ability.engine.mobile.mAndroid import AndroidEngine
                self.MobileEngine = AndroidEngine()
        if info.platform in platform_code.MWEB:
            from ability.engine.web.mChrome import ChromeEngine
            self.WebEngine = ChromeEngine()
        if info.platform in platform_code.MPC:
            if info.platform == platform_code.MACOS:
                from ability.engine.pc.mMac import MacEngine
                self.PCEngine = MacEngine()
            elif info.platform == platform_code.WINDOWS:
                from ability.engine.pc.mWindows import WindowsEngine
                self.PCEngine = WindowsEngine()

        return True

    def offline(self):
        if self.PCEngine: self.PCEngine.end()
        if self.WebEngine: self.WebEngine.end()
        if self.MobileEngine: self.MobileEngine.end()
