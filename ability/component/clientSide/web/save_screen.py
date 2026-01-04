# !/usr/bin/env python
# -*-coding:utf-8 -*-
import allure

from ability.component.template import Template
from ability.component.router import BaseRouter

from ability.manager import Manager
TAG = "WebComponent"

@BaseRouter.route('web/save_screen')
class SaveScreen(Template):
    """
        This component will
    """

    def on_check(self):
        ...

    def execute(self):
        self.engine = Manager().WEBEngine
        _engine.driver.save_screenshot('screenshot.png')
        allure.attach("这是一个tupian附件", name="示例文本", attachment_type=allure.attachment_type.TEXT)
        allure.attach.file("screenshot.png", name='screenshot.png', attachment_type=allure.attachment_type.PNG)




