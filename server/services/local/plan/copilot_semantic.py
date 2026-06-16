# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""口语化指令语义展开（Tab 切换、随机切换等），避免字面生成不存在的控件名。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from script.log import SLog

TAG = "CopilotSemantic"

_TAB_COUNT_MAP = {
    "两": 2, "二": 2, "2": 2,
    "三": 3, "3": 3,
    "四": 4, "4": 4,
    "五": 5, "5": 5,
}


def _normalize_tab_label(name: str) -> str:
    s = (name or "").strip()
    s = re.sub(r"(按钮|按键|图标|入口|菜单|tab|Tab|TAB)$", "", s, flags=re.I).strip()
    return s


def discover_bottom_tabs(
    sn: Optional[str],
    platform: str = "android",
    *,
    max_tabs: int = 6,
) -> List[str]:
    """从当前界面层级提取底栏 Tab 文案（按从左到右）。"""
    if not sn:
        return []
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
        from driver.agent.Crawl.ui_discovery import discover_clickables_from_hierarchy

        builtins.TARGET_DEVICE_SN = str(sn)
        engine, (w, h) = bootstrap_mobile_engine(sn, platform)
        y_min = int(h * 0.86)
        candidates: List[tuple] = []
        for t in discover_clickables_from_hierarchy(engine, w, h, max_items=64):
            cy = t.y + t.h // 2
            if cy < y_min:
                continue
            label = _normalize_tab_label(t.label)
            if not label or len(label) > 8:
                continue
            if re.search(r"随意|随机|切换", label):
                continue
            candidates.append((t.x, label))
        candidates.sort(key=lambda x: x[0])
        seen = set()
        out: List[str] = []
        for _, label in candidates:
            if label in seen:
                continue
            seen.add(label)
            out.append(label)
            if len(out) >= max_tabs:
                break
        return out
    except Exception as e:
        SLog.w(TAG, f"discover_bottom_tabs failed: {e}")
        return []


def _tabs_from_graph(app_id: Optional[str]) -> List[str]:
    if not app_id:
        return []
    try:
        from server.core.database import SessionLocal
        from server.models.AppGraph.app_structure import AppGraph
        from server.models.AppGraph.app_component import AppComponent

        session = SessionLocal()
        try:
            graph = session.query(AppGraph).filter(AppGraph.app_id == str(app_id)).first()
            if not graph:
                return []
            labels: List[str] = []
            for comp in (
                session.query(AppComponent)
                .filter(AppComponent.graph_id == graph.id)
                .all()
            ):
                name = (comp.label or comp.name or "").strip()
                if name and len(name) <= 8:
                    labels.append(_normalize_tab_label(name))
            return list(dict.fromkeys(labels))[:8]
        finally:
            session.close()
    except Exception as e:
        SLog.w(TAG, f"tabs from graph failed: {e}")
    return []


def _resolve_tab_list(
    sn: Optional[str],
    platform: str,
    context: Optional[Dict[str, Any]],
    *,
    limit: int = 5,
) -> List[str]:
    ctx = context or {}
    tabs = discover_bottom_tabs(sn, platform)
    if not tabs:
        tabs = _tabs_from_graph(ctx.get("app_id"))
    return tabs[:limit]


def _parse_tab_count(segment: str) -> int:
    m = re.search(r"(两|二|三|四|五|\d+)\s*个?\s*tab", segment, re.I)
    if not m:
        m = re.search(r"(\d+)\s*个", segment)
    if not m:
        return 4
    token = m.group(1)
    if token.isdigit():
        return max(2, min(8, int(token)))
    return _TAB_COUNT_MAP.get(token, 4)


def _mentioned_tabs_in_segment(segment: str) -> set:
    mentioned = set()
    for m in re.finditer(r"[「『\"']([^」』\"']+)[」』\"']", segment):
        mentioned.add(_normalize_tab_label(m.group(1)))
    for m in re.finditer(r"点击\s*([^\s,，]+)", segment):
        mentioned.add(_normalize_tab_label(m.group(1)))
    return mentioned


def semantic_split_segment(
    segment: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Optional[List[str]]:
    """
    若 segment 为口语化 Tab 切换描述，展开为多条「点击{真实Tab}」子指令。
    返回 None 表示无需展开，由原 _plan_segment 处理。
    """
    seg = (segment or "").strip()
    if not seg:
        return None

    try:
        from server.services.copilot_service import parse_bottom_tab_label

        tab_core = parse_bottom_tab_label(seg)
        if tab_core and re.search(r"点击|tap|点一下", seg, re.I):
            target = f"点击底部{tab_core}"
            seg_norm = re.sub(r"\s+", "", seg)
            target_norm = re.sub(r"\s+", "", target)
            if seg_norm != target_norm:
                return [target]
    except Exception:
        pass

    platform = (context or {}).get("platform", "android")
    tabs = _resolve_tab_list(sn, platform, context)
    mentioned = _mentioned_tabs_in_segment(seg)

    # 「随意/随机 + tab」—— 不是控件名，改为依次点其余底栏 Tab
    if re.search(r"随意|随机", seg, re.I) and (re.search(r"tab", seg, re.I) or re.search(r"切换", seg, re.I)):
        rest = [t for t in tabs if _normalize_tab_label(t) not in mentioned]
        if rest:
            return [f"点击{t}" for t in rest]
        return [] if tabs else None

    # 「N个tab间切换 / 切换四个tab」
    if re.search(r"tab", seg, re.I) and re.search(r"切换|间", seg, re.I):
        n = _parse_tab_count(seg)
        pick = [t for t in tabs if _normalize_tab_label(t) not in mentioned][:n]
        if pick:
            return [f"点击{t}" for t in pick]
        if tabs:
            return [f"点击{t}" for t in tabs[:n]]

    # 单条但 label 含「随意切换」类废话
    if re.search(r"点击|tap", seg, re.I):
        label_m = re.search(r"[「『\"']([^」』\"']+)[」』\"']", seg) or re.search(
            r"点击\s*([^\s,，]+)", seg
        )
        if label_m:
            label = label_m.group(1).strip()
            if re.search(r"随意|随机", label, re.I) and re.search(r"tab|切换", label, re.I):
                rest = [t for t in tabs if _normalize_tab_label(t) not in mentioned]
                if rest:
                    return [f"点击{t}" for t in rest]
                return []

    return None
