# !/usr/bin/env python
# -*-coding:utf-8 -*-

from ability.core.step_result import StepResult
from ability.component.template import Template
from ability.component.router import BaseRouter


import script.constPath.component_code as MCode

@BaseRouter.route('mobile/find')
class Find(Template):
    """
        This component will close web browser.
    """

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        mLocatorChain = self.get_param_value("locator_chain")

        self.engine.driver.screen_on()
        if mLocatorChain:
            build_chain = self.engine.build_chain(mLocatorChain)
            mResult = {
                "elements": build_chain
            }
            self.cache_create(MCode.C_M_LIST_ELEMENTS, mResult)

            return StepResult(MCode.MTYPE_NORMAL, MCode.MN_WITHRETURNED, mResult)
        return None


