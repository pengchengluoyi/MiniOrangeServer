# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Baseline ↔ 当前 plan 的对齐策略。

为什么需要对齐？
----------------
baseline 存的是"上一次执行的 events 序列"，但本次 plan 由 AI 重新生成，可能：
  - 多 / 少几条事件（AI 加了 wait_screen_ready）
  - 顺序微调（合并/拆分动作）
  - 同一条 case_step 被映射成不同 capability_id

复合策略（默认）
----------------
对于本次 plan 的第 i 条 event，按以下优先级在 baseline 中找"对应那条"：
  1. case_step_index 相等 + 同一 case_step 下出现序号相同
  2. capability_id 相等 + 出现序号相同
  3. seq 相等
找不到则映射为 None（窗口给 BaselineSnippet 留空表示"上次没这条"）。

输出
----
compute_baseline_alignment(...) → list[Optional[int]]，长度 = len(current_events)，
每个元素是匹配到的 baseline_index（或 None）。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional


def _get(ev: Any, key: str, default: Any = None) -> Any:
    if isinstance(ev, dict):
        return ev.get(key, default)
    return getattr(ev, key, default)


def _step_idx(ev: Any) -> Optional[int]:
    v = _get(ev, "case_step_index", None)
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _cap(ev: Any) -> str:
    return str(_get(ev, "capability_id", "") or "")


def _seq(ev: Any) -> int:
    try:
        return int(_get(ev, "seq", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _build_indices(events: list[Any]) -> tuple[dict, dict, dict]:
    """返回 3 个查找索引。

    - by_step:    {step_index: [baseline_idx, ...]} 保持出现顺序
    - by_cap:     {capability_id: [baseline_idx, ...]}
    - by_seq:     {seq: baseline_idx}
    """
    by_step: dict[int, list[int]] = defaultdict(list)
    by_cap: dict[str, list[int]] = defaultdict(list)
    by_seq: dict[int, int] = {}
    for idx, ev in enumerate(events):
        si = _step_idx(ev)
        if si is not None:
            by_step[si].append(idx)
        cap = _cap(ev)
        if cap:
            by_cap[cap].append(idx)
        s = _seq(ev)
        if s:
            by_seq[s] = idx
    return dict(by_step), dict(by_cap), by_seq


def compute_baseline_alignment(
    baseline_events: list[Any],
    current_events: list[Any],
) -> list[Optional[int]]:
    """计算 current → baseline 的下标映射。"""
    if not baseline_events or not current_events:
        return [None] * len(current_events)

    by_step, by_cap, by_seq = _build_indices(baseline_events)

    # 当前侧出现序号计数器
    step_ord: dict[int, int] = defaultdict(int)
    cap_ord: dict[str, int] = defaultdict(int)

    used: set[int] = set()
    result: list[Optional[int]] = []

    for ev in current_events:
        match: Optional[int] = None

        si = _step_idx(ev)
        cap = _cap(ev)
        seq = _seq(ev)

        # 1) case_step_index + 出现序号
        if si is not None and si in by_step:
            cands = by_step[si]
            ordinal = step_ord[si]
            step_ord[si] += 1
            if ordinal < len(cands) and cands[ordinal] not in used:
                match = cands[ordinal]

        # 2) capability_id + 出现序号
        if match is None and cap and cap in by_cap:
            cands = by_cap[cap]
            ordinal = cap_ord[cap]
            cap_ord[cap] += 1
            if ordinal < len(cands) and cands[ordinal] not in used:
                match = cands[ordinal]

        # 3) seq 完全相等（兜底，仅用于 baseline 与本次 plan 几乎同形的情况）
        #    要求 capability_id 也一致，避免把"上次第 4 步的 press_key"误配给"这次第 4 步的 input_text"
        if match is None and seq and seq in by_seq:
            cand = by_seq[seq]
            if cand not in used:
                cand_cap = _cap(baseline_events[cand])
                if not cap or not cand_cap or cap == cand_cap:
                    match = cand

        if match is not None:
            used.add(match)
        result.append(match)

    return result
