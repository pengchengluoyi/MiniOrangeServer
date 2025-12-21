# !/usr/bin/env python
# -*-coding:utf-8 -*-
import time

import script.constPath.component_code as m_code
from ability.component.template import Template
from ability.component.router import BaseRouter




@BaseRouter.route('mobile/recent_app')
class RecentApp(Template):
    """
        This component will close web browser.
    """

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        mEvent = self.get_param_value("event")
        if mEvent == m_code.RECENT_OPEN:
            self.engine.driver.press("recent")
        elif mEvent == m_code.RECENT_CLOSE:
            ...
        elif mEvent == m_code.RECENT_SWITCH:
            mPackage_name = self.get_param_value("package_name")
            if not self.open_recent_apps():
                # return False
                ...


    def open_recent_apps(self):
        """
        打开最近任务列表（多任务视图）
        :return: 是否成功打开
        """
        try:
            # 方法1: 使用按键事件打开最近任务
            self.get_engine()
            self.engine.press("recent")
            time.sleep(2)  # 等待最近任务列表加载
            return True
        except Exception as e:
            print(f"打开最近任务列表失败: {str(e)}")
            return False


