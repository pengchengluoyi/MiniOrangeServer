# !/usr/bin/env python
# -*-coding:utf-8 -*-

from ability.component.router import BaseRouter

from script.component.web.find_element import FindElement

TAG = "WebComponent"

@BaseRouter.route('web/find_elem_and_click')
class FindElemAndClick(FindElement):
    """
        This component will
    """

    def execute(self):
        super().execute()
        self.controlled_elem.click()


