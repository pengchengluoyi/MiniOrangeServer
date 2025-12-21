# !/usr/bin/env python
# -*-coding:utf-8 -*-

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter
from ability.core.exeception import MException
from script.constPath.error_code import ErrorCode

from ability.manager import Manager
TAG = "WebComponent"

@BaseRouter.route('web/find_element')
class FindElement(Template):
    """
        This component will
    """
    controlled_elem = None

    def on_check(self):
        ...

    def _wait_for_page_to_show(self):
        """
        Internal method to wait for the page to load.
        This is a placeholder and should be implemented based on the actual page.
        """
        # Example: wait for an element with id 'content' to be present
        self.engine = Manager().WEBEngine
        _elem_path = self.get_param_value("Xpath")
        try:
            WebDriverWait(_engine.driver, 10).until(
                EC.presence_of_element_located((By.XPATH, str(_elem_path))),
            )
            SLog.d(TAG, "The elements you want to operate have been displayed normally.")
        except TimeoutException:
            try:
                raise MException(ErrorCode.COMPONENT_ERROR_FOUND_ELEMENT_TIMEOUT)
            except MException as e:
                SLog.e(TAG, e)

    def execute(self):
        self.engine = Manager().WEBEngine
        _elem_path = self.get_param_value("Xpath")
        self._wait_for_page_to_show()

        # 等待元素存在
        self.controlled_elem = _engine.driver.find_element(By.XPATH, str(_elem_path))


