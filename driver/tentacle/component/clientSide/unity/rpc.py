# !/usr/bin/env python
# -*-coding:utf-8 -*-
import requests

from driver.tentacle.component.template import Template
from driver.tentacle.component.router import BaseRouter


@BaseRouter.route('unity/rpc')
class CloseBrowser(Template):
    """
        This component will close web browser.
    """
    base = "http://127.0.0.1:95271"

    def on_check(self):
        ...

    def execute(self):
        res = requests.get(f"{self.base}/ui_tree")
        res.raise_for_status()
        return res.json()

