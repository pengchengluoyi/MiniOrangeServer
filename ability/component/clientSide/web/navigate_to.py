# !/usr/bin/env python
# -*-coding:utf-8 -*-
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException


from script.log import SLog
from script.sleep import mSleep
from ability.component.template import Template
from ability.component.router import BaseRouter

from ability.manager import Manager
TAG = "WebComponent"

@BaseRouter.route('web/navigate_to')
class RouterNavigator(Template):
    """
    Method to navigate to a specific path.

    Args:
        path (str): The relative path to navigate to.
    """
    controlled_elem = None
    current_page = None

    def on_check(self):
        ...

    @staticmethod
    def _wait_for_page_to_load():
        """
        Internal method to wait for the page to load.
        This is a placeholder and should be implemented based on the actual page.
        """
        # Example: wait for an element with id 'content' to be present
        self.engine = Manager().WEBEngine
        WebDriverWait(_engine.driver, 10).until(
            EC.presence_of_element_located((By.ID, "content"))
        )

    def _wait_for_page_to_ready(self, timeout=300):
        """
        Wait for the page to be fully loaded by checking document.readyState.
        """
        self.engine = Manager().WEBEngine
        start_time = time.time()

        while True:
            SLog.d(TAG, ":{0}:, :{1}:".format(self.get_current_path(), self.get_param_value("current_route")))
            if self.get_current_path() == self.get_param_value("current_route"):
                SLog.i(TAG, "page is ready")
                break
            if time.time() - start_time > timeout:
                raise TimeoutException()
            mSleep(2)

    @staticmethod
    def get_current_path():
        """
        Method to get the current path of the URL using current_url property.

        Returns:
            str: The current path of the URL.
        """
        self.engine = Manager().WEBEngine
        current_url = _engine.driver.current_url
        url_parts = current_url.split('/')
        current_path = '/' + '/'.join(url_parts[3:])  # Assuming the first three parts are scheme and domain
        SLog.i(TAG, "current path is %s" % current_path)
        return current_path

    def execute(self):
        self.engine = Manager().WEBEngine
        _router_path = self.get_param_value("navigate_to_route")

        self._wait_for_page_to_ready()
        _engine.driver.execute_script(f"window.history.pushState({{}}, '', '{_router_path}');")

        # This step is very important. This step triggers a popstate event to inform the framework that the route has changed.
        _engine.driver.execute_script(f"window.dispatchEvent(new PopStateEvent('popstate', {{}}));")
        self.get_current_path()





