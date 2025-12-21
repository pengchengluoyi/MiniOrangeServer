# !/usr/bin/env python
# -*-coding:utf-8 -*-
from selenium.webdriver.common.action_chains import ActionChains

from ability.component.router import BaseRouter

from ability.manager import Manager
from script.component.web.find_element import FindElement

TAG = "WebComponent"

@BaseRouter.route('web/offset_click')
class OffsetClick(FindElement):
    """
        This component will
    """

    def execute(self):
        super().execute()
        self.engine = Manager().WEBEngine
        _offset_x = self.get_param_value("offset_x")    # 水平偏移量
        _offset_y = self.get_param_value("offset_y")    # 水平偏移量

        action = ActionChains(self.engine.driver)
        action.move_to_element_with_offset(self.controlled_elem, _offset_x, _offset_y).click().perform()


