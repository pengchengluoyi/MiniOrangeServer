# !/usr/bin/env python
# -*-coding:utf-8 -*-
import os
from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter



@BaseRouter.route('win/dump_hierarchy')
class WinDumpHierarchy(Template):
    """
        This component will
    """
    META = {
        "inputs": [
            {
                "name": "package",
                "type": "str",
                "desc": "窗口标题 (Window Title)",
                "placeholder": "chrome.exe",
            },
        ],
        "defaultData": {
            "package": ""
        },
        "outputVars": [
            {"key": "dom", "type": "str", "desc": "win dom树"}
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        package = self.get_param_value("package")
        package_name = None
        if package:
            package_name = os.path.basename(package)
        else:
            package_name = os.path.basename(self.package_path)
        if package_name:
            self.engine.driver.connect(path=package_name)
            window = self.engine.driver.top_window()

            # 3. 打印控件树
            window.print_control_identifiers(filename="control_log.txt")

            # 2. 读取文件内容到变量
            with open("control_log.txt", "r", encoding="utf-8") as file:
                xml = file.read()
                self.memory.set(self.info, "dom", xml)
        self.result.success()
        return self.result

