# !/usr/bin/env python
# -*-coding:utf-8 -*-


from ability.component.template import Template
from ability.component.router import BaseRouter

 
from ability.manager import Manager

TAG = "WebComponent"

@BaseRouter.route('web/get_local_storage')
class GetLocalStorage(Template):
    """
        This component will
    """
    controlled_elem = None

    def on_check(self):
        ...

    def execute(self):
        self.engine = Manager().WEBEngine

        token_script = _engine.driver.execute_script("""
            let items = {}; 
            for (let i = 0; i < localStorage.length; i++) {
                let key = localStorage.key(i);
                items[key] = localStorage.getItem(key);
            }
            return items;
        """)
        component_cache = CCache()
        component_cache.create("web_local_storage", token_script)

