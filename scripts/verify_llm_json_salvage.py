#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验 LLM JSON 截断抢救逻辑（llm_client._salvage_truncated_json）。

为什么需要这个脚本：
  `_extract_first_json_object` 靠括号配平，被 max_tokens 截断的输出永远配不平，
  会让一整批（8 个测试点）的用例静默退化成模板桩用例。抢救逻辑必须满足两条：
    1. 能救回已完整的元素
    2. **绝不**留下残缺元素（例如只有 case_id、没有 steps 的用例）
  第 2 条比第 1 条重要 —— 残缺元素会伪装成真用例，把覆盖率刷高。

用法：python scripts/verify_llm_json_salvage.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.ai.regression.llm_client import (  # noqa: E402
    _extract_first_json_object,
    _salvage_truncated_json,
)

CASES = [
    (
        "用例数组截断在第 3 条中间 → 只留完整的 2 条",
        '{"cases":[{"case_id":"a","steps":"1. x"},{"case_id":"b","steps":"1. y"},{"case_id":"c","ste',
        lambda r: len(r["cases"]) == 2 and r["cases"][-1]["case_id"] == "b",
    ),
    (
        "截断在数组级逗号后",
        '{"cases":[{"a":1},{"b":2},',
        lambda r: len(r["cases"]) == 2,
    ),
    (
        "嵌套脑图树截断 → 丢掉不完整的 Web 枝",
        '{"title":"t","children":[{"text":"App","children":[{"text":"m1"},{"text":"m2"}]},{"text":"Web","chil',
        lambda r: len(r["children"]) == 1 and len(r["children"][0]["children"]) == 2,
    ),
    (
        "完整 JSON 不被破坏",
        '{"cases":[{"a":1}]}',
        lambda r: r == {"cases": [{"a": 1}]},
    ),
    (
        "字符串内的括号和转义引号不误判",
        '{"cases":[{"steps":"1. 点击\\"确定\\" {不是括号}"},{"steps":"2. broken',
        lambda r: len(r["cases"]) == 1 and "确定" in r["cases"][0]["steps"],
    ),
    (
        "一个完整元素都没有 → None（不许编）",
        '{"cases":[{"case_id":"aaa',
        lambda r: r is None,
    ),
    (
        "markdown 包裹 + 截断",
        '```json\n{"cases":[{"a":1},{"b":',
        lambda r: len(r["cases"]) == 1,
    ),
    (
        "顶层字段截断 → 保住已完整的字段",
        '{"title":"x","points":[{"id":"tp1"},{"id":"tp2"}],"missing_po',
        lambda r: r.get("title") == "x" and len(r["points"]) == 2,
    ),
    (
        "无数组的顶层截断 → 保住前面的标量字段",
        '{"summary":"一句话","change_kind":"optimize","baseli',
        lambda r: r.get("change_kind") == "optimize",
    ),
    (
        "深层对象内的逗号不切 → 不留残缺元素",
        '{"a":[{"x":1,"y":2},{"x":3,"y":',
        lambda r: len(r["a"]) == 1 and r["a"][0]["y"] == 2,
    ),
    ("空输入", "", lambda r: r is None),
    ("没有大括号", "sorry I cannot", lambda r: r is None),
]


def main() -> int:
    failed = 0
    rescued = 0
    for i, (name, raw, check) in enumerate(CASES, 1):
        baseline = _extract_first_json_object(raw)
        got = _salvage_truncated_json(raw)
        try:
            ok = bool(check(got))
        except Exception as e:
            ok, got = False, f"{got!r} (校验抛错 {e})"
        if not ok:
            failed += 1
        if baseline is None and got is not None:
            rescued += 1
        flag = "PASS" if ok else "FAIL"
        print(f"{flag}  #{i:<2} {name}")
        if not ok:
            print(f"        实际：{got!r}")

    print(f"\n{len(CASES) - failed}/{len(CASES)} 通过；其中 {rescued} 个场景是现有实现救不回、抢救逻辑救回来的。")
    if failed:
        print("有失败：截断抢救行为已变化，请检查 llm_client._salvage_truncated_json")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
