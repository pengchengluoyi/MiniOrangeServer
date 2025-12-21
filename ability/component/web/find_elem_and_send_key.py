# !/usr/bin/env python
# -*-coding:utf-8 -*-

from ability.component.router import BaseRouter

from script.component.web.find_element import FindElement
from script.sleep import mSleep

TAG = "WebComponent"

@BaseRouter.route('web/find_elem_and_send_key')
class FindElemAndSendKey(FindElement):
    """
        This component will
    """

    def execute(self):
        super().execute()
        _context = self.get_param_value("context")
        self.controlled_elem.send_keys(str(_context))
        mSleep(0.4)


