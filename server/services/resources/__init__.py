# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""测试资源：租账号、取口令、备会话。不进业务 capability_menu。"""
from server.services.resources.catalog import list_resource_skills
from server.services.resources.gateway import resolve_secret
from server.services.resources.lease import lease_account, release_account

__all__ = [
    "list_resource_skills",
    "resolve_secret",
    "lease_account",
    "release_account",
]
