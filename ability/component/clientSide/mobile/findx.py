# !/usr/bin/env python
# -*-coding:utf-8 -*-

from ability.component.template import Template
from ability.component.router import BaseRouter


from lxml import etree

@BaseRouter.route('mobile/findx')
class Findx(Template):
    """
        This component will close web browser.
    """

    def on_check(self):
        ...

    def execute(self):
        self.get_engine()
        mLocatorChain = self.get_param_value("locator_chain")

        # 获取界面层次结构
        xml_content = self.engine.driver.dump_hierarchy()
        root = etree.fromstring(xml_content.encode('utf-8'))

        # 使用 XPath 查找 description 包含 "123" 的元素
        xpath_expr = f'//node[contains(@content-desc, {mLocatorChain[0]['content-desc']})]'
        matching_nodes = root.xpath(xpath_expr)

        # 将节点转换为可操作的元素
        for node in matching_nodes:
            bounds = node.get('bounds')
            if bounds:
                # 解析坐标并点击
                coords = bounds.replace('[', '').replace(']', ',').split(',')
                if len(coords) >= 4:
                    x1, y1, x2, y2 = map(int, coords[:4])
                    center_x = (x1 + x2) // 2
                    center_y = (y1 + y2) // 2
                    self.engine.driver.click(center_x, center_y)
            break
