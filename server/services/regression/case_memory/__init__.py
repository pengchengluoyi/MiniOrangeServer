# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Case Memory：baseline / run trace 持久化 + 三段窗口 + 总览注入。

API 划分
--------
- repo.py    : SQLAlchemy CRUD（save_run_trace / load_baseline / promote_baseline / list_traces）
- align.py   : baseline 与新 plan 的对齐策略（case_step_index → capability_id → seq）
- windows.py : 三段窗口构造（BaselineContext）+ overview brief 文本
- service.py : 高级一站式 API：record_run_finished() / load_baseline_for_planning() / build_replan_window()

上层只用 service.py；下面 3 个文件是各自可单测的内部组件。
"""
from server.services.regression.case_memory.service import (
    build_replan_window,
    load_baseline_for_planning,
    load_baseline_overview_brief,
    promote_run_to_baseline,
    record_run_finished,
)
from server.services.regression.case_memory.windows import (
    BaselineOverview,
    baseline_snippets_from_brief,
)
from server.services.regression.case_memory.align import compute_baseline_alignment

__all__ = [
    "record_run_finished",
    "load_baseline_for_planning",
    "load_baseline_overview_brief",
    "promote_run_to_baseline",
    "build_replan_window",
    "BaselineOverview",
    "baseline_snippets_from_brief",
    "compute_baseline_alignment",
]
