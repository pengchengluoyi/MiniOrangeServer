# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""通用图标意图：CLIP / 图标库别名；不做屏幕区域裁剪（方位仅 spatial.py）。"""
from __future__ import annotations

import re
from typing import List, Optional, Tuple

_VISUAL_BY_TOKEN: Tuple[Tuple[str, str], ...] = (
    (r"微信", "wechat green chat bubble icon"),
    (r"手机|电话", "smartphone mobile phone outline icon"),
    (r"苹果|apple", "apple logo sign in icon"),
    (r"邮箱|账号|密码", "user account password login icon"),
    (r"访客|游客", "guest browse visitor icon"),
)

_ALIAS_BY_TOKEN: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("微信", ("微信", "WeChat", "wechat", "微信登录")),
    ("手机", ("手机", "phone", "mobile", "手机号", "手机图标")),
    ("苹果", ("苹果", "Apple", "apple", "Apple ID")),
    ("邮箱", ("邮箱", "email", "账号密码", "密码登录")),
)


def _strip_action_prefix(raw: str) -> str:
    q = re.sub(r"^(点击|点一下|tap|click|进入|打开)\s*", "", raw, flags=re.I).strip()
    q = re.sub(r"^(登录页(面)?|页面)(上的|中的)?\s*", "", q)
    q = re.sub(r"^(右上角|左上角|右下角|左下角|底部|底栏|顶部)(的)?\s*", "", q)
    return q.strip("「」『』【】\"' \t的")


def _icon_core_text(label: str) -> str:
    core = _strip_action_prefix((label or "").strip())
    core = re.sub(r"(按钮|按键|图标|icon|入口|菜单|tab|Tab|TAB)$", "", core, flags=re.I).strip()
    return core


def is_bottom_tab_icon_label(label: str) -> bool:
    raw = (label or "").strip()
    if not raw:
        return False
    try:
        from server.services.copilot_service import parse_bottom_tab_label

        parsed = parse_bottom_tab_label(raw)
    except Exception:
        parsed = None
    if not parsed:
        return False
    return bool(re.search(r"底部|底栏|\btab\b", raw, re.I))


def is_icon_target_label(label: str) -> bool:
    """图标类点击（含底栏 Tab、第三方图标等）；仅用于元数据，不参与通道互斥。"""
    raw = (label or "").strip()
    if not raw:
        return False
    if re.search(r"图标|\bicon\b", raw, re.I):
        return True
    if is_bottom_tab_icon_label(raw):
        return True
    try:
        from server.services.locate.clip_query_plan import lookup_clip_query_plan

        plan = lookup_clip_query_plan(raw)
        if plan and plan.icon_row:
            return True
    except Exception:
        pass
    return False


def icon_visual_query_from_label(label: str) -> Tuple[str, List[str], Optional[str]]:
    """CLIP 视觉检索词；region 恒 None（全屏），方位由 spatial 约束。"""
    raw = (label or "").strip()
    if is_bottom_tab_icon_label(raw):
        try:
            from server.services.copilot_service import parse_bottom_tab_label

            tab = parse_bottom_tab_label(raw) or _icon_core_text(raw)
        except Exception:
            tab = _icon_core_text(raw)
        return tab, [raw, tab, f"{tab} tab", f"profile tab {tab}"], None

    core = _icon_core_text(raw)
    for pat, visual in _VISUAL_BY_TOKEN:
        if re.search(pat, raw, re.I) or re.search(pat, core, re.I):
            aliases = [raw, core, visual] if core else [raw, visual]
            return visual, list(dict.fromkeys(a for a in aliases if a)), None

    if core:
        return f"{core} icon", [raw, core, f"{core} icon"], None
    return "icon button", [raw], None


def icon_name_aliases_from_label(label: str) -> List[str]:
    raw = (label or "").strip()
    if not raw:
        return []
    out: List[str] = []
    core = _icon_core_text(raw)

    def _add(c: str) -> None:
        c = (c or "").strip()
        if c and c not in out:
            out.append(c)

    _add(raw)
    _add(core)
    if is_bottom_tab_icon_label(raw):
        try:
            from server.services.copilot_service import parse_bottom_tab_label

            tab = parse_bottom_tab_label(raw)
            if tab:
                _add(tab)
                if tab == "我的":
                    _add("我")
        except Exception:
            pass

    for token, aliases in _ALIAS_BY_TOKEN:
        if token in raw or token in core:
            for a in aliases:
                _add(a)
    return out
