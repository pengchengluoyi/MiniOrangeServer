# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Pack 多根存储（app / team / builtin / learned）。

对外只用 get_store()；四根优先级与写入规则见 store.py 的模块注释。
"""
from server.services.packs.store import ROOT_RANK, PackEntry, PackStore, get_store  # noqa: F401
