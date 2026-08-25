#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""扫描通用服务里的应用业务字面量（A 类去耦的防回流守卫）。

为什么需要：
  case_precondition / page_context / page_navigation / copilot 这些**名字通用、实现专用**
  的服务里曾经写死了单个被测应用的 tab 文案。换应用不是「效果变差」，是判定错了还告诉你
  判定对了。清理完如果没有守卫，它一定会长回来 —— 下一个人为了让某个应用跑通，
  最省事的办法永远是再加一个 if。

判据：
  从 server/resources/app_profiles/*.yaml 里取每个应用的业务字面量（app_profile.all_literals()，
  已排除「首页」「我的」这类通用词），在 SCAN_ROOTS 下的文件里找。命中即失败。
  真的需要某个词的地方：把它加进对应应用的 ui_profile，代码改成从画像读。

用法：
  python scripts/verify_no_app_literals.py            # 只报错，退出码 1
  python scripts/verify_no_app_literals.py --list     # 顺便列出所有画像和字面量
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.ai import app_profile as ap  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# 必须保持应用中立的地方
SCAN_ROOTS = [
    "server/services/shared",
    "server/services/local",
    "server/services/ai/regression/prompts.py",
    "server/services/ai/roles_catalog.py",
    "server/services/case_precondition_service.py",
    "server/services/copilot_service.py",
    "server/services/figma_logic_service.py",
    "server/services/app_automation_service.py",
    "server/services/qa_role_jobs.py",
    "server/resources/locate/page_profiles.yaml",
]

# 画像本身、画像加载器、以及本脚本当然可以出现业务词
EXEMPT = {
    "server/services/ai/app_profile.py",
    "scripts/verify_no_app_literals.py",
}

_CN_COMMENT = re.compile(r"^\s*#")


def iter_files():
    for rel in SCAN_ROOTS:
        path = ROOT / rel
        if path.is_file():
            yield path
        elif path.is_dir():
            for p in sorted(path.rglob("*")):
                if p.is_file() and p.suffix in (".py", ".yaml", ".yml") and "__pycache__" not in str(p):
                    yield p


def main() -> int:
    profiles = ap.list_profiles()
    if not profiles:
        print("没有找到任何应用画像（server/resources/app_profiles/*.yaml），跳过扫描。")
        return 0

    literals: dict[str, str] = {}          # 字面量 -> 属于哪个应用
    for prof in profiles:
        for lit in prof.all_literals():
            literals.setdefault(lit, prof.key)

    if "--list" in sys.argv:
        for prof in profiles:
            print(f"[{prof.key}] {prof.label}  packages={list(prof.packages)}")
            print(f"    字面量 {len(prof.all_literals())} 个：{prof.all_literals()}")
        print()

    hits: list[tuple[str, int, str, str, str]] = []
    for path in iter_files():
        rel = str(path.relative_to(ROOT))
        if rel in EXEMPT:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for no, line in enumerate(lines, 1):
            # 注释里提到「这个词原来写在哪」是允许的，只查真正的代码/数据
            if _CN_COMMENT.match(line):
                continue
            for lit, app_key in literals.items():
                if lit in line:
                    hits.append((rel, no, app_key, lit, line.strip()[:110]))

    if not hits:
        print(f"通过：{len(SCAN_ROOTS)} 个扫描目标里没有发现应用业务字面量"
              f"（比对了 {len(profiles)} 个画像、{len(literals)} 个字面量）。")
        return 0

    by_file: dict[str, list] = {}
    for rel, no, app_key, lit, text in hits:
        by_file.setdefault(rel, []).append((no, app_key, lit, text))

    print(f"发现 {len(hits)} 处应用业务字面量，分布在 {len(by_file)} 个文件：\n")
    for rel in sorted(by_file):
        print(f"── {rel}")
        for no, app_key, lit, text in by_file[rel]:
            print(f"   {no:>5}  [{app_key}] 「{lit}」  {text}")
        print()
    print("修法：把这个词加进 server/resources/app_profiles/<app>.yaml，代码改成从 "
          "app_profile.current() 读，而不是写死在通用服务里。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
