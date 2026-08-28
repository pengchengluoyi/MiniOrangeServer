#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验应用事实真的被喂进了 prompt。

`verify_no_app_literals.py` 守的是「别把应用词写回通用服务」。
这个脚本守另一半：说明书里的术语必须进 facts_prompt / playbook 段。

用法：python scripts/verify_app_facts_injected.py
"""
from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.playbook_fixture import OTHER_PKG, ZHW_PKG, bind_zaohaowu, zaohaowu_profile  # noqa: E402
from server.services.ai import app_profile as ap  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

BIND_SITES = [
    ("server/routers/rAppAutomation.py", "_tick_in_background"),
    ("server/routers/rAppAutomation.py", "qa_process_assist"),
    ("server/routers/rAppAutomation.py", "_followup_in_background"),
    ("server/routers/rAppAutomation.py", "_reanalyze_in_background"),
]


def func_source(rel: str, name: str) -> str:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment((ROOT / rel).read_text(encoding="utf-8"), node) or ""
    return ""


def _is_bound(src: str) -> bool:
    return (
        "_bind_app_profile(" in src
        or "bind_profile(" in src
        or "ap.bind(" in src
        or "app_profile_ctx.bind(" in src
        or "app_profile.bind(" in src
    )


def main() -> int:
    failed = []

    def check(cond, msg):
        print(f"   {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failed.append(msg)

    print("── 说明书里的术语必须出现在喂给模型的那段里")
    z = zaohaowu_profile()
    block = z.facts_prompt()
    check(bool(block), "造好物有应用事实段")
    for term, row in (z.lexicon or {}).items():
        means = str((row or {}).get("means") or "")
        ok = term in block and (not means or means in block)
        check(ok, f"术语「{term}」及其释义进了 prompt")
    check(z.label in block, f"应用名「{z.label}」在里面")
    for tab in z.bottom_tabs:
        check(tab in block, f"主导航「{tab}」在里面")

    print("\n── 逐字节稳定（否则 stable 分片的前缀缓存永远不命中）")
    check(block == zaohaowu_profile().facts_prompt(), "两次生成完全一致")

    print("\n── 未接入画像的应用：一个字都不许出")
    other = ap.for_package(OTHER_PKG)
    check(other.facts_prompt() == "", f"通用默认返回空串（实际 {other.facts_prompt()[:40]!r}）")
    check(not other.has_facts(), "has_facts() 为 False")
    empty_lex = ap.UiProfile(key="someapp", label="某应用")
    check(empty_lex.facts_prompt() == "", "有 key 但没有任何事实时也返回空串，不输出只有标题的空段")

    print("\n── _ask_json 真的拼进去了，且排在 stable 之前")
    src = func_source("server/services/qa_role_jobs.py", "_ask_json")
    check(bool(src), "找得到 _ask_json")
    check("facts_prompt()" in src, "_ask_json 调了 facts_prompt()")
    check("match_testing_knowledge(" in src, "_ask_json 按检索注入知识")
    if "facts_prompt()" in src and "stable" in src:
        pos_facts = src.index("facts_prompt()")
        pos_stable = src.index("if stable:")
        check(pos_facts < pos_stable, "应用事实排在 stable 之前（事实比图谱更少变，缓存前缀才切得干净）")

    print("\n── 每个会调模型的入口都绑了画像")
    for rel, name in BIND_SITES:
        src = func_source(rel, name)
        if not src:
            check(False, f"{rel}::{name} 找不到（改过名？绑定可能一起丢了）")
            continue
        bound = _is_bound(src)
        check(bound, f"{name} 绑了应用画像")
        if bound:
            check("reset(" in src, f"{name} 用完 reset 了（不 reset 会串到同线程的下一个请求）")

    print("\n── 同一段事实在两个应用下不同（证明真的按应用取）")
    tok = bind_zaohaowu()
    try:
        a = ap.current().facts_prompt()
    finally:
        ap.reset(tok)
    tok = ap.bind(package=OTHER_PKG)
    try:
        b = ap.current().facts_prompt()
    finally:
        ap.reset(tok)
    check(a and not b, f"造好物 {len(a)} 字 / 未知应用 {len(b)} 字")
    check(ap.current().facts_prompt() == "", "reset 之后回到通用默认，没有残留")
    check(ap.for_package(ZHW_PKG).facts_prompt() == "", "未 bind 时 for_package 不冒充说明书")

    print("\n" + ("=== 全部通过 ===" if not failed else f"=== {len(failed)} 条失败 ==="))
    for m in failed:
        print(f"  · {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
