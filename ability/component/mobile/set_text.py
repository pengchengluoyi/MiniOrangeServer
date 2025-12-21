# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.sleep import mSleep
from ability.component.template import Template
from ability.component.router import BaseRouter


@BaseRouter.route('mobile/set_text')
class SetText(Template):
    """
        This component will close web browser.
    """

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        mLocatorChain = self.get_param_value("locator_chain")
        mContent = self.get_param_value("content")

        self.engine.driver.screen_on()
        if mLocatorChain:
            build_chain = self.engine.build_chain(mLocatorChain)
            # self.wait_display
            bounds = build_chain.info['bounds']
            center_x = (bounds['left'] + bounds['right']) // 2
            center_y = (bounds['top'] + bounds['bottom']) // 2
            self.engine.driver.click(center_x, center_y)
            build_chain.set_text(mContent)
            mSleep(1)
            return None
        return None
