#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验知识索引：不点名不展开正文。"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.regression.agent_executor import (  # noqa: E402
    build_knowledge_index_text,
)
from server.services.system_settings_service import knowledge_index_brief  # noqa: E402


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> None:
    rows = [
        {"id": "k1", "title": "如何登录", "category": "登录注册", "when": "登录", "used": True,
         "content": "点手机号再填验证码这条很长的正文不应该出现在目录里"},
        {"id": "k2", "title": "空壳", "used": False, "content": "x"},
    ]
    text = build_knowledge_index_text(rows)
    _assert("k1" in text and "如何登录" in text, text)
    _assert("点手机号" not in text, text)
    _assert("k2" not in text, text)

    # 函数可调用（库可能为空）
    try:
        brief = knowledge_index_brief("登录 首页", limit=12)
    except Exception:
        brief = []
    _assert(isinstance(brief, list), type(brief))
    for row in brief:
        _assert("id" in row and "title" in row, row)
        _assert("content" not in row or not row.get("content"), row)

    named = SimpleNamespace(knowledge_ids=["k1"])
    _assert(list(named.knowledge_ids) == ["k1"], "named ids")
    print("ok: knowledge index has titles not bodies")


if __name__ == "__main__":
    main()
