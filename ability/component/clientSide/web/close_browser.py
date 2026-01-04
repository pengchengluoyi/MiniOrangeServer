# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.sleep import mSleep
from ability.component.template import Template
from ability.component.router import BaseRouter

from ability.manager import Manager


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

