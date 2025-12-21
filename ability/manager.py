# !/usr/bin/env python
# -*-coding:utf-8 -*-
from script.log import SLog
import ability.common.platform as platform_code
from ability.component.router import BaseRouter
from script.singleton_meta import SingletonMeta



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


    def register_router(self, info):
        return self.router.handle_request(info.nodeCode, info)

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
