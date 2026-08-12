# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""ClawNode RemoteEngine 判定（回归 / 对话 / 定位共用）。"""


def is_clawnode_remote_engine(engine) -> bool:
    return type(engine).__name__ == "RemoteEngine"
