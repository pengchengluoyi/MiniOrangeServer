# !/usr/bin/env python
# -*-coding:utf-8 -*-
import allure
from selenium.webdriver.common.by import By

from driver.tentacle.component.template import Template
from driver.tentacle.component.router import BaseRouter

from driver.tentacle.manager import Manager


@BaseRouter.route('web/upload_file')
class UploadFile(Template):
    """
        This component will open a web browser.
    """
    def on_check(self):
        ...

    def execute(self):
        self.engine = Manager().WEBEngine
        _elem_path = self.get_param_value("Xpath")
        _file_path = self.get_param_value("file_path")
        # 等待元素存在
        self.engine.driver.find_element(By.NAME, str(_elem_path)).send_keys(_file_path)


