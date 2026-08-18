# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""iOS 链式定位（后端无关：元素查找与外框都经 engine 转发）"""
import time


class IOSLocator:
    def __init__(self, engine, locator_chain):
        self._engine = engine
        self._locator_chain = locator_chain
        self._element = None

    def _resolve(self):
        if self._element is None:
            self._element = self._engine.find_element(self._locator_chain)
        return self._element

    def exists(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._element = self._engine.find_element(self._locator_chain)
            if self._element is not None:
                return True
            time.sleep(0.5)
        return False

    def set_text(self, text):
        self._engine.send_keys(self._resolve(), text)

    def click(self):
        self._resolve().click()

    @property
    def info(self):
        left, top, right, bottom = self._engine.element_bounds(self._resolve())
        return {
            "bounds": {
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
            }
        }
