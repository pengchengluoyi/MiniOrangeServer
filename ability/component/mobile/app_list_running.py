# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter



@BaseRouter.route('mobile/app_list_running')
class AppListRunning(Template):
    """
        This component will close web browser.
    """

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        if not self.engine.driver:
            self.engine.start()
        result = self.engine.driver.app_list_running()
        SLog.i("AppListRunning", result)
        self.memory.set(self.info, True)

