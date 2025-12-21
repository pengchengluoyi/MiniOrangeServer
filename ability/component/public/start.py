# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from script.sleep import mSleep
from ability.component.template import Template
from ability.component.router import BaseRouter

PID = "pid"

@BaseRouter.route('public/start')
class Start(Template):
    """
        This component will start something
    """
    META = {
        "inputs": [
            {
                "name": "test_subject",
                "type": "str",
                "desc": "应用包名 (例如 com.tencent.mm)",
                "placeholder": "com.tencent.mm",
            },
            {
                "name": "restart",
                "type": "bool",
                "desc": "是否重新启动应用/浏览器",
                "defaultValue": True,
                "trueText": "重启",      # 开启状态显示文本
                "falseText": "不重启",     # 关闭状态显示文本
                "required": False,
                "hidden": False,
                "disabled": False
            }
        ],
        "defaultData": {
            "test_subject": "",  # 对应上面的 name
            "restart": True  # 对应上面的 name
        },
        "outputVars": [
            {"key": "pid", "type": "str", "desc": "APP启动后进程ID"},
            {"key": "versionName", "type": "str", "desc": "App版本号"},
            {"key": "versionCode", "type": "str", "desc": "App版本号"},
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        test_subject = self.get_param_value("test_subject")
        restart = self.get_param_value("restart")

        if restart:
            self.engine.stop_app(test_subject)
            mSleep(3)

        pid = self.engine.start_app(test_subject)

        self.memory.set(self.info, PID, pid)
        if not pid:
            SLog.e('Start', "Failed to start the test object.")
            self.result.fail()
            self.memory.set(self.info, PID, False)
        else:
            self.result.success()
            self.memory.set(self.info, PID, pid)
        return self.result

