# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""单次回归执行总报告：成功/失败分类与摘要。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


_CATEGORY_LABELS = {
    "passed": "通过",
    "operation_fail": "操作失败",
    "expectation_fail": "预期未达成",
    "precondition_skip": "前置未满足",
    "platform_skip": "平台不适用",
    "device_offline": "设备离线",
    "parse_error": "指令解析",
    "unknown_fail": "其它失败",
    "skipped": "跳过",
}


def _classify_case(case: Dict[str, Any]) -> str:
    status = (case.get("status") or "").strip().lower()
    msg = (case.get("msg") or "").strip()

    if status == "pass":
        return "passed"
    if status == "skip":
        if re.search(r"ios|android|平台|设备类型", msg, re.I):
            return "platform_skip"
        if re.search(r"前置|已登录|未登录|登录状态", msg):
            return "precondition_skip"
        return "skipped"
    if status != "fail":
        return "skipped"

    if re.search(r"离线|offline", msg, re.I):
        return "device_offline"
    if re.search(r"未识别子指令|解析|segment", msg, re.I):
        return "parse_error"
    if re.search(r"预期未达成|预期校验|界面校验", msg):
        return "expectation_fail"
    if re.search(r"操作失败|点击失败|未找到|Tap", msg):
        return "operation_fail"
    return "unknown_fail"


def build_run_report(run_doc: Dict[str, Any]) -> Dict[str, Any]:
    cases = list(run_doc.get("cases") or [])
    categories: Dict[str, List[Dict[str, Any]]] = {k: [] for k in _CATEGORY_LABELS}
    issues: List[Dict[str, Any]] = []

    for c in cases:
        cat = _classify_case(c)
        if cat not in categories:
            cat = "unknown_fail"
        row = {
            "case_id": c.get("case_id"),
            "name": c.get("name"),
            "status": c.get("status"),
            "msg": c.get("msg") or "",
            "duration_ms": c.get("duration_ms"),
            "category": cat,
            "category_label": _CATEGORY_LABELS.get(cat, cat),
        }
        categories[cat].append(row)
        if cat not in ("passed",):
            issues.append(row)

    # 去重 issues
    seen = set()
    deduped_issues: List[Dict[str, Any]] = []
    for row in issues:
        key = (row.get("case_id"), row.get("category"))
        if key in seen:
            continue
        seen.add(key)
        deduped_issues.append(row)

    passed = int(run_doc.get("passed") or 0)
    failed = int(run_doc.get("failed") or 0)
    skipped = int(run_doc.get("skipped") or 0)
    total = int(run_doc.get("total") or len(cases))
    foreground = run_doc.get("foreground_drift") or {}

    headline = "全部通过"
    level = "success"
    if failed > 0:
        headline = f"{failed} 条用例失败"
        level = "error"
    elif skipped > 0:
        headline = f"{passed} 通过，{skipped} 条跳过"
        level = "warning"

    return {
        "headline": headline,
        "level": level,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "executed": run_doc.get("executed") or len(cases),
            "duration_ms": run_doc.get("duration_ms"),
        },
        "categories": {
            k: {
                "label": _CATEGORY_LABELS.get(k, k),
                "count": len(v),
                "items": v,
            }
            for k, v in categories.items()
            if v
        },
        "issues": deduped_issues,
        "foreground_drift": foreground,
    }
