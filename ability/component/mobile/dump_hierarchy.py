# !/usr/bin/env python
# -*-coding:utf-8 -*-

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter



@BaseRouter.route('mobile/dump_hierarchy')
class DumpHierarchy(Template):
    """
        This component will close web browser.
    """

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        xml = self.engine.driver.dump_hierarchy(compressed=False, pretty=False, max_depth=50)
        self.memory.set(self.info, "dom", xml)

