# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from script.sleep import mSleep
from ability.component.template import Template
from ability.component.router import BaseRouter


@BaseRouter.route('mobile/find_list_and_click')
class Flac(Template):
    """
        This component will close web browser.
    """

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        mListLocatorChain = self.get_param_value("list_locator_chain")
        mClickLocatorChain = self.get_param_value("click_locator_chain")
        mTClickLocatorChain = self.get_param_value("tClick_locator_chain")
        mInputLocatorChain = self.get_param_value("input_locator_chain")
        mIsSwipe = self.get_param_value("is_swipe")
        mIndex = self.get_param_value("index")

        self.engine.driver.screen_on()

        for i in range(0, mIndex):
            if mListLocatorChain:
                list_elements = self.engine.driver(resourceId=mListLocatorChain[0]["resourceId"])
                SLog.i("flac", "找到了{}个".format(list_elements.count))
                if list_elements.exists:
                    for locator_chain in list_elements:
                        click_current, input_current = "", ""
                        for clickCondition in mClickLocatorChain:
                            locator_params = {k: v for k, v in clickCondition.items() if k != 'index'}
                            index = clickCondition.get('index', 0)


                            click_current = locator_chain.child(**locator_params)[index]
                        click_current.click()
                        input_current = self.engine.build_chain(mInputLocatorChain["locator_chain"])
                        for i in range(0, 3):
                            if input_current.exists:
                                bounds = input_current.info['bounds']
                                center_x = (bounds['left'] + bounds['right']) // 2
                                center_y = (bounds['top'] + bounds['bottom']) // 2
                                self.engine.driver.click(center_x, center_y)
                                input_current.set_text(mInputLocatorChain["text"])
                            else:
                                mSleep(0.5)
                                SLog.i("123", "123")


                        tClick_current = self.engine.build_chain(mTClickLocatorChain)
                        tClick_current.click()

                        SLog.i("flac - locator_chain: ", locator_chain)

                    if mIsSwipe:
                        self.engine.driver.swipe(mIsSwipe[0], mIsSwipe[1], mIsSwipe[2], mIsSwipe[3], mIsSwipe[4])
                    mSleep(0.5)

        return None
