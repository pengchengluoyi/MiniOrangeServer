# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.sleep import mSleep
from driver.tentacle.component.template import Template
from driver.tentacle.component.router import BaseRouter

from driver.tentacle.manager import Manager


@BaseRouter.route('web/close_browser')
class CloseBrowser(Template):
    """
        This component will close web browser.
    """

    def on_check(self):
        ...

    def execute(self):
        self.engine = Manager().WEBEngine
        mSleep(5)
        _engine.end()

