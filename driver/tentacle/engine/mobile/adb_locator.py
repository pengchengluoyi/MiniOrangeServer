# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Android 链式定位（与 IOSLocator 接口一致）"""
import time


class AdbLocator:
    def __init__(self, engine, locator_chain):
        self._engine = engine
        self._locator_chain = locator_chain

    def exists(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._engine.find_element(self._locator_chain):
                return True
            time.sleep(0.5)
        return False

    def set_text(self, text):
        pos = self._engine.find_element(self._locator_chain)
        if pos:
            self._engine.send_keys(pos, text)

    @property
    def info(self):
        pos = self._engine.find_element(self._locator_chain)
        if not pos:
            raise ValueError("Element not found")
        x, y = pos
        return {
            "bounds": {
                "left": x,
                "top": y,
                "right": x,
                "bottom": y,
            }
        }
