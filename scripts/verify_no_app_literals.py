#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""扫描通用服务里的应用业务字面量（防回流守卫）。

YAML 已退出运行期。守卫改盯一份退休清单：这些词曾经写死在通用服务里，
不允许再长回去。真的需要某个词：写进该应用的说明书（库），代码从 bind 后的
app_profile.current() 读。

用法：
  python scripts/verify_no_app_literals.py            # 只报错，退出码 1
  python scripts/verify_no_app_literals.py --list     # 列出退休字面量
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.playbook_fixture import zaohaowu_profile  # noqa: E402
from server.services.ai import app_profile as ap  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

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

EXEMPT = {
    "server/services/ai/app_profile.py",
    "server/services/ai/playbook_service.py",
    "scripts/verify_no_app_literals.py",
    "scripts/verify_ui_profile.py",
    "scripts/verify_app_facts_injected.py",
    "scripts/playbook_fixture.py",
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


def retired_literals() -> dict[str, str]:
    """字面量 -> 来源。夹具有内容时用夹具；否则用硬编码退休清单。"""
    literals: dict[str, str] = {}
    try:
        prof = zaohaowu_profile()
        for lit in prof.all_literals():
            literals.setdefault(lit, prof.key or "retired")
    except Exception:
        pass
    for lit in (
        "造好物",
        "造物秀",
        "想要成真",
        "真造物秀",
        "传图定制",
        "创意定制",
        "造物者",
        "艺术家专区",
        "定制专区",
        "定制页",
        "定制模版页",
        "模型管理",
        "造物者，你好",
    ):
        if lit not in ap.GENERIC_NAV_WORDS:
            literals.setdefault(lit, "retired")
    return literals


def main() -> int:
    literals = retired_literals()
    if "--list" in sys.argv:
        print(f"退休/夹具字面量 {len(literals)} 个：")
        for lit, src in sorted(literals.items(), key=lambda x: (x[1], x[0])):
            print(f"  [{src}] {lit}")
        print()

    hits: list[tuple[str, int, str, str, str]] = []
    for path in iter_files():
        rel = str(path.relative_to(ROOT))
        if rel in EXEMPT or rel.startswith("scripts/fixtures/"):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for no, line in enumerate(lines, 1):
            if _CN_COMMENT.match(line):
                continue
            for lit, app_key in literals.items():
                if lit in line:
                    hits.append((rel, no, app_key, lit, line.strip()[:110]))

    if not hits:
        print(
            f"通过：{len(SCAN_ROOTS)} 个扫描目标里没有发现应用业务字面量"
            f"（比对了 {len(literals)} 个字面量）。"
        )
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
    print("修法：把这个词写进该应用的「应用基础逻辑」（库），代码改成从 "
          "app_profile.current() / playbook 读，而不是写死在通用服务里。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
