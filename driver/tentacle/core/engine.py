# !/usr/bin/env python
# -*-coding:utf-8 -*-
from script.singleton_meta import SingletonMeta
TAG = "BaseEngine"


class BaseEngine(metaclass=SingletonMeta):
    def __init__(self):
        self.driver = None

    def init_driver(self):
        ...

    def start(self):
        if not self.driver:
            self.init_driver()

    def start_app(self):
        ...

    def stop_app(self):
        ...

    def build_chain(self):
        ...

    def end(self):
        ...