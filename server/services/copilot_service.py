# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
对话流：自然语言 → 可执行步骤 → Manager/引擎执行（类似 Midscene 的规划+执行循环）。
"""
from __future__ import annotations

import ast
import base64
import io
import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from script.log import SLog

import requests

TAG = "CopilotService"


# 规划阶段无坐标时的占位；执行时会按文案/层级/OCR 重新定位
_DEFAULT_CLICK_XY = (0, 0)

# 登录页底部图标行默认顺序（左→右，仅作兜底）；未安装微信等导致图标缺失时不能盲用槽位
_DEFAULT_LOGIN_ICON_ORDER = {
    "wechat": 0,
    "phone_sms": 1,
    "email_password": 2,
    "apple": 3,
}
_LOGIN_INTENT_RESOURCE_HINTS = {
    "wechat": ("wechat", "weixin", "wx", "微信"),
    "phone_sms": ("phone", "mobile", "sms", "verify", "手机", "验证码", "短信"),
    "email_password": ("email", "password", "account", "mail", "邮箱", "密码", "账号"),
    "apple": ("apple", "苹果"),
}
_LOGIN_METHOD_U2_HINTS = {
    "wechat": ("微信", "WeChat", "wechat"),
    "phone_sms": ("手机号登录", "手机登录", "验证码登录", "短信登录", "phone login"),
    "email_password": ("密码", "邮箱", "账号", "email", "password", "帐号"),
    "apple": ("Apple", "苹果", "apple", "Apple ID"),
}
_LOGIN_ICON_ALIAS_GROUPS = {
    "wechat": ("微信", "微信登录", "微信登录方式", "WeChat"),
    "phone_sms": ("手机号登录", "手机登录", "验证码登录", "手机号登录方式", "短信登录"),
    "email_password": ("账号密码", "邮箱密码", "密码登录", "邮箱登录", "账号密码登录"),
    "apple": ("苹果", "Apple", "appleid", "Apple ID", "苹果账号", "苹果登录"),
}






def _label_variants(label: str) -> List[str]:
    raw = (label or "").strip()
    if not raw:
        return []
    out: List[str] = []

    def _add(cand: str) -> None:
        c = (cand or "").strip()
        if c and c not in out:
            out.append(c)

    _add(raw)
    _add(re.sub(r"(按钮|按键|图标|入口|菜单)$", "", raw).strip())
    _add(re.sub(r"(按钮|按键|图标|入口|菜单|tab|Tab|TAB)$", "", raw, flags=re.I).strip())
    if re.search(r"底部|底栏", raw):
        core_tab = re.sub(r"底部|底栏", "", raw, flags=re.I).strip()
        core_tab = re.sub(r"tab|Tab|TAB", "", core_tab, flags=re.I).strip()
        core_tab = core_tab.strip("「」『』\"' ")
        _add(core_tab)
    parsed_tab = parse_bottom_tab_label(raw)
    if parsed_tab:
        _add(parsed_tab)
    # 用例里常见「登录页面一键登录按钮」→ 抽核心文案
    _add(re.sub(r"^.{0,16}(页面|页|界面|屏幕)", "", raw).strip())
    _add(re.sub(r"^(点击|点|选择|进入|打开)", "", raw).strip())
    core = re.sub(r"^.{0,16}(页面|页|界面|屏幕)", "", raw)
    core = re.sub(r"(按钮|按键|图标|入口|菜单)$", "", core).strip()
    _add(core)
    if _is_one_click_login_label(raw):
        _add("一键登录")
        _add("本机号码一键登录")
        _add("本机号码")
    try:
        from server.services.local.locate.icon_intent import icon_name_aliases_from_label

        for alias in icon_name_aliases_from_label(raw):
            _add(alias)
    except Exception:
        intent = _classify_login_method_intent(raw)
        if intent and intent != "one_click":
            for alias in _LOGIN_ICON_ALIAS_GROUPS.get(intent, ()):
                _add(alias)
    return out


def _make_target_rect(
    left: int,
    top: int,
    width: int,
    height: int,
    *,
    label: str = "",
) -> Dict[str, Any]:
    w = max(1, int(width))
    h = max(1, int(height))
    l = int(left)
    t = int(top)
    return {
        "left": l,
        "top": t,
        "width": w,
        "height": h,
        "center": [l + w // 2, t + h // 2],
        "label": (label or "").strip(),
    }


def parse_bottom_tab_label(label: str) -> Optional[str]:
    """从「底部想要tab」「底栏[消息]」等口语描述提取 Tab 文案。"""
    raw = (label or "").strip()
    if not raw:
        return None
    if re.search(r"协议|勾选|checkbox|复选框|单选框|隐私条款|用户协议", raw, re.I):
        return None
    if not (re.search(r"底部|底栏", raw) or re.search(r"tab", raw, re.I)):
        return None
    t = re.sub(r"^(点击|点一下|tap|click)\s*", "", raw, flags=re.I).strip()
    t = re.sub(r"底部|底栏", "", t, flags=re.I).strip()
    t = re.sub(r"tab|Tab|TAB", "", t, flags=re.I).strip()
    t = t.strip("「」『』\"' \t")
    return t or None


def _is_bottom_tab_intent(label: str) -> bool:
    return bool(parse_bottom_tab_label(label))


_BOTTOM_TAB_NAMES = frozenset(
    {"首页", "想要", "消息", "我的", "我", "发现", "探索", "分类", "购物车"}
)
_NON_BOTTOM_TAB_SHORT = frozenset(
    {
        "下一步",
        "确认",
        "确定",
        "取消",
        "提交",
        "完成",
        "发送",
        "关注",
        "返回",
        "关闭",
        "保存",
        "删除",
        "编辑",
        "搜索",
    }
)
_LOGIN_DISCLAIMER_MARKERS = ("运营商", "认证服务", "服务由", "条款", "协议", "隐私")
_SEGMENT_TAB_NAMES = frozenset(
    {"造物秀", "AI创意", "想要成真", "真造物秀", "怪兽", "艺术家专区"}
)


def _is_segment_tab_query(label: str) -> bool:
    raw = (label or "").strip()
    if not raw:
        return False
    core = re.sub(r"^(点击|点一下|tap|click|进入|打开)\s*", "", raw, flags=re.I).strip()
    core = re.sub(r"(列表|页面|界面|tab)$", "", core, flags=re.I).strip()
    if core in _SEGMENT_TAB_NAMES:
        return True
    return any(name in raw for name in _SEGMENT_TAB_NAMES)


def _is_probable_bottom_tab_query(label: str) -> bool:
    """仅明确底栏 Tab 才走底栏定位，避免「下一步」「【关注】」误走底栏。"""
    raw = (label or "").strip()
    if not raw:
        return False
    if _is_toggle_intent(raw):
        return False
    if _is_segment_tab_query(raw):
        return False
    if _is_bottom_tab_intent(raw):
        return True
    if re.search(r"[【\[]", raw) or re.search(r"登录|一键|按钮|协议|隐私|同意|密码|验证码|下一步|关注", raw):
        return False
    core = re.sub(r"^(点击|点一下|tap|click)\s*", "", raw, flags=re.I).strip()
    core = core.strip("「」『』【】\"' \t")
    if core in _NON_BOTTOM_TAB_SHORT:
        return False
    if core in _BOTTOM_TAB_NAMES:
        return True
    if re.search(r"底部|底栏", raw):
        return len(core) <= 6
    return False


def _match_bottom_tab_label(query: str, target: str) -> bool:
    """底栏 Tab 仅精确匹配，禁止「想要」命中「想要成真」。"""
    q = (query or "").strip()
    t = (target or "").strip()
    if not q or not t:
        return False
    if q == t:
        return True
    for vq in _label_variants(q):
        if vq == t:
            return True
        for vt in _label_variants(t):
            if vq == vt:
                return True
    return False


def _in_bottom_bar_band(cy: int, screen_h: int, *, ratio: float = 0.86) -> bool:
    return cy >= int(screen_h * ratio)


def _discover_segment_tab_targets(
    engine,
    screen_w: int,
    screen_h: int,
) -> List[Tuple[Any, str]]:
    """顶栏分段 Tab（如「造物秀」「AI创意」），y 约在屏高 5%~28%。"""
    try:
        from driver.agent.Crawl.ui_discovery import (
            discover_clickables_from_hierarchy,
            discover_clickables_ocr,
        )

        y_max = int(screen_h * 0.28)
        y_min = int(screen_h * 0.04)
        merged: List[Tuple[int, Any, str]] = []
        seen: set = set()

        def consider(t, source: str) -> None:
            cy = t.y + t.h // 2
            if cy < y_min or cy > y_max:
                return
            name = (t.label or "").strip()
            if not name or len(name) > 12:
                return
            key = (name, int(t.x) // 12, int(t.y) // 12)
            if key in seen:
                return
            seen.add(key)
            merged.append((int(t.x), t, source))

        for t in discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=80):
            consider(t, "hierarchy")

        shot = None
        if hasattr(engine, "screenshot"):
            try:
                shot = engine.screenshot()
            except Exception:
                shot = None
        if shot is not None:
            for t in discover_clickables_ocr(shot, screen_w, screen_h, max_items=48):
                consider(t, "ocr")

        merged.sort(key=lambda x: x[0])
        return [(t, src) for _, t, src in merged]
    except Exception as e:
        SLog.w(TAG, f"discover segment tab targets failed: {e}")
        return []


def _resolve_segment_tab_target(
    label: str,
    engine,
    screen_w: int,
    screen_h: int,
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[Tuple[int, int]], str, str, Optional[Dict[str, Any]]]:
    raw = (label or "").strip()
    search_names: List[str] = []
    for name in _SEGMENT_TAB_NAMES:
        if name in raw:
            search_names.append(name)
    if not search_names:
        core = re.sub(r"^(点击|点一下|tap|click|进入|打开)\s*", "", raw, flags=re.I).strip()
        core = re.sub(r"(列表|页面|界面)$", "", core).strip()
        if core:
            search_names.append(core)
    search_names.extend(_label_variants(raw))

    targets = _discover_segment_tab_targets(engine, screen_w, screen_h)
    for t, source in targets:
        for cand in search_names:
            if cand and (cand == t.label or cand in (t.label or "")):
                cx, cy = t.center
                rect = _make_target_rect(t.x, t.y, t.w, t.h, label=t.label)
                method = "hierarchy" if source == "hierarchy" else "ocr"
                return (cx, cy), method, f"顶栏「{t.label}」@({cx},{cy})", rect

    clip_hit = _try_clip_resolve(
        engine, screen_w, screen_h, label=raw, icon_targets=icon_targets
    )
    if clip_hit and clip_hit[0]:
        cx, cy = clip_hit[0]
        rect = clip_hit[3]
        method = clip_hit[1] or "clip"
        if rect and int(rect.get("top") or cy) <= int(screen_h * 0.28):
            return clip_hit

    visible = list({(t.label or "").strip() for t, _ in targets if (t.label or "").strip()})
    detail = (
        f"顶栏未找到「{search_names[0] if search_names else raw}」"
        + (f"，当前可见：{'、'.join(visible)}" if visible else "")
    )
    return None, "none", detail, None


def _discover_bottom_tab_targets(
    engine,
    screen_w: int,
    screen_h: int,
) -> List[Tuple[Any, str]]:
    """全屏收集可点击控件（层级 + OCR），由文本相似度匹配 Tab 名。"""
    try:
        from driver.agent.Crawl.ui_discovery import (
            discover_clickables_from_hierarchy,
            discover_clickables_ocr,
        )

        merged: List[Tuple[int, Any, str]] = []
        seen: set = set()

        def consider(t, source: str) -> None:
            name = (t.label or "").strip()
            if not name or len(name) > 10:
                return
            if re.search(r"随意|随机|切换", name):
                return
            key = (name, int(t.x) // 12, int(t.y) // 12)
            if key in seen:
                return
            seen.add(key)
            merged.append((int(t.x), t, source))

        for t in discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=80):
            consider(t, "hierarchy")

        shot = None
        if hasattr(engine, "screenshot"):
            try:
                shot = engine.screenshot()
            except Exception:
                shot = None
        if shot is not None:
            for t in discover_clickables_ocr(shot, screen_w, screen_h, max_items=48):
                consider(t, "ocr")

        merged.sort(key=lambda x: x[0])
        return [(t, src) for _, t, src in merged]
    except Exception as e:
        SLog.w(TAG, f"discover bottom tab targets failed: {e}")
        return []


def _resolve_bottom_tab_target(
    label: str,
    engine,
    screen_w: int,
    screen_h: int,
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[Tuple[int, int]], str, str, Optional[Dict[str, Any]]]:
    """仅在当前屏底栏已识别到的控件上匹配 Tab，不做槽位估算。"""
    parsed = parse_bottom_tab_label(label) or (label or "").strip()
    search_names: List[str] = []
    if parse_bottom_tab_label(label):
        search_names.append(parsed)
    search_names.extend(_label_variants(label))

    targets = _discover_bottom_tab_targets(engine, screen_w, screen_h)
    for t, source in targets:
        for cand in search_names:
            if _match_bottom_tab_label(cand, t.label):
                cx, cy = t.center
                rect = _make_target_rect(t.x, t.y, t.w, t.h, label=t.label)
                method = "hierarchy" if source == "hierarchy" else "ocr"
                return (cx, cy), method, f"底栏「{t.label}」@({cx},{cy})", rect

    icon_hit = _resolve_icon_target(
        parsed, icon_targets, screen_w=screen_w, screen_h=screen_h
    )
    if icon_hit:
        cx, cy, detail, rect = icon_hit
        return (cx, cy), "icon_target", detail, rect

    for tab_query in (parsed, f"{parsed} tab icon", f"profile tab {parsed}"):
        clip_hit = _try_clip_resolve(
            engine,
            screen_w,
            screen_h,
            label=tab_query,
            icon_targets=icon_targets,
        )
        if clip_hit and clip_hit[0]:
            rect = clip_hit[3] if len(clip_hit) > 3 else None
            cx, cy = clip_hit[0]
            return (
                (cx, cy),
                clip_hit[1] or "clip",
                f"Tab CLIP「{parsed}」@({cx},{cy})",
                rect,
            )

    for alias in (parsed, "我" if parsed == "我的" else ""):
        if not alias:
            continue
        icon_hit = _resolve_icon_target(
            alias, icon_targets, screen_w=screen_w, screen_h=screen_h
        )
        if icon_hit:
            cx, cy, detail, rect = icon_hit
            return (cx, cy), "icon_target", f"图标库「{alias}」{detail}", rect

    visible: List[str] = []
    for t, _ in targets:
        name = (t.label or "").strip()
        if not name or name in visible:
            continue
        if len(name) > 12 and not re.match(r"^icon_\d+$", name, re.I):
            continue
        visible.append(name)
    if visible:
        detail = f"底栏未找到「{parsed}」，当前可见 Tab：{'、'.join(visible[:12])}"
    else:
        detail = (
            f"底栏未找到「{parsed}」：当前屏未识别到底栏 Tab 文案"
            "（请确认在首页且底栏可见；无字 Tab 请录入图标库）"
        )
    return None, "none", detail, None


def _is_login_related_label(label: str) -> bool:
    return any(k in (label or "") for k in ("登录", "一键", "手机号", "注册"))


def _is_one_click_login_label(label: str) -> bool:
    """仅「本机号码一键登录」主按钮，不含底部图标入口。"""
    raw = (label or "").strip()
    if not raw:
        return False
    if re.search(r"微信|苹果|Apple|邮箱|密码|验证码|游客|账号密码", raw, re.I):
        return False
    if re.search(r"手机(号)?.*(方式|登录方式)", raw) and "一键" not in raw and "本机" not in raw:
        return False
    return bool(re.search(r"一键|本机号码", raw))


def _classify_login_method_intent(label: str) -> Optional[str]:
    """登录澄清流程用的槽位分类（one_click / wechat / …）；定位主路径请用 locate.icon_intent。"""
    raw = (label or "").strip()
    if not raw:
        return None
    if _is_one_click_login_label(raw):
        return "one_click"
    if re.search(r"微信", raw):
        return "wechat"
    if re.search(r"输入框", raw):
        return None
    if re.search(r"苹果|apple\s*id|appleid", raw, re.I):
        return "apple"
    if re.search(r"邮箱|账号密码|密码登录|密码方式|帐号密码|使用账号密码", raw):
        return "email_password"
    if re.search(r"手机(号)?.*(图标|icon)|手机.*(登录|方式)|验证码|短信", raw, re.I):
        return "phone_sms"
    if re.search(r"访客|游客", raw):
        return None
    if "登录" not in raw:
        return None
    return None


def _discover_login_icon_row(
    engine,
    screen_w: int,
    screen_h: int,
) -> List:
    """登录页主按钮与协议行之间的无字图标行（左→右）。"""
    from driver.agent.Crawl.ui_discovery import discover_clickables_from_hierarchy

    y_lo = int(screen_h * 0.66)
    y_hi = int(screen_h * 0.87)
    max_w = int(screen_w * 0.22)
    max_h = int(screen_h * 0.08)
    skip_fragments = (
        "一键",
        "本机",
        "访客",
        "同意",
        "协议",
        "阅读",
        "造好",
        "登录按钮",
        "checkbox",
        "CheckBox",
        "运营商",
        "认证服务",
    )

    candidates = []
    for t in discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=96):
        cy = t.y + t.h // 2
        if cy < y_lo or cy > y_hi:
            continue
        if t.w > max_w or t.h > max_h or t.w < 20 or t.h < 20:
            continue
        lbl = (t.label or "").strip()
        low = lbl.lower()
        if any(k in lbl or k in low for k in skip_fragments):
            continue
        if len(lbl) > 8 and re.search(r"[\u4e00-\u9fff]{3,}", lbl):
            continue
        candidates.append(t)

    if not candidates:
        return []

    centers_y = [t.y + t.h // 2 for t in candidates]
    centers_y.sort()
    row_y = centers_y[len(centers_y) // 2]
    band = max(36, int(screen_h * 0.035))
    row = [t for t in candidates if abs((t.y + t.h // 2) - row_y) <= band]
    row.sort(key=lambda t: t.x)
    return row


def _clip_search_params(label: str) -> Tuple[str, List[str], Optional[str]]:
    """
    从自然语言指令提炼 CLIP 检索词 / 别名。
    屏幕方位仅由 spatial.py 约束；此处第三项恒为 None。
    """
    raw = (label or "").strip()
    if not raw:
        return "", [], None

    try:
        from server.services.local.locate.clip_query_plan import clip_params_from_plan, lookup_clip_query_plan

        plan = lookup_clip_query_plan(raw)
        if plan:
            return clip_params_from_plan(plan, raw)
    except Exception:
        pass

    parsed_tab = parse_bottom_tab_label(raw)
    if parsed_tab:
        aliases = [raw] if raw != parsed_tab else []
        return parsed_tab, aliases, None

    if _is_segment_tab_query(raw):
        for name in _SEGMENT_TAB_NAMES:
            if name in raw:
                return name, [raw], None

    if re.search(r"勾选|勾上|checkbox|单选|radio|复选", raw, re.I):
        from server.services.local.locate.toggle_locate_service import parse_toggle_intent, toggle_clip_queries

        intent = parse_toggle_intent(raw)
        if intent:
            q, extras = toggle_clip_queries(intent)
            return q, extras + ([raw] if raw != q else []), None
        q = "empty checkbox"
        return q, ["checkbox", "round checkbox", raw], None

    if re.search(r"下一步|继续|提交", raw):
        return (
            re.sub(r"^(点击|点一下)\s*", "", raw).strip() or "下一步",
            [raw],
            None,
        )

    try:
        from server.services.local.locate.icon_intent import (
            icon_visual_query_from_label,
            is_icon_target_label,
        )

        if is_icon_target_label(raw):
            q, extras, region = icon_visual_query_from_label(raw)
            return q, list(dict.fromkeys(extras + [raw])), region
    except Exception:
        pass

    try:
        from server.services.local.locate.clip_query_plan import is_form_input_label

        if is_form_input_label(raw):
            q, _ = _extract_ui_text_core(raw)
            return q or raw, list(dict.fromkeys([raw, q] if q else [raw])), None
    except Exception:
        # Form-input heuristics are optional. A missing or failing helper must not break normal click text.
        pass

    intent = _classify_login_method_intent(raw)
    if intent == "one_click":
        return (
            "本机号码一键登录",
            ["一键登录", "one click login button", "本机号码", "登录按钮"],
            None,
        )
    if intent and intent in _LOGIN_ICON_ALIAS_GROUPS:
        group = list(_LOGIN_ICON_ALIAS_GROUPS[intent])
        visual_primary = {
            "phone_sms": "smartphone mobile phone outline icon button",
            "wechat": "wechat green chat bubble icon",
            "email_password": "user account password login icon",
            "apple": "apple logo sign in icon",
        }.get(intent, group[0])
        extras = list(group[1:]) + [raw] if raw and raw not in group else list(group[1:])
        if intent == "phone_sms":
            extras.extend(
                [
                    "phone icon",
                    "mobile phone icon",
                    "手机图标",
                    "smartphone icon",
                ]
            )
        return visual_primary, extras, None

    q, _pos_hint = _extract_ui_text_core(raw)
    if not q:
        q = raw

    aliases = [raw, q] if q != raw else [raw]
    return q, list(dict.fromkeys(aliases)), None


def _is_disagree_label(text: str) -> bool:
    t = (text or "").strip()
    return t == "不同意" or t.startswith("不同意")


def _extract_ui_text_core(label: str) -> Tuple[str, Optional[str]]:
    """从「登录页面点击右上角的访客浏览」提取核心文案与方位。"""
    raw = (label or "").strip()
    pos_hint: Optional[str] = None
    if re.search(r"右上角|右上", raw):
        pos_hint = "top_right"
    elif re.search(r"左上角|左上", raw):
        pos_hint = "top_left"
    elif re.search(r"底部|底栏", raw) and not re.search(r"协议|勾选", raw):
        pos_hint = "bottom"

    q = re.sub(r"^(点击|点一下|tap|click|进入|打开)\s*", "", raw, flags=re.I).strip()
    q = re.sub(r"^(登录页(面)?|页面)(上的|中的)?\s*", "", q)
    q = re.sub(r"^(右上角|左上角|右下角|左下角|底部|底栏|顶部)(的)?\s*", "", q)
    q = q.strip("「」『』【】\"' \t的")
    return q or raw, pos_hint


def _in_top_right_band(cx: int, cy: int, screen_w: int, screen_h: int) -> bool:
    return cy <= int(screen_h * 0.32) and cx >= int(screen_w * 0.48)


def _in_top_left_band(cx: int, cy: int, screen_w: int, screen_h: int) -> bool:
    return cy <= int(screen_h * 0.32) and cx <= int(screen_w * 0.52)


def _position_band_ok(
    pos_hint: Optional[str], cx: int, cy: int, screen_w: int, screen_h: int
) -> bool:
    if pos_hint == "top_right":
        return _in_top_right_band(cx, cy, screen_w, screen_h)
    if pos_hint == "top_left":
        return _in_top_left_band(cx, cy, screen_w, screen_h)
    if pos_hint == "bottom":
        return _in_bottom_bar_band(cy, screen_h)
    return True


def _should_try_text_locate_first(label: str) -> bool:
    if not label:
        return False
    try:
        from server.services.local.locate.icon_intent import is_icon_target_label

        if is_icon_target_label(label):
            return False
    except Exception:
        if re.search(r"图标|icon", label, re.I):
            return False
    if _is_toggle_intent(label) or _is_consent_action_label(label):
        return False
    core, _ = _extract_ui_text_core(label)
    return bool(core and re.search(r"[\u4e00-\u9fff]", core) and len(core) <= 24)


def _resolve_text_click_target(
    engine,
    screen_w: int,
    screen_h: int,
    label: str,
) -> Tuple[Optional[Tuple[int, int]], str, str, Optional[Dict[str, Any]]]:
    """短中文文案优先 hierarchy/OCR（CLIP 不适合纯文字链接）。"""
    from server.services.local.locate.spatial import parse_spatial_constraint, point_in_zones

    spatial = parse_spatial_constraint(label)
    query = spatial.core_text or label
    try:
        from driver.agent.Crawl.ui_discovery import (
            discover_clickables_from_hierarchy,
            discover_clickables_ocr,
        )

        hier_pool = list(
            discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=96)
        )
        if spatial.active:
            hier_pool = [
                t
                for t in hier_pool
                if point_in_zones(
                    t.center[0], t.center[1], screen_w, screen_h, spatial.zones
                )
            ]
        pick = _pick_best_text_clickable(query, hier_pool, screen_h=screen_h)
        if pick:
            cx, cy, txt, score, t = pick
            rect = _make_target_rect(t.x, t.y, t.w, t.h, label=txt)
            return (
                (cx, cy),
                "hierarchy",
                f"层级「{txt}」@({cx},{cy}) sim={score:.2f}",
                rect,
            )

        shot = engine.screenshot() if hasattr(engine, "screenshot") else None
        if shot is not None:
            ocr_pool = list(discover_clickables_ocr(shot, screen_w, screen_h, max_items=96))
            if spatial.active:
                ocr_pool = [
                    t
                    for t in ocr_pool
                    if point_in_zones(
                        t.center[0], t.center[1], screen_w, screen_h, spatial.zones
                    )
                ]
            pick = _pick_best_text_clickable(query, ocr_pool, screen_h=screen_h)
            if pick:
                cx, cy, txt, score, t = pick
                rect = _make_target_rect(t.x, t.y, t.w, t.h, label=txt)
                return (
                    (cx, cy),
                    "ocr",
                    f"OCR「{txt}」@({cx},{cy}) sim={score:.2f}",
                    rect,
                )
    except Exception as e:
        SLog.w(TAG, f"text locate failed label={label!r}: {e}")
    return None, "none", f"未找到文本「{query}」", None


def _label_similarity(query: str, candidate: str) -> float:
    """文本贴合度；成对按钮（同意/不同意）按语义区分，不依赖左右位置。"""
    from difflib import SequenceMatcher

    q = (query or "").strip()
    c = (candidate or "").strip()
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in ("同意", "同意并继续") and c in ("同意", "同意并继续"):
        return 0.96
    neg_prefixes = ("不", "勿", "未", "非")
    for neg in neg_prefixes:
        if c.startswith(neg) and len(c) > len(q) and c[len(neg) :] == q:
            return 0.05
        if q.startswith(neg) and len(q) > len(c) and q[len(neg) :] == c:
            return 0.05
    if q in ("同意", "同意并继续") and ("不同意" in c or c.startswith("不同意")):
        return 0.05
    if c in ("同意", "同意并继续") and ("不同意" in q or q.startswith("不同意")):
        return 0.05
    ratio = SequenceMatcher(None, q, c).ratio()
    if (q in c or c in q) and min(len(q), len(c)) <= 4:
        longer = max(len(q), len(c))
        shorter = min(len(q), len(c))
        if longer > shorter + 1:
            ratio = min(ratio, shorter / longer)
    return ratio


def _min_label_similarity(query: str) -> float:
    q = (query or "").strip()
    if len(q) <= 4:
        return 0.82
    if len(q) <= 8:
        return 0.72
    return 0.58


def _pick_consent_agree_clickable(
    query: str,
    candidates: list,
    *,
    screen_h: int = 0,
) -> Optional[Tuple[int, int, str, float, Any]]:
    """consent / 登录确认「同意」「同意并继续」：排除「不同意」，同分时取更靠右。"""
    floor = _min_label_similarity(query)
    matches: List[Tuple[float, int, int, str, Any]] = []
    for t in candidates or []:
        txt = (getattr(t, "label", None) or "").strip()
        if not txt or _is_disagree_label(txt) or _is_legal_bearing_target(txt):
            continue
        if txt not in ("同意", "同意并继续") and not _is_consent_action_label(txt):
            if not (
                query in ("同意", "同意并继续")
                and "同意并继续" in txt
                and len(txt) <= 16
            ):
                continue
        cx, cy = t.center
        score = _label_similarity(query, txt)
        if score < floor:
            continue
        matches.append((score, cx, cy, txt, t))
    if not matches:
        return None
    matches.sort(key=lambda m: (-m[0], -m[1]))
    score, cx, cy, txt, t = matches[0]
    return cx, cy, txt, score, t


def _pick_best_text_clickable(
    query: str,
    candidates: list,
    *,
    screen_h: int = 0,
    max_label_len: int = 0,
    consent_modal: bool = False,
) -> Optional[Tuple[int, int, str, float, Any]]:
    """从可点击候选中按文本贴合度选最高分。"""
    if query in ("同意", "同意并继续") or consent_modal:
        hit = _pick_consent_agree_clickable(query, candidates, screen_h=screen_h)
        if hit:
            return hit
    best: Optional[Tuple[int, int, str, float, Any]] = None
    best_score = 0.0
    floor = _min_label_similarity(query)
    for t in candidates or []:
        txt = (getattr(t, "label", None) or "").strip()
        if not txt or _is_legal_bearing_target(txt):
            continue
        if max_label_len and len(txt) > max_label_len:
            continue
        cx, cy = t.center
        score = _label_similarity(query, txt)
        if score < floor:
            continue
        if score > best_score:
            best_score = score
            best = (cx, cy, txt, score, t)
    return best


def _match_target_label(label: str, target_label: str) -> bool:
    a = (label or "").strip()
    b = (target_label or "").strip()
    if not a or not b:
        return False
    if _is_legal_bearing_target(b) and _is_login_related_label(a):
        return False
    # 「同意」是「不同意」的子串，禁止子串误匹配左侧按钮
    if _is_consent_action_label(a) and _is_disagree_label(b):
        return False
    if _is_consent_action_label(a):
        return b in ("同意", "同意并继续")
    for va in _label_variants(a):
        for vb in _label_variants(b):
            if va == vb:
                return True
            if _is_legal_bearing_target(b) or _is_legal_bearing_target(vb):
                continue
            if _is_consent_action_label(a) and _is_disagree_label(vb):
                continue
            if va in vb or vb in va:
                if a in ("同意", "同意并继续") and "不同意" in b:
                    continue
                return True
    return False


def _icon_names_match_label(label: str, names: List[str]) -> bool:
    for name in names:
        if name and _match_target_label(label, name):
            return True
    try:
        from server.services.local.locate.icon_intent import icon_name_aliases_from_label

        aliases = icon_name_aliases_from_label(label)
        for name in names:
            n = (name or "").strip()
            if not n:
                continue
            if any(a == n or a in n or n in a for a in aliases):
                return True
    except Exception:
        intent = _classify_login_method_intent(label)
        if intent and intent != "one_click":
            group = _LOGIN_ICON_ALIAS_GROUPS.get(intent, ())
            for name in names:
                n = (name or "").strip()
                if not n:
                    continue
                if any(g in n or n in g for g in group):
                    return True
    return False


def _resolve_icon_target(
    label: str,
    icon_targets: Optional[List[Dict[str, Any]]],
    *,
    screen_w: int = 0,
    screen_h: int = 0,
) -> Optional[Tuple[int, int, str, Dict[str, Any]]]:
    from server.services.figma_icon_service import scale_icon_target_rect

    for t in icon_targets or []:
        names = [(t.get("name") or "").strip()]
        for a in t.get("aliases") or []:
            if a:
                names.append(str(a).strip())
        if not _icon_names_match_label(label, names):
            continue
        name = names[0]
        if screen_w > 0 and screen_h > 0:
            x, y, w, h = scale_icon_target_rect(t, screen_w, screen_h)
        else:
            x, y = int(t.get("x") or 0), int(t.get("y") or 0)
            w, h = int(t.get("w") or 0), int(t.get("h") or 0)
        if w <= 0 or h <= 0:
            continue
        cx = x + w // 2
        cy = y + h // 2
        if cx > 0 and cy > 0:
            rect = _make_target_rect(x, y, w or 48, h or 48, label=name)
            src = "Figma" if "figma_norm" in (t.get("note") or "") else "图标库"
            return cx, cy, f"{src}目标「{name}」@({cx},{cy})", rect
    return None


def _is_legal_link_target(label: str) -> bool:
    t = (label or "").strip()
    return any(k in t for k in ("用户协议", "隐私条款", "隐私政策", "服务协议"))


def _is_legal_bearing_target(label: str) -> bool:
    """含协议链接或 consent 正文的长文案，不能作为「同意」等短指令的点击目标。"""
    t = (label or "").strip()
    if not t:
        return False
    if _is_legal_link_target(t):
        return True
    if len(t) > 10 and any(k in t for k in ("《", "》", "阅读", "点击", "条款", "协议", "隐私")):
        return True
    return False


def _is_consent_action_label(label: str) -> bool:
    t = (label or "").strip()
    return t in ("同意", "同意并继续", "接受", "我知道了", "继续")


def _is_toggle_intent(label: str) -> bool:
    from server.services.local.locate.toggle_locate_service import is_toggle_intent

    return is_toggle_intent(label)


def _is_checkbox_intent(label: str) -> bool:
    return _is_toggle_intent(label)


def _dedupe_consecutive_toggle_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并连续重复的勾选步骤（如误拆成「底部协议勾选框」+「框勾选框」）。"""
    out: List[Dict[str, Any]] = []
    for st in steps or []:
        if not out:
            out.append(st)
            continue
        prev = out[-1]
        if st.get("kind") != "click" or prev.get("kind") != "click":
            out.append(st)
            continue
        pl = (prev.get("label") or prev.get("summary") or "").strip()
        sl = (st.get("label") or st.get("summary") or "").strip()
        if not (_is_toggle_intent(pl) and _is_toggle_intent(sl)):
            out.append(st)
            continue
        if sl in pl or pl in sl or (sl.endswith("框") and len(sl) <= 6 and "协议" in pl):
            continue
        out.append(st)
    return out


def _try_clip_resolve(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    label: str,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    region: Optional[str] = None,
) -> Optional[Tuple[Optional[Tuple[int, int]], str, str, Optional[Dict[str, Any]]]]:
    if not label:
        return None
    try:
        from server.core.vision.clip_service import clip_enabled
        from server.services.local.locate.clip_locate_service import try_clip_locate

        if not clip_enabled():
            SLog.i(TAG, f"CLIP skip label={label!r} (CLIP_ENABLED=0)")
            return None

        query, aliases, _region_hint = _clip_search_params(label)
        _ = region
        SLog.i(
            TAG,
            f"CLIP try label={label!r} query={query!r} region=full "
            f"screen={screen_w}x{screen_h}",
        )
        clip_hit = try_clip_locate(
            engine,
            screen_w,
            screen_h,
            label=label,
            query=query,
            aliases=aliases,
            icon_targets=icon_targets,
            region="full",
        )
        if clip_hit[0] is not None:
            SLog.i(TAG, f"CLIP hit label={label!r} method={clip_hit[1]} {clip_hit[2]}")
            return clip_hit
        SLog.i(TAG, f"CLIP miss label={label!r} {clip_hit[2]}")
    except Exception as e:
        SLog.w(TAG, f"CLIP locate failed label={label!r}: {e}")
    return None


def _resolve_click_target(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    label: str = "",
    x: int = 0,
    y: int = 0,
    coords_explicit: bool = False,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    exact_label: bool = False,
    login_icon_order: Optional[Dict[str, int]] = None,
    prefer_text_locate: bool = False,
) -> Tuple[Optional[Tuple[int, int]], str, str, Optional[Dict[str, Any]]]:
    """
    解析点击目标，返回 (position|None, method, detail, target_rect)。
    优先级：显式坐标 → consent 安全定位 → toggle → CLIP(全屏/区域) → 图标库 → hierarchy/OCR → Tab → 无障碍 label
    method: clip_* | hierarchy | ocr | icon_target | coordinate | label
    """
    from server.services.executor.locate_debug import stash_locate_debug

    if label:
        try:
            from server.services.local.overlay.overlay_guard_service import is_screen_blocked
            from server.services.local.navigation.page_navigation_service import is_overlay_dismiss_target_label

            if is_screen_blocked(engine) and not is_overlay_dismiss_target_label(label):
                from server.services.local.overlay.overlay_guard_service import (
                    blocked_overlay_message,
                    detect_blocking_overlay,
                )

                ov = detect_blocking_overlay(engine) or {}
                blocked_msg = blocked_overlay_message(engine) or "当前屏被阻塞弹窗占用"
                stash_locate_debug({
                    "query": label,
                    "blocked_overlay": True,
                    "overlay_type": ov.get("type") or "",
                    "detail": blocked_msg,
                })
                return (
                    None,
                    "blocked_overlay",
                    blocked_msg,
                    None,
                )
        except Exception:
            pass

        intent = _classify_login_method_intent(label)
        if intent == "one_click":
            try:
                from server.services.local.navigation.page_navigation_service import (
                    _collect_ocr_text_only,
                    _screen_is_login_home,
                    _screen_is_login_surface,
                )

                blob = (_collect_ocr_text_only(engine) or "").strip()
                if blob and not _screen_is_login_surface(blob) and not _screen_is_login_home(blob):
                    stash_locate_debug({
                        "query": label,
                        "wrong_page": True,
                        "detail": "不在登录页，跳过一键登录定位",
                    })
                    return None, "wrong_page", "当前不在登录页", None
            except Exception:
                pass

    if coords_explicit and x > 0 and y > 0:
        half = 24
        rect = _make_target_rect(x - half, y - half, half * 2, half * 2, label=label or f"({x},{y})")
        return (x, y), "coordinate", f"坐标({x},{y})", rect

    try:
        from server.services.local.locate.resolver import _locate_arbitrator_enabled, resolve_locate_target

        if label and _locate_arbitrator_enabled():
            lr = resolve_locate_target(
                engine,
                screen_w,
                screen_h,
                label=label,
                icon_targets=icon_targets,
            )
            stash_locate_debug(lr.debug)
            if lr.ok:
                return lr.position, lr.method, lr.detail, lr.target_rect
    except Exception as e:
        SLog.w(TAG, f"locate arbitrator failed label={label!r}: {e}")

    if label and _should_try_text_locate_first(label):
        text_hit = _resolve_text_click_target(engine, screen_w, screen_h, label)
        if text_hit[0]:
            return text_hit

    clip_attempted = False
    if label and not _is_toggle_intent(label):
        clip_attempted = True
        clip_hit = _try_clip_resolve(
            engine, screen_w, screen_h, label=label, icon_targets=icon_targets
        )
        if clip_hit and clip_hit[0]:
            return clip_hit

    if label:
        icon_hit = _resolve_icon_target(
            label, icon_targets, screen_w=screen_w, screen_h=screen_h
        )
        if icon_hit:
            cx, cy, detail, rect = icon_hit
            return (cx, cy), "icon_target", detail, rect

    if label and _is_segment_tab_query(label):
        seg_hit = _resolve_segment_tab_target(
            label, engine, screen_w, screen_h, icon_targets=icon_targets
        )
        if seg_hit[0]:
            return seg_hit

    try:
        from driver.agent.Crawl.ui_discovery import (
            discover_clickables_from_hierarchy,
            discover_clickables_ocr,
        )

        shot = None
        tab_query = label and (
            _is_bottom_tab_intent(label) or _is_probable_bottom_tab_query(label)
        )
        if tab_query:
            tab_hit = _resolve_bottom_tab_target(
                label, engine, screen_w, screen_h, icon_targets=icon_targets
            )
            if tab_hit[0]:
                return tab_hit
            if _is_bottom_tab_intent(label):
                return tab_hit

        if label and not _is_consent_action_label(label):
            try:
                from server.core.vision.clip_service import clip_enabled

                from server.services.local.locate.icon_intent import is_icon_target_label

                icon_visual = is_icon_target_label(label)
                if clip_attempted and clip_enabled() and icon_visual:
                    return (
                        None,
                        "none",
                        f"CLIP 未找到「{label}」，已跳过文本子串兜底以免误点",
                        None,
                    )
            except Exception:
                pass
            tab_only = _is_probable_bottom_tab_query(label)
            hier_pool = list(
                discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=48)
            )
            pick = _pick_best_text_clickable(
                label,
                hier_pool,
                screen_h=screen_h,
            )
            if pick:
                cx, cy, txt, score, t = pick
                rect = _make_target_rect(t.x, t.y, t.w, t.h, label=txt)
                return (
                    cx,
                    cy,
                ), "hierarchy", f"层级「{txt}」@({cx},{cy}) sim={score:.2f}", rect

            shot = None
            if hasattr(engine, "screenshot"):
                try:
                    shot = engine.screenshot()
                except Exception:
                    shot = None
            if shot is not None:
                ocr_pool = list(
                    discover_clickables_ocr(shot, screen_w, screen_h, max_items=24)
                )
                pick = _pick_best_text_clickable(
                    label,
                    ocr_pool,
                    screen_h=screen_h,
                )
                if pick:
                    cx, cy, txt, score, t = pick
                    rect = _make_target_rect(t.x, t.y, t.w, t.h, label=txt)
                    return (
                        cx,
                        cy,
                    ), "ocr", f"OCR「{txt}」@({cx},{cy}) sim={score:.2f}", rect
            if tab_only:
                return _resolve_bottom_tab_target(
                    label, engine, screen_w, screen_h, icon_targets=icon_targets
                )
    except Exception as e:
        SLog.w(TAG, f"resolve click target failed: {e}")

    try:
        from server.services.local.locate.resolver import _locate_arbitrator_enabled

        arbitrator_on = _locate_arbitrator_enabled()
    except Exception:
        arbitrator_on = False

    if (
        label
        and hasattr(engine, "click_by_label")
        and not _is_toggle_intent(label)
        and not arbitrator_on
    ):
        use_exact = (
            exact_label
            or _is_probable_bottom_tab_query(label)
            or not any(k in label for k in ("用户协议", "隐私", "同意", "条款"))
        )
        for variant in _label_variants(label):
            fn = getattr(engine, "click_by_label")
            try:
                clicked = fn(variant, exact_only=use_exact)
            except TypeError:
                clicked = fn(variant)
            if clicked:
                if _is_legal_link_target(variant) and _is_login_related_label(label):
                    SLog.w(TAG, f"blocked legal link click variant={variant!r} for={label!r}")
                    continue
                rect = None
                try:
                    from driver.agent.Crawl.ui_discovery import discover_clickables_from_hierarchy

                    for t in discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=48):
                        if _match_target_label(label, t.label):
                            rect = _make_target_rect(t.x, t.y, t.w, t.h, label=t.label)
                            break
                except Exception:
                    pass
                return None, "label", f"无障碍文案「{variant}」", rect

    return None, "none", "未找到可点击目标", None






def _nav_route(text: str) -> Optional[Dict[str, str]]:
    t = text.lower()
    rules = [
        (("应用列表", "应用", "首页", "dashboard", "apps", "app list"), "AppList", "/report/apps"),
        (("设备", "device"), "DeviceManage", "/device"),
        (("定时", "schedule", "任务"), "Schedule", "/schedule"),
        (("时间线", "timeline", "日志"), "Timeline", "/timeline"),
        (("对话", "copilot", "dialogue", "助手"), "Dialogue", "/dialogue"),
        (("设置", "settings", "配置中心"), "SettingsHub", "/settings/hub"),
        (("资源", "resource"), "ResourceList", "/resources"),
    ]
    for keys, name, path in rules:
        if any(k in t for k in keys):
            return {"name": name, "path": path}
    return None


_PACKAGE_RE = re.compile(r"(com\.[a-zA-Z0-9_.]+)")


def _normalize_app_token(token: str) -> str:
    t = (token or "").strip()
    t = re.sub(r"(?:应用|软件|程序|app|APP|应用程序)$", "", t, flags=re.I).strip()
    return t


def _looks_like_package(token: str) -> bool:
    t = (token or "").strip()
    return bool(t) and ("." in t) and not re.search(r"[\u4e00-\u9fff]", t)


def _name_match_score(query: str, candidate: str) -> int:
    q = _normalize_app_token(query).lower()
    c = _normalize_app_token(candidate).lower()
    if not q or not c:
        return 0
    if q == c:
        return 100
    if q in c:
        return 80 + int(len(q) / max(len(c), 1) * 15)
    if c in q:
        return 70 + int(len(c) / max(len(q), 1) * 15)
    return 0


def _pkg_match_score(query: str, package: str) -> int:
    q = _normalize_app_token(query).lower().replace(" ", "")
    p = (package or "").lower()
    if not q or not p:
        return 0
    if q == p:
        return 95
    if q in p:
        return 55
    seg = p.rsplit(".", 1)[-1]
    if q == seg or q in seg or seg in q:
        return 65
    return 0


def _package_from_env(env_raw: Any) -> str:
    """从 app.env 或 project.env 解析 Android 包名。"""
    try:
        from server.services.project_env import normalize_project_env, profile_snapshot

        snap = profile_snapshot(normalize_project_env(env_raw or {}))
        return ((snap.get("android") or {}).get("package") or "").strip()
    except Exception:
        return ""


def _package_for_app_record(app) -> str:
    """应用包名：优先 app.env，否则继承所属项目的 project.env。"""
    pkg = _package_from_env(getattr(app, "env", None))
    if pkg:
        return pkg
    project = getattr(app, "project", None)
    if project:
        return _package_from_env(getattr(project, "env", None))
    return ""


def _resolve_app_from_db(name: str, *, app_id: Optional[str] = None) -> Optional[Tuple[str, str, str]]:
    """从 MiniOrange 项目/应用库解析 (package, source, display_name)。"""
    try:
        from server.core.database import SessionLocal
        from server.models.project import App, Project
        from sqlalchemy.orm import joinedload

        session = SessionLocal()
        try:
            best_pkg = ""
            best_name = ""
            best_score = 0

            app_query = session.query(App).options(joinedload(App.project))
            if app_id:
                app_query = app_query.filter(App.id == str(app_id))
            for app in app_query.all():
                pkg = _package_for_app_record(app)
                project_name = app.project.name if app.project else ""
                score = max(
                    _name_match_score(name, app.name),
                    _name_match_score(name, project_name),
                    _pkg_match_score(name, pkg),
                )
                if score > best_score:
                    best_score = score
                    best_pkg = pkg
                    best_name = app.name or project_name

            for project in session.query(Project).all():
                pkg = _package_from_env(project.env)
                score = max(_name_match_score(name, project.name), _pkg_match_score(name, pkg))
                if score > best_score:
                    best_score = score
                    best_pkg = pkg
                    best_name = project.name

            if best_score >= 55 and best_pkg:
                return best_pkg, "db", best_name
            if best_score >= 55 and not best_pkg:
                SLog.w(
                    TAG,
                    f"app name matched「{best_name}」but package empty; "
                    "check project env android.package",
                )
        finally:
            session.close()
    except Exception as e:
        SLog.w(TAG, f"resolve app from db failed: {e}")
    try:
        from server.services.local.locate.app_packages import resolve_known_app_by_alias

        known = resolve_known_app_by_alias(name)
        if known and known.android_packages:
            return known.android_packages[0], "registry", known.name
    except Exception:
        pass
    return None


def _resolve_app_from_device(sn: str, name: str) -> Optional[Tuple[str, str, str]]:
    """从手机已安装应用解析 (package, source, display_name)。"""
    if not sn:
        return None
    try:
        import uiautomator2 as u2

        d = u2.connect(str(sn))
        out = d.shell("pm list packages -3").output or ""
        pkgs = [
            line.replace("package:", "").strip()
            for line in out.splitlines()
            if line.strip().startswith("package:")
        ]
        best_pkg = ""
        best_label = ""
        best_score = 0
        for pkg in pkgs:
            label = ""
            try:
                info = d.app_info(pkg) or {}
                label = (info.get("label") or info.get("name") or "").strip()
            except Exception:
                label = ""
            score = max(_name_match_score(name, label), _pkg_match_score(name, pkg))
            if score > best_score:
                best_score = score
                best_pkg = pkg
                best_label = label or pkg
        if best_score >= 55 and best_pkg:
            return best_pkg, "device", best_label
    except Exception as e:
        SLog.w(TAG, f"resolve app from device failed: {e}")
    return None


def resolve_app_package(
    name: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
) -> Tuple[Optional[str], str, str]:
    """
    应用名/别名 → 包名。
    返回 (package|None, source, display_name)。
    """
    ctx = context or {}
    token = _normalize_app_token(name)
    if not token:
        return None, "", ""

    if _looks_like_package(token):
        return token, "package", token

    pkg_match = _PACKAGE_RE.search(token)
    if pkg_match:
        pkg = pkg_match.group(1)
        return pkg, "package", pkg

    ctx_pkg = (ctx.get("package") or ctx.get("android_package") or "").strip()
    ctx_name = (ctx.get("app_name") or ctx.get("appName") or "").strip()
    if ctx_pkg and (not ctx_name or _name_match_score(token, ctx_name) >= 55):
        return ctx_pkg, "context", ctx_name or ctx_pkg

    db_hit = _resolve_app_from_db(token, app_id=ctx.get("app_id") or ctx.get("appId"))
    if db_hit:
        return db_hit

    device_hit = _resolve_app_from_device(sn, token) if sn else None
    if device_hit:
        return device_hit

    return None, "", ""


def _extract_app_identifier(raw: str, operation: str) -> str:
    quoted = re.search(r"[「『\"']([^」』\"']+)[」』\"']", raw)
    if quoted:
        return _normalize_app_token(quoted.group(1))
    if operation == "open":
        m = re.search(r"(?:打开|启动|open|launch)\s*(.+)$", raw, re.I)
    else:
        m = re.search(r"(?:关闭|退出|关掉|关|close|kill|force[- ]?stop)\s*(.+)$", raw, re.I)
    if not m:
        return ""
    return _normalize_app_token(m.group(1))


def _plan_app_action(
    raw: str,
    operation: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
) -> Optional[Dict[str, Any]]:
    """operation: open | close"""
    pkg_match = _PACKAGE_RE.search(raw)
    if pkg_match:
        pkg = pkg_match.group(1)
        display = pkg
        source = "package"
    else:
        name = _extract_app_identifier(raw, operation)
        if not name:
            return None
        pkg, source, display = resolve_app_package(name, sn=sn, context=context)
        if not pkg:
            verb = "打开" if operation == "open" else "关闭"
            return {
                "error": (
                    f"未找到应用「{name}」对应的包名。"
                    f"请在项目「运行环境」中配置 Android 包名，或确认手机已安装该应用。"
                ),
                "reply_hint": f"{verb} {name}",
            }

    is_open = operation == "open"
    kind = "open_app" if is_open else "close_app"
    op = "start" if is_open else "close"
    verb = "启动" if is_open else "关闭"
    label = display or pkg
    src_hint = {"db": "项目环境", "device": "本机", "context": "上下文", "package": "包名"}.get(source, source)
    return {
        "step": {
            "kind": kind,
            "nodeCode": "public/window",
            "platform": "mobile",
            "data": {
                "operation": op,
                "target_mobile": pkg,
                "restart": is_open,
                "platform": "mobile",
            },
            "summary": f"{verb}应用 {label} ({pkg})",
            "app_name": label,
            "package": pkg,
            "resolve_source": source,
        },
        "reply": f"将{verb}「{label}」→ {pkg}（{src_hint}）",
    }


# 拆分前保护完整短语，避免「同意并继续」被「并」切成两段
_COMPOUND_PHRASES = (
    "同意并继续",
    "不同意",
    "切换到微信app, 并打开登录页面",
    "切换到微信app，并打开登录页面",
)

# 多指令拆分：标点 / 连接词 / 连续动词
_SPLIT_DELIM_RE = re.compile(
    r"(?:"
    r"然后|接着|之后|接下来|再然后|然后再|并且|并|"
    r"and then|then|after that|"
    r"[,，;；\n|]|"
    r"→|->"
    r")+",
    re.I,
)
_VERB_BOUNDARY_RE = re.compile(
    r"(?=(?:"
    r"打开|启动|关闭|退出|关掉|"
    r"输入(?!框)|填写|填入|勾选|勾上|"
    r"点击|点一下|"
    r"上滑|下滑|左滑|右滑|滑动|滑一下|"
    r"截图|截屏|等待|返回|后退|"
    r"open|launch|close|kill|input|type|click|tap|swipe|screenshot|wait|back"
    r"))",
    re.I,
)
_INPUT_TEXT_RE = re.compile(
    r"(?:输入|填写|填入)\s*(?:手机号|手机|电话|号码|验证码)?\s*(\d{4,15})",
    re.I,
)
_INPUT_COLON_RE = re.compile(
    r"^(?:输入|填写|填入)\s*(?:到\s*)?(?:[「『\"']?([^」』\"'：:]+)[」』\"']?\s*)?[:：]\s*(.+)$",
    re.I,
)
_CLICK_INPUT_PAIR_RE = re.compile(
    r"(?:点击|点一下)\s*"
    r"(?:[「『\"'](?P<field_q>[^」』\"']+)[」』\"']|(?P<field_p>[^,，]+?))"
    r"(?:输入框)?"
    r"\s*[,，]\s*"
    r"输入\s*[:：]\s*"
    r"(?:[「『\"'](?P<val_q>[^」』\"']+)[」』\"']|(?P<val_p>[^\s,，;；]+))",
    re.I,
)
_CLICK_FIELD_SEG_RE = re.compile(r"^(?:点击|点一下)\s*.+", re.I)
_NUMBERED_STEP_RE = re.compile(r"(?:^|\s)\d+[.、)\）]\s*")


def _field_click_label(field: str) -> str:
    name = (field or "").strip().strip("「」\"'")
    if not name:
        return "输入框"
    if name.endswith("输入框"):
        return name
    return f"{name}输入框"


def _field_hint_name(field: str) -> str:
    name = (field or "").strip().strip("「」\"'")
    return re.sub(r"输入框$", "", name).strip() or name


def _plan_click_input_pairs(segment: str) -> Optional[List[Dict[str, Any]]]:
    """
    解析「点击账号输入框,输入: xxx」链式片段（可重复多对）。
    产出 click + input(bind_last_click) 原子步骤。
    """
    text = (segment or "").strip()
    if not text:
        return None
    matches = list(_CLICK_INPUT_PAIR_RE.finditer(text))
    if not matches:
        return None

    steps: List[Dict[str, Any]] = []
    for m in matches:
        field_raw = (m.group("field_q") or m.group("field_p") or "").strip()
        value = (m.group("val_q") or m.group("val_p") or "").strip().strip("「」\"'")
        if not field_raw or not value:
            return None
        click_label = _field_click_label(field_raw)
        hint = _field_hint_name(field_raw)
        steps.append(
            {
                "kind": "click",
                "x": 0,
                "y": 0,
                "label": click_label,
                "coords_explicit": False,
                "summary": f"点击「{click_label}」",
            }
        )
        steps.append(
            {
                "kind": "input",
                "text": value,
                "field_hint": hint,
                "bind_last_click": True,
                "summary": f"输入{hint} {value}",
            }
        )
    return steps or None


def _merge_click_input_segment_pairs(segments: List[str]) -> List[str]:
    """合并被逗号拆开的「点击X输入框」+「输入: y」为单段，供模板解析。"""
    merged: List[str] = []
    i = 0
    while i < len(segments):
        seg = (segments[i] or "").strip()
        if i + 2 < len(segments):
            mid = (segments[i + 1] or "").strip()
            nxt = (segments[i + 2] or "").strip()
            if (
                _CLICK_FIELD_SEG_RE.match(seg)
                and mid == "输入框"
                and _INPUT_COLON_RE.match(nxt)
            ):
                merged.append(f"{seg}{mid},{nxt}")
                i += 3
                continue
        if i + 1 < len(segments):
            nxt = (segments[i + 1] or "").strip()
            if _CLICK_FIELD_SEG_RE.match(seg) and _INPUT_COLON_RE.match(nxt):
                merged.append(f"{seg},{nxt}")
                i += 2
                continue
        merged.append(seg)
        i += 1
    return merged


def _normalize_segment(segment: str) -> str:
    seg = (segment or "").strip()
    seg = re.sub(r"^(?:再|然后|接着|并|并且|接下来)\s*", "", seg, flags=re.I).strip()
    return seg


_ORPHAN_TOGGLE_SEG_RE = re.compile(r"^(?:勾选框|框勾选框|选框)$")
_CLICK_ORPHAN_SEG_RE = re.compile(r"^(?:点击|点一下|tap|click)$", re.I)


def _coalesce_split_segments(parts: List[str]) -> List[str]:
    """合并被误拆的「勾选框」后缀，避免「勾选底部协议」+「勾选框」两步。"""
    out: List[str] = []
    for seg in parts:
        s = (seg or "").strip()
        if not s:
            continue
        if out and _CLICK_ORPHAN_SEG_RE.match(out[-1]) and not _CLICK_ORPHAN_SEG_RE.match(s):
            out[-1] = f"{out[-1]}{s}"
            continue
        if out and _ORPHAN_TOGGLE_SEG_RE.match(s):
            out[-1] = f"{out[-1]}{s}"
            continue
        if out and s.startswith("勾选框") and len(s) <= 8:
            out[-1] = f"{out[-1]}{s}"
            continue
        if out and s == "输入框" and re.search(r"(?:点击|点一下)", out[-1]):
            out[-1] = f"{out[-1]}{s}"
            continue
        if out and s in ("继续", "并继续") and re.search(r"同意", out[-1]):
            out[-1] = f"{out[-1]}并继续" if s == "继续" else f"{out[-1]}{s}"
            continue
        out.append(s)
    return out


def _split_commands(text: str) -> List[str]:
    """将一条用户输入拆成多个可独立规划的子指令。"""
    raw = (text or "").strip()
    if not raw:
        return []

    protected: Dict[str, str] = {}

    def _protect(match: re.Match) -> str:
        key = f"__MO_{len(protected)}__"
        protected[key] = match.group(0)
        return key

    shielded = _PACKAGE_RE.sub(_protect, raw)
    shielded = re.sub(r"\d{2,4}\s*[,，]\s*\d{2,4}", _protect, shielded)
    shielded = re.sub(r"[\w.+-]+@[\w.-]+\.\w+", _protect, shielded)
    for phrase in _COMPOUND_PHRASES:
        if phrase in shielded:
            key = f"__CP_{len(protected)}__"
            protected[key] = phrase
            shielded = shielded.replace(phrase, key)

    parts: List[str] = []
    if _NUMBERED_STEP_RE.search(shielded):
        chunks = _NUMBERED_STEP_RE.split(shielded)
        parts = [c.strip() for c in chunks if c and c.strip()]
    else:
        chunks = _SPLIT_DELIM_RE.split(shielded)
        parts = [c.strip() for c in chunks if c and c.strip()]

    if len(parts) <= 1:
        parts = [shielded]

    expanded: List[str] = []
    for part in parts:
        subs = [s.strip() for s in _VERB_BOUNDARY_RE.split(part) if s and s.strip()]
        expanded.extend(subs if subs else [part])

    restored: List[str] = []
    seen = set()
    for part in expanded:
        seg = part
        for key, val in protected.items():
            seg = seg.replace(key, val)
        seg = _normalize_segment(seg)
        if seg and seg not in seen:
            seen.add(seg)
            restored.append(seg)
    merged = _coalesce_split_segments(restored)
    return merged or [raw]


def _inject_step_waits(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """启动应用后若紧跟点击/滑动，自动插入短暂等待。"""
    if not steps:
        return steps
    out: List[Dict[str, Any]] = []
    for i, step in enumerate(steps):
        out.append(step)
        if step.get("kind") != "open_app" or i + 1 >= len(steps):
            continue
        nxt = steps[i + 1]
        if nxt.get("kind") not in ("click", "swipe"):
            continue
        if out and out[-1].get("kind") == "ability" and out[-1].get("nodeCode") == "cfs/sleep":
            continue
        out.append({
            "kind": "ability",
            "nodeCode": "cfs/sleep",
            "platform": "common",
            "data": {"seconds": 2},
            "summary": "等待应用就绪 2 秒",
        })
    return out


def _plan_segment(
    raw: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
) -> Dict[str, Any]:
    """单条子指令 → steps + reply_parts + errors。"""
    segment = _normalize_segment(raw)
    if not segment:
        return {"steps": [], "reply_parts": [], "errors": []}

    pair_steps = _plan_click_input_pairs(segment)
    if pair_steps:
        return {
            "steps": pair_steps,
            "reply_parts": [s.get("summary") or "" for s in pair_steps if s.get("summary")],
            "errors": [],
        }

    try:
        from server.services.local.plan.copilot_semantic import semantic_split_segment

        expanded = semantic_split_segment(segment, sn=sn, context=context)
        if expanded is not None:
            if len(expanded) == 1 and _normalize_segment(expanded[0]) == segment:
                expanded = None
        if expanded is not None:
            steps: List[Dict[str, Any]] = []
            reply_parts: List[str] = []
            errors: List[str] = []
            if not expanded:
                errors.append(f"无法从当前界面解析 Tab 列表，已跳过口语步骤：{segment}")
                return {"steps": steps, "reply_parts": reply_parts, "errors": errors}
            for sub in expanded:
                sub_plan = _plan_segment(sub, sn=sn, context=context)
                steps.extend(sub_plan.get("steps") or [])
                reply_parts.extend(sub_plan.get("reply_parts") or [])
                errors.extend(sub_plan.get("errors") or [])
            if steps:
                reply_parts.insert(0, f"语义展开 {len(expanded)} 步")
            return {"steps": steps, "reply_parts": reply_parts, "errors": errors}
    except Exception as e:
        SLog.w(TAG, f"semantic split skipped: {e}")

    steps: List[Dict[str, Any]] = []
    reply_parts: List[str] = []
    errors: List[str] = []

    open_intent = bool(re.search(r"打开|启动|open|launch", segment, re.I))
    close_intent = bool(re.search(r"关闭|退出|close|kill|force[- ]?stop", segment, re.I))
    if open_intent and not close_intent:
        app_plan = _plan_app_action(segment, "open", sn=sn, context=context)
        if app_plan:
            if app_plan.get("error"):
                errors.append(app_plan["error"])
            else:
                steps.append(app_plan["step"])
                reply_parts.append(app_plan["reply"])
    elif close_intent and not open_intent:
        app_plan = _plan_app_action(segment, "close", sn=sn, context=context)
        if app_plan:
            if app_plan.get("error"):
                errors.append(app_plan["error"])
            else:
                steps.append(app_plan["step"])
                reply_parts.append(app_plan["reply"])

    colon_input = _INPUT_COLON_RE.match(segment.strip())
    if colon_input:
        field_hint = (colon_input.group(1) or "").strip()
        value = (colon_input.group(2) or "").strip().strip("「」\"'")
        if value:
            steps.append({
                "kind": "input",
                "text": value,
                "field_hint": field_hint,
                "summary": f"输入{field_hint or '文本'} {value}",
            })
            reply_parts.append(steps[-1]["summary"])
    else:
        input_m = _INPUT_TEXT_RE.search(segment)
        if input_m:
            value = input_m.group(1)
            field_hint = "手机号" if re.search(r"手机|电话|号码", segment) else ""
            steps.append({
                "kind": "input",
                "text": value,
                "field_hint": field_hint,
                "summary": f"输入{field_hint or '文本'} {value}",
            })
            reply_parts.append(steps[-1]["summary"])

    toggle_m = re.search(
        r"(勾选|勾上|选中|单选)\s*[「『\"']?([^」』\"'\n,，]+)[」』\"']?",
        segment,
        re.I,
    )
    if toggle_m:
        hint = toggle_m.group(2).strip() or "勾选框"
        if not re.search(r"勾选框|复选框|单选框|checkbox|radio", hint, re.I):
            hint = f"{hint}勾选框"
        steps.append({
            "kind": "click",
            "x": 0,
            "y": 0,
            "label": hint,
            "coords_explicit": False,
            "summary": f"勾选「{hint}」",
        })
        reply_parts.append(steps[-1]["summary"])
    elif re.search(r"勾选|勾上|勾上选|check", segment, re.I):
        hint = "勾选框"
        steps.append({
            "kind": "click",
            "x": 0,
            "y": 0,
            "label": hint,
            "coords_explicit": False,
            "summary": f"勾选「{hint}」",
        })
        reply_parts.append(steps[-1]["summary"])

    coord = re.search(r"(\d{2,4})\s*[,，]\s*(\d{2,4})", segment)
    label_m = re.search(r"[「『\"']([^」』\"']+)[」』\"']", segment) or re.search(
        r"点击\s*(.+?)(?:\s*[,，;；]|$)", segment
    )
    if re.search(r"点击|点一下|tap|click", segment, re.I):
        label = label_m.group(1).strip() if label_m else ""
        if re.search(r"同意并继续", segment):
            label = "同意并继续"
        elif re.search(r"同意并继续", (label or "")):
            label = "同意并继续"
        coords_explicit = bool(coord)
        if coord:
            x, y = int(coord.group(1)), int(coord.group(2))
        else:
            x, y = _DEFAULT_CLICK_XY
        if label or coords_explicit:
            steps.append({
                "kind": "click",
                "x": x,
                "y": y,
                "label": label,
                "coords_explicit": coords_explicit,
                "summary": f"点击「{label or (f'{x},{y}' if coords_explicit else '目标')}」",
            })
            reply_parts.append(steps[-1]["summary"])

    swipe_dir = None
    if re.search(r"上滑|向上滑|swipe\s*up", segment, re.I):
        swipe_dir = "up"
    elif re.search(r"下滑|向下滑|swipe\s*down", segment, re.I):
        swipe_dir = "down"
    elif re.search(r"左滑|向左", segment, re.I):
        swipe_dir = "left"
    elif re.search(r"右滑|向右", segment, re.I):
        swipe_dir = "right"
    elif re.search(r"滑动|滑一下|scroll|swipe", segment, re.I):
        swipe_dir = "up"
    if swipe_dir:
        steps.append({"kind": "swipe", "direction": swipe_dir, "summary": f"滑动 {swipe_dir}"})
        reply_parts.append(f"滑动 {swipe_dir}")

    if re.search(r"截图|截屏|screenshot", segment, re.I):
        steps.append({
            "kind": "ability",
            "nodeCode": "tools/screenshot",
            "platform": "mobile",
            "data": {"platform": "mobile"},
            "summary": "截图",
        })
        reply_parts.append("截图")

    if re.search(r"等待|wait|sleep", segment, re.I):
        sec_m = re.search(r"(\d+)\s*秒", segment)
        sec = int(sec_m.group(1)) if sec_m else 2
        steps.append({
            "kind": "ability",
            "nodeCode": "cfs/sleep",
            "platform": "common",
            "data": {"seconds": sec},
            "summary": f"等待 {sec} 秒",
        })
        reply_parts.append(f"等待 {sec}s")

    if (
        re.search(r"返回|后退|back", segment, re.I)
        and not re.search(r"页面", segment)
        and not re.search(r"点击|点一下|点按|tap|click", segment, re.I)
    ):
        steps.append({"kind": "back", "summary": "返回"})
        reply_parts.append("返回")

    if (
        re.search(r"\bhome\b|home键|主页键|回到桌面|回到首页", segment, re.I)
        and not re.search(r"点击|点一下|点按|tap|click", segment, re.I)
    ):
        steps.append({"kind": "system_key", "key": "home", "summary": "按 Home 键"})
        reply_parts.append("按 Home 键")

    verify_text = re.sub(r"^验证预期[：:]\s*", "", segment).strip()
    if not steps and (
        segment.startswith("验证预期")
        or re.search(r"成功|失败|校验|校验通过|预期|确认", verify_text)
    ):
        frag = verify_text or segment
        steps.append(
            {
                "kind": "verify",
                "verify_text": frag,
                "summary": f"验证：{frag}",
            }
        )
        reply_parts.append(f"验证：{frag}")

    if not steps and not errors:
        errors.append(f"未识别子指令：{segment}")

    return {"steps": steps, "reply_parts": reply_parts, "errors": errors}


def _plan_message_local(
    text: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
) -> Dict[str, Any]:
    """将用户输入拆解为步骤列表 + 回复文案。"""
    raw = (text or "").strip()
    if not raw:
        return {"reply": "请输入指令，例如：打开 造物相机 / 关闭 美团 / 点击 600,1200", "steps": [], "navigate": None}

    if raw.startswith("/"):
        cmd = raw[1:].strip().lower()
        nav = _nav_route(cmd)
        if nav:
            return {
                "reply": f"切换到：{nav['name']}",
                "steps": [],
                "navigate": nav,
            }

    nav = _nav_route(raw)
    if nav and len(raw) < 24 and len(_split_commands(raw)) <= 1:
        return {"reply": f"切换到页面：{nav['path']}", "steps": [], "navigate": nav}

    segments = _split_commands(raw)
    segments = _merge_click_input_segment_pairs(segments)
    steps: List[Dict[str, Any]] = []
    reply_parts: List[str] = []
    segment_errors: List[str] = []
    knowledge_hints: List[str] = []
    page_hint = ""

    ctx = context or {}
    app_id = ctx.get("app_id") or ctx.get("appId")
    plat = (ctx.get("platform") or "android").lower()
    try:
        from server.services.system_settings_service import match_testing_knowledge

        for item in match_testing_knowledge(raw, app_id=app_id, limit=3):
            title = (item.get("title") or "").strip()
            content = (item.get("content") or "").strip()
            if title and content:
                knowledge_hints.append(f"「{title}」: {content[:120]}")
    except Exception:
        pass

    skip_page_hint = False
    try:
        from server.services.shared.run_context.regression_run_context import get_ctx

        skip_page_hint = bool(get_ctx() and get_ctx().get("run_id"))
    except Exception:
        pass

    if sn and app_id and not skip_page_hint:
        try:
            import builtins
            from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
            from server.services.shared.page_context.page_context_service import (
                format_page_hint,
                get_engine_screen_snapshot,
                identify_for_app,
            )

            builtins.TARGET_DEVICE_SN = str(sn)
            engine, _ = bootstrap_mobile_engine(str(sn), plat)
            snap = get_engine_screen_snapshot(engine)
            page_ctx = identify_for_app(
                str(app_id), engine, frame_count=1, screen_text=snap.get("blob") or ""
            )
            page_hint = format_page_hint(page_ctx)
        except Exception as e:
            SLog.w(TAG, f"page context hint failed: {e}")

    for seg in segments:
        planned = _plan_segment(seg, sn=sn, context=context)
        steps.extend(planned.get("steps") or [])
        reply_parts.extend(planned.get("reply_parts") or [])
        segment_errors.extend(planned.get("errors") or [])

    steps = _inject_step_waits(steps)
    steps = _dedupe_consecutive_toggle_steps(steps)

    if not steps and segment_errors:
        return {
            "reply": "\n".join(segment_errors),
            "steps": [],
            "navigate": None,
            "sn": sn,
            "auto_run": False,
        }

    if not steps:
        return {
            "reply": (
                "未识别指令。可尝试：\n"
                "· 打开 造物相机，点击我的，上滑\n"
                "· 打开 造物相机 / 关闭 美团\n"
                "· 打开 com.xxx.app（也支持包名）\n"
                "· 点击 600,1200 或 点击「我的」\n"
                "· 上滑 / 左滑\n"
                "· 去应用列表 / 设备管理\n"
                "· / 查看快捷命令"
            ),
            "steps": [],
            "navigate": None,
        }

    if not sn and any(s.get("kind") in ("click", "swipe", "open_app", "close_app", "system_key", "ability") for s in steps):
        reply_parts.append("（未选设备：请先在顶部选择在线手机）")

    reply = " → ".join(reply_parts) if reply_parts else "好的"
    if len(steps) > 1:
        reply = f"共 {len(steps)} 步：{reply}"
    if segment_errors:
        reply += "\n⚠ " + "；".join(segment_errors)
    if page_hint:
        reply += f"\n📍 {page_hint}"

    return {
        "reply": reply,
        "display_reply": " → ".join(reply_parts) if reply_parts else reply,
        "steps": steps,
        "navigate": None,
        "sn": sn,
        "auto_run": True,
        "knowledge_hints": knowledge_hints,
        "page_hint": page_hint,
        "segment_errors": segment_errors,
        "plan_complete": len(segment_errors) == 0,
    }


def _num(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return default


def _normalize_ai_step(step: Dict[str, Any], *, require_visual_coordinates: bool = False) -> Optional[Dict[str, Any]]:
    if not isinstance(step, dict):
        return None
    tool_payload: Dict[str, Any] = {}
    kind = str(step.get("kind") or step.get("type") or step.get("action") or "").strip().lower()
    summary = str(step.get("summary") or step.get("reason") or kind or "step")
    if step.get("tool_code"):
        tool_payload = step
    elif summary.strip().startswith("{") and "tool_code" in summary:
        try:
            parsed = ast.literal_eval(summary)
            if isinstance(parsed, dict):
                tool_payload = parsed
                summary = str(parsed.get("step_text") or parsed.get("reason") or summary)
        except Exception:
            tool_payload = {}
    if tool_payload and not kind:
        tool_code = str(tool_payload.get("tool_code") or "")
        if "click" in tool_code:
            kind = "click"
        elif "input" in tool_code or "type" in tool_code:
            kind = "input"
    if not kind and _num(step.get("x")) > 0 and _num(step.get("y")) > 0:
        if str(step.get("text") or step.get("value") or "").strip():
            kind = "input"
        else:
            kind = "click"
    if kind in {"tap", "click"}:
        label = str(step.get("label") or step.get("target") or step.get("text") or "").strip()
        params = tool_payload.get("parameters") if isinstance(tool_payload.get("parameters"), dict) else {}
        if (not label or label in {"tool_code", "click_by_text", "x_mini_orange.click_by_text"}) and params:
            label = str(params.get("text") or params.get("label") or params.get("target") or "").strip()
        if not label or label in {"文本", "按钮", "目标", "tool_code"}:
            match = re.search(r"[「『“\"']([^「」『』“”\"']{1,40})[」』”\"']", summary)
            if match:
                label = match.group(1).strip()
        if not label or label in {"文本", "按钮", "目标"}:
            match = re.search(r"(?:点击|点按|tap|click)(?:文本|按钮|目标)?\s*([\u4e00-\u9fffA-Za-z0-9_ -]{1,30})", summary, re.I)
            if match:
                label = match.group(1).strip(" ：:，,。.")
        x = _num(step.get("x") or step.get("center_x") or step.get("cx"))
        y = _num(step.get("y") or step.get("center_y") or step.get("cy"))
        bbox = step.get("bbox") if isinstance(step.get("bbox"), dict) else {}
        if (not x or not y) and bbox:
            bx = _num(bbox.get("x") or bbox.get("left"))
            by = _num(bbox.get("y") or bbox.get("top"))
            bw = _num(bbox.get("w") or bbox.get("width"))
            bh = _num(bbox.get("h") or bbox.get("height"))
            if bx and by and bw and bh:
                x = bx + bw // 2
                y = by + bh // 2
        if require_visual_coordinates and (x <= 0 or y <= 0):
            return None
        return {
            "kind": "click",
            "x": x,
            "y": y,
            "label": label,
            "coords_explicit": bool(step.get("coords_explicit") or (x > 0 and y > 0)),
            "ai_coordinate_only": bool(require_visual_coordinates),
            "bbox": bbox,
            "confidence": step.get("confidence"),
            "reason": step.get("reason") or "",
            "summary": summary if summary != "click" else f"点击「{label or '目标'}」",
        }
    if kind in {"input", "type", "text"}:
        value = str(step.get("text") or step.get("value") or "").strip()
        if not value:
            return None
        field_hint = str(step.get("field_hint") or step.get("field") or "").strip()
        x = _num(step.get("x") or step.get("center_x") or step.get("cx"))
        y = _num(step.get("y") or step.get("center_y") or step.get("cy"))
        if require_visual_coordinates and (x <= 0 or y <= 0):
            return None
        out = {
            "kind": "input",
            "text": value,
            "summary": summary if summary != "input" else f"输入{field_hint or '文本'} {value}",
        }
        # AI 坐标模式直接点坐标再输入，不需要本地 field_hint 找输入框；
        # field_hint 只在 Local 模式作为本地定位线索保留。
        if not require_visual_coordinates:
            out["field_hint"] = field_hint
        if x and y:
            out.update(
                {
                    "x": x,
                    "y": y,
                    "label": str(step.get("label") or field_hint or "输入框"),
                    "coords_explicit": True,
                    "ai_coordinate_only": bool(require_visual_coordinates),
                    "reason": step.get("reason") or "",
                }
            )
        return out
    if kind in {"swipe", "scroll"}:
        sx = _num(step.get("start_x") or step.get("x1") or step.get("from_x"))
        sy = _num(step.get("start_y") or step.get("y1") or step.get("from_y"))
        ex = _num(step.get("end_x") or step.get("x2") or step.get("to_x"))
        ey = _num(step.get("end_y") or step.get("y2") or step.get("to_y"))
        if require_visual_coordinates:
            if min(sx, sy, ex, ey) <= 0:
                return None
            return {
                "kind": "swipe",
                "start_x": sx,
                "start_y": sy,
                "end_x": ex,
                "end_y": ey,
                "duration_ms": _num(step.get("duration_ms") or step.get("duration") or 350, 350),
                "ai_coordinate_only": True,
                "summary": summary or "滑动",
                "reason": step.get("reason") or "",
            }
        direction = str(step.get("direction") or "up").strip().lower()
        if direction not in {"up", "down", "left", "right"}:
            direction = "up"
        return {"kind": "swipe", "direction": direction, "summary": summary or f"滑动 {direction}"}
    if kind in {"open_app", "close_app"}:
        package = str(
            step.get("package")
            or step.get("package_name")
            or step.get("target_mobile")
            or step.get("app_package")
            or (step.get("data") or {}).get("target_mobile")
            or ""
        ).strip()
        if package:
            try:
                from server.services.local.locate.app_packages import (
                    resolve_known_app_by_alias,
                    resolve_known_app_by_package,
                )

                if not resolve_known_app_by_package(package):
                    for hint in (
                        str(step.get("app_name") or ""),
                        str(step.get("label") or ""),
                        summary,
                    ):
                        known = resolve_known_app_by_alias(hint) if hint else None
                        if known and known.android_packages:
                            package = known.android_packages[0]
                            break
            except Exception:
                pass
        if not package:
            return None
        return {
            "kind": kind,
            "package": package,
            "summary": summary or ("打开应用" if kind == "open_app" else "关闭应用"),
        }
    if kind == "back":
        return {"kind": "back", "summary": summary or "返回"}
    if kind in {"system_key", "keyevent", "press_key"}:
        key = str(step.get("key") or step.get("event") or step.get("key_code") or "").strip().lower()
        if key in {"home", "back", "menu", "power"}:
            return {"kind": "system_key", "key": key, "summary": summary or f"按 {key} 键"}
        return None
    if kind == "ability":
        node_code = str(step.get("nodeCode") or step.get("node_code") or "").strip()
        if not node_code:
            return None
        return {
            "kind": "ability",
            "nodeCode": node_code,
            "platform": step.get("platform") or "mobile",
            "data": step.get("data") if isinstance(step.get("data"), dict) else {},
            "summary": summary,
        }
    if kind == "verify":
        return {
            "kind": "verify",
            "verify_text": str(step.get("verify_text") or step.get("text") or "").strip(),
            "summary": summary,
        }
    return None


def _repair_ai_plan_json_text(text: str) -> str:
    """Fix common Volcengine/Doubao JSON typos before parsing."""
    raw = (text or "").strip()
    if not raw:
        return raw
    # Doubao 偶发：{"steps":[...]},"auto_run":true} 多一个 }
    raw = re.sub(
        r'(\]\s*)\}\s*,(\s*"(?:auto_run|blockers|navigate)"\s*:)',
        r'\1,\2',
        raw,
        count=1,
    )
    return raw


def _extract_balanced_json_slice(raw: str, start: int) -> Optional[str]:
    """从 start 处的 `{` 起截取括号平衡的 JSON 对象。"""
    if start < 0 or start >= len(raw) or raw[start] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(raw)):
        ch = raw[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0:
                    return raw[start : idx + 1]
    return None


def _score_ai_plan_dict(obj: Dict[str, Any]) -> int:
    """优先选择含可执行 steps（尤其带坐标）的 Plan JSON。"""
    if not isinstance(obj, dict):
        return -1
    score = 0
    if obj.get("reply"):
        score += 5
    steps = obj.get("steps")
    if isinstance(steps, list):
        score += 10
        if steps:
            score += 25
        for item in steps:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or item.get("type") or "").strip().lower()
            if kind in {"click", "tap", "input", "swipe", "scroll"}:
                score += 8
            if _num(item.get("x")) > 0 and _num(item.get("y")) > 0:
                score += 40
                break
    return score


def _iter_json_object_candidates(text: str):
    """Yield JSON object substrings; Doubao thinking 模型常在推理中嵌入多段 JSON。"""
    raw = (text or "").strip()
    if not raw:
        return
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    seen: set[str] = set()

    def _emit(candidate: str):
        candidate = candidate.strip()
        if not candidate or candidate in seen:
            return
        seen.add(candidate)
        yield candidate

    yield from _emit(raw)
    yield from _emit(_repair_ai_plan_json_text(raw))
    # 截断 JSON 后的中文推理/解释（Doubao / Seed 2.0 常见）
    for sep in (
        "\n我现在",
        "\n我需要",
        "\n首先，",
        "\n接下来，",
        "\n所以：",
        "\n用户现在",
        "\n首先看",
        "\n对，",
        "。所以：",
        "。所以:",
    ):
        head = raw.split(sep, 1)[0].strip()
        if head and head != raw:
            yield from _emit(head)
            yield from _emit(_repair_ai_plan_json_text(head))
    # 每个 {"reply" 锚点单独平衡截取（思考链里最终 Plan 常在后面）
    for m in re.finditer(r'\{"reply"\s*:', raw):
        sub = _extract_balanced_json_slice(raw, m.start())
        if sub:
            yield from _emit(sub)
            yield from _emit(_repair_ai_plan_json_text(sub))
    # 每个 `{` 起点尝试平衡截取
    for start in range(len(raw)):
        if raw[start] != "{":
            continue
        sub = _extract_balanced_json_slice(raw, start)
        if sub and len(sub) > 12:
            yield from _emit(sub)
            yield from _emit(_repair_ai_plan_json_text(sub))


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    best_score = -1
    for candidate in _iter_json_object_candidates(text):
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        score = _score_ai_plan_dict(obj)
        if score > best_score:
            best_score = score
            best = obj
    return best


def _coerce_ai_plan_steps(raw_plan: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize plan shape: steps array, or promote a lone top-level visual step."""
    plan = dict(raw_plan or {})
    steps = plan.get("steps")
    if isinstance(steps, dict):
        plan["steps"] = [steps]
        return plan
    if isinstance(steps, list) and steps:
        return plan
    visual_kinds = {"click", "tap", "input", "type", "text", "swipe", "scroll"}
    kind = str(plan.get("kind") or plan.get("type") or plan.get("action") or "").strip().lower()
    if kind in visual_kinds or _num(plan.get("x")) > 0 or _num(plan.get("y")) > 0:
        step = {
            k: v
            for k, v in plan.items()
            if k
            not in {
                "reply",
                "display_reply",
                "auto_run",
                "blockers",
                "navigate",
                "steps",
                "plan_complete",
            }
        }
        if kind and not step.get("kind") and not step.get("type"):
            step["kind"] = kind
        plan["steps"] = [step]
    return plan


def _openai_message_text(message: Any) -> str:
    """Normalize OpenAI-compatible assistant message text (string, parts, or reasoning)."""
    if not isinstance(message, dict):
        return ""
    chunks: List[str] = []
    content = message.get("content")
    if isinstance(content, str):
        if content.strip():
            chunks.append(content.strip())
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if text is None and block.get("type") == "text":
                text = block.get("content")
            if text:
                chunks.append(str(text).strip())
    for key in ("reasoning_content", "reasoning"):
        extra = message.get(key)
        if isinstance(extra, str) and extra.strip():
            chunks.append(extra.strip())
    return "\n".join(chunks).strip()


def _parse_openai_chat_completion(resp_json: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    choice = (resp_json.get("choices") or [{}])[0] if isinstance(resp_json, dict) else {}
    if not isinstance(choice, dict):
        choice = {}
    message = choice.get("message") or {}
    finish_reason = str(choice.get("finish_reason") or "")
    text = _openai_message_text(message)
    parsed = _extract_json_object(text)
    if not parsed and isinstance(message, dict):
        for key in ("reasoning_content", "reasoning"):
            alt = message.get(key)
            if isinstance(alt, str) and alt.strip():
                parsed = _extract_json_object(alt)
                if parsed:
                    break
    meta = {
        "finish_reason": finish_reason,
        "content_len": len(text),
        "content_preview": text[:800] if text else "",
    }
    return parsed, meta


def _ai_foreground_metadata(
    sn: Optional[str],
    platform: str,
    *,
    expected_package: str = "",
) -> Dict[str, Any]:
    """仅读取前台包名，供 AI 规划参考；不触发 OCR。"""
    if not sn:
        return {}
    try:
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
        from server.services.app_automation_service import _engine_current_package, _pkg_matches

        engine, _ = bootstrap_mobile_engine(str(sn), platform)
        fg_pkg = (_engine_current_package(engine) or "").strip()
        meta: Dict[str, Any] = {"foreground_package": fg_pkg}
        expected = (expected_package or "").strip()
        if expected:
            meta["expected_package"] = expected
            if fg_pkg and not _pkg_matches(expected, fg_pkg):
                meta["foreground_drift"] = True
                meta["foreground_note"] = (
                    f"设备前台包名为 {fg_pkg}，被测应用包名为 {expected}。"
                    "请结合截图自行判断是否仍在被测应用流程内（如微信授权栈），再决定坐标与动作。"
                )
        return meta
    except Exception as e:
        SLog.w(TAG, f"read foreground package for AI failed: {e}")
        return {}


def _build_ai_screen_context(
    sn: Optional[str],
    platform: str,
    provider_id: Optional[str] = None,
    *,
    expected_package: str = "",
) -> Dict[str, Any]:
    if not sn:
        return {}
    fg_meta = _ai_foreground_metadata(sn, platform, expected_package=expected_package)
    try:
        from PIL import Image

        from server.core.database import APP_DATA_DIR
        from server.services.shared.screenshot.regression_capture import capture_device_screenshot
        from server.services.system_settings_service import get_ai_plan_compress_ratio

        static_path = capture_device_screenshot(
            str(sn),
            platform,
            run_id=f"ai-plan-{uuid.uuid4().hex[:8]}",
            tag="observe",
            settle_ms=120,
            max_attempts=2,
        )
        if not static_path:
            return fg_meta
        name = static_path.split("/static/")[-1] if "/static/" in static_path else os.path.basename(static_path)
        local_path = os.path.join(APP_DATA_DIR, "uploads", name)
        if not os.path.isfile(local_path):
            return {"image_path": static_path, **fg_meta}
        ratio = get_ai_plan_compress_ratio(provider_id)
        configured_ratio = ratio
        compress = ratio > 1.0
        with Image.open(local_path) as img:
            img = img.convert("RGB")
            original_w, original_h = img.size
            if compress:
                preview_w = max(1, round(original_w / ratio))
                preview_h = max(1, round(original_h / ratio))
                img = img.resize((preview_w, preview_h))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=68, optimize=True)
            else:
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=92, optimize=True)
            encoded = base64.b64encode(buf.getvalue()).decode("ascii")
            preview_w, preview_h = img.size
            result = {
                "image_path": static_path,
                "width": original_w,
                "height": original_h,
                "preview_width": preview_w,
                "preview_height": preview_h,
                "compress_image": compress,
                "configured_compress_ratio": configured_ratio,
                "effective_compress_ratio": ratio if compress else 1.0,
                "compress_ratio": ratio if compress else 1.0,
                "mime_type": "image/jpeg",
                "base64": encoded,
                "data_url": f"data:image/jpeg;base64,{encoded}",
                "note": (
                    "坐标请基于 preview_width/preview_height（发给模型的截图像素尺寸）返回，"
                    f"Server 会按 compress_ratio={ratio if compress else 1.0} 映射到设备 width/height。"
                    "点击点应对准目标控件可点击区域中心，不要落在系统状态栏或屏幕外缘。"
                ),
            }
            result.update(fg_meta)
            return result
    except Exception as e:
        SLog.w(TAG, f"build AI screen context failed: {e}")
        return fg_meta


def _collect_ai_preview_coord_violations(
    steps: Any,
    screen: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    violations, _coord_space = _analyze_ai_plan_coordinates(steps, screen)
    return violations


def _doubao_coord_input_mode(x: float, y: float, screen: Dict[str, Any]) -> str:
    """Guess whether Doubao returned normalized_1000 / device_pixels / preview_pixels."""
    orig_w = int(screen.get("width") or 0)
    orig_h = int(screen.get("height") or 0)
    prev_w = int(screen.get("preview_width") or orig_w)
    prev_h = int(screen.get("preview_height") or orig_h)
    if x <= 0 and y <= 0:
        return "skip"
    if x > 1000 or y > 1000:
        if orig_w > 0 and orig_h > 0 and x <= orig_w and y <= orig_h:
            return "device_pixels"
        return "invalid"
    if orig_w > 1000 or orig_h > 1000:
        return "normalized_1000"
    if x <= prev_w and y <= prev_h:
        return "preview_pixels"
    return "normalized_1000"


def _doubao_point_to_device(
    x: float,
    y: float,
    *,
    mode: str,
    screen: Dict[str, Any],
) -> tuple[int, int]:
    orig_w = int(screen.get("width") or 0)
    orig_h = int(screen.get("height") or 0)
    prev_w = int(screen.get("preview_width") or orig_w)
    prev_h = int(screen.get("preview_height") or orig_h)
    if mode == "normalized_1000":
        dx = max(1, min(orig_w, int(round(x * orig_w / 1000.0))))
        dy = max(1, min(orig_h, int(round(y * orig_h / 1000.0))))
        return dx, dy
    if mode == "preview_pixels" and prev_w > 0 and prev_h > 0:
        dx = max(1, min(orig_w, int(round(x * orig_w / float(prev_w)))))
        dy = max(1, min(orig_h, int(round(y * orig_h / float(prev_h)))))
        return dx, dy
    return max(1, min(orig_w, int(round(x)))), max(1, min(orig_h, int(round(y))))


def _convert_doubao_plan_to_device_coords(
    raw_plan: Dict[str, Any],
    screen: Optional[Dict[str, Any]],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Map Doubao visual coords (normalized / preview / device) to device pixel space."""
    plan = dict(raw_plan or {})
    if not screen:
        return plan, {"applied": False, "reason": "no_screen"}
    steps = plan.get("steps")
    if not isinstance(steps, list):
        return plan, {"applied": False, "reason": "no_steps"}

    visual_kinds = {"click", "tap", "input", "type", "text", "swipe", "scroll"}
    detected_mode = ""
    samples: List[Dict[str, Any]] = []

    def _detect_from_point(x: float, y: float) -> str:
        return _doubao_coord_input_mode(x, y, screen)

    for item in steps:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("type") or "").strip().lower()
        if kind not in visual_kinds:
            continue
        if kind in {"click", "tap", "input", "type", "text"}:
            mode = _detect_from_point(_num(item.get("x")), _num(item.get("y")))
        else:
            mode = _detect_from_point(_num(item.get("start_x")), _num(item.get("start_y")))
        if mode not in {"", "skip"}:
            detected_mode = mode
            break

    if not detected_mode or detected_mode == "invalid":
        return plan, {"applied": False, "reason": "unknown_coord_mode", "detected_mode": detected_mode}

    converted_steps: List[Dict[str, Any]] = []
    for item in steps:
        if not isinstance(item, dict):
            converted_steps.append(item)
            continue
        step = dict(item)
        kind = str(step.get("kind") or step.get("type") or "").strip().lower()
        if kind in {"click", "tap", "input", "type", "text"}:
            bx, by = _num(step.get("x")), _num(step.get("y"))
            if bx > 0 and by > 0:
                ax, ay = _doubao_point_to_device(bx, by, mode=detected_mode, screen=screen)
                samples.append({"kind": kind, "before": {"x": bx, "y": by}, "after": {"x": ax, "y": ay}})
                step["x"], step["y"] = ax, ay
        elif kind in {"swipe", "scroll"}:
            for xk, yk in (("start_x", "start_y"), ("end_x", "end_y")):
                bx, by = _num(step.get(xk)), _num(step.get(yk))
                if bx > 0 and by > 0:
                    ax, ay = _doubao_point_to_device(bx, by, mode=detected_mode, screen=screen)
                    step[xk], step[yk] = ax, ay
            if any(step.get(k) for k in ("start_x", "start_y", "end_x", "end_y")):
                samples.append(
                    {
                        "kind": kind,
                        "before": {k: step.get(k) for k in ("start_x", "start_y", "end_x", "end_y")},
                        "after": {k: step.get(k) for k in ("start_x", "start_y", "end_x", "end_y")},
                    }
                )
        converted_steps.append(step)

    plan["steps"] = converted_steps
    return plan, {
        "applied": True,
        "provider_coord_mode": detected_mode,
        "device": {"width": int(screen.get("width") or 0), "height": int(screen.get("height") or 0)},
        "preview": {
            "width": int(screen.get("preview_width") or 0),
            "height": int(screen.get("preview_height") or 0),
        },
        "samples": samples[:5],
    }


def _analyze_ai_plan_coordinates(
    steps: Any,
    screen: Optional[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], str]:
    """Classify model coords as preview / device / invalid (mixed or out of range)."""
    if not isinstance(steps, list) or not screen:
        return [], "preview"
    prev_w = int(screen.get("preview_width") or 0)
    prev_h = int(screen.get("preview_height") or 0)
    orig_w = int(screen.get("width") or 0)
    orig_h = int(screen.get("height") or 0)
    if prev_w <= 0 or prev_h <= 0:
        return [], "preview"
    if prev_w == orig_w and prev_h == orig_h:
        return [], "preview"

    visual_kinds = {"click", "tap", "input", "type", "text", "swipe", "scroll"}
    violations: List[Dict[str, Any]] = []
    saw_preview = False
    saw_device = False

    def _classify_point(x: float, y: float) -> str:
        if x <= 0 and y <= 0:
            return "skip"
        in_preview = x > 0 and y > 0 and x <= prev_w and y <= prev_h
        in_device = (
            x > 0
            and y > 0
            and orig_w > 0
            and orig_h > 0
            and x <= orig_w
            and y <= orig_h
        )
        exceeds_preview = x > prev_w or y > prev_h
        if in_preview and not exceeds_preview:
            return "preview"
        if exceeds_preview and in_device:
            return "device"
        return "invalid"

    def _record_violation(
        *,
        step_index: int,
        kind: str,
        x_key: str,
        y_key: str,
        step: Dict[str, Any],
        x: float,
        y: float,
    ) -> None:
        issues: List[str] = []
        if x <= 0 or x > prev_w:
            issues.append(f"{x_key}={x} 不在 [1,{prev_w}]")
        if y <= 0 or y > prev_h:
            issues.append(f"{y_key}={y} 不在 [1,{prev_h}]")
        if issues:
            violations.append(
                {
                    "step_index": step_index,
                    "kind": kind,
                    "point": x_key.replace("_x", "").replace("start_", "start").replace("end_", "end"),
                    "x": x,
                    "y": y,
                    "preview": {"width": prev_w, "height": prev_h},
                    "device": {"width": orig_w, "height": orig_h},
                    "issues": issues,
                }
            )

    def _check_point(
        *,
        step_index: int,
        kind: str,
        x_key: str,
        y_key: str,
        step: Dict[str, Any],
    ) -> None:
        nonlocal saw_preview, saw_device
        x = _num(step.get(x_key))
        y = _num(step.get(y_key))
        space = _classify_point(x, y)
        if space == "skip":
            return
        if space == "preview":
            saw_preview = True
            return
        if space == "device":
            saw_device = True
            return
        _record_violation(
            step_index=step_index,
            kind=kind,
            x_key=x_key,
            y_key=y_key,
            step=step,
            x=x,
            y=y,
        )

    for step_index, item in enumerate(steps):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("type") or item.get("action") or "").strip().lower()
        if kind not in visual_kinds:
            continue
        if kind in {"click", "tap", "input", "type", "text"}:
            _check_point(step_index=step_index, kind=kind, x_key="x", y_key="y", step=item)
        elif kind in {"swipe", "scroll"}:
            _check_point(step_index=step_index, kind=kind, x_key="start_x", y_key="start_y", step=item)
            _check_point(step_index=step_index, kind=kind, x_key="end_x", y_key="end_y", step=item)

    if violations:
        return violations, "invalid"
    if saw_device and not saw_preview:
        return [], "device"
    return [], "preview"


def _validate_device_coord_violations(
    steps: Any,
    screen: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """After Doubao coord conversion, ensure device-space pixels are in range."""
    if not isinstance(steps, list) or not screen:
        return []
    dev_w = int(screen.get("width") or 0)
    dev_h = int(screen.get("height") or 0)
    if dev_w <= 0 or dev_h <= 0:
        return []
    visual_kinds = {"click", "tap", "input", "type", "text", "swipe", "scroll"}
    violations: List[Dict[str, Any]] = []

    def _check(step_index: int, kind: str, x_key: str, y_key: str, step: Dict[str, Any]) -> None:
        x = _num(step.get(x_key))
        y = _num(step.get(y_key))
        if x <= 0 and y <= 0:
            return
        issues: List[str] = []
        if x <= 0 or x > dev_w:
            issues.append(f"{x_key}={x} 不在 [1,{dev_w}]")
        if y <= 0 or y > dev_h:
            issues.append(f"{y_key}={y} 不在 [1,{dev_h}]")
        if issues:
            violations.append(
                {
                    "step_index": step_index,
                    "kind": kind,
                    "x": x,
                    "y": y,
                    "device": {"width": dev_w, "height": dev_h},
                    "issues": issues,
                }
            )

    for step_index, item in enumerate(steps):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("type") or item.get("action") or "").strip().lower()
        if kind not in visual_kinds:
            continue
        if kind in {"click", "tap", "input", "type", "text"}:
            _check(step_index, kind, "x", "y", item)
        elif kind in {"swipe", "scroll"}:
            _check(step_index, kind, "start_x", "start_y", item)
            _check(step_index, kind, "end_x", "end_y", item)
    return violations


def _scale_ai_plan_coordinates(
    raw_plan: Dict[str, Any],
    screen: Optional[Dict[str, Any]],
    *,
    coord_space: str = "preview",
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Map model coordinates from preview image space to device screen space."""
    plan = dict(raw_plan or {})
    steps = plan.get("steps")
    if not isinstance(steps, list) or not screen:
        return plan, {"applied": False, "reason": "no_steps_or_screen", "coord_space": coord_space}

    orig_w = int(screen.get("width") or 0)
    orig_h = int(screen.get("height") or 0)
    prev_w = int(screen.get("preview_width") or orig_w)
    prev_h = int(screen.get("preview_height") or orig_h)
    if orig_w <= 0 or orig_h <= 0 or prev_w <= 0 or prev_h <= 0:
        return plan, {"applied": False, "reason": "invalid_dimensions", "coord_space": coord_space}
    if coord_space == "device":
        return plan, {
            "applied": False,
            "reason": "already_device_space",
            "coord_space": "device",
            "device": {"width": orig_w, "height": orig_h},
            "preview": {"width": prev_w, "height": prev_h},
            "compress_image": bool(screen.get("compress_image")),
            "compress_ratio": float(screen.get("compress_ratio") or 1.0),
        }
    if prev_w == orig_w and prev_h == orig_h:
        return plan, {
            "applied": False,
            "reason": "same_dimensions",
            "device": {"width": orig_w, "height": orig_h},
            "preview": {"width": prev_w, "height": prev_h},
            "compress_image": bool(screen.get("compress_image")),
            "compress_ratio": float(screen.get("compress_ratio") or 1.0),
        }

    ratio = float(screen.get("compress_ratio") or 0)
    use_ratio_scale = bool(screen.get("compress_image")) and ratio > 1.0
    sx = ratio if use_ratio_scale else (orig_w / float(prev_w))
    sy = ratio if use_ratio_scale else (orig_h / float(prev_h))

    def _scale_axis(value: float, *, orig_max: int, scale: float) -> int:
        v = _num(value)
        if v <= 0:
            return 0
        return max(1, min(orig_max, int(round(v * scale))))

    def _scale_point(x_key: str, y_key: str, step: Dict[str, Any]) -> bool:
        x = _num(step.get(x_key))
        y = _num(step.get(y_key))
        if x <= 0 or y <= 0:
            return False
        step[x_key] = _scale_axis(x, orig_max=orig_w, scale=sx)
        step[y_key] = _scale_axis(y, orig_max=orig_h, scale=sy)
        return True

    scaled_steps: List[Dict[str, Any]] = []
    samples: List[Dict[str, Any]] = []
    for item in steps:
        if not isinstance(item, dict):
            scaled_steps.append(item)
            continue
        step = dict(item)
        kind = str(step.get("kind") or step.get("type") or "").strip().lower()
        before = {}
        scaled = False
        if kind in {"click", "tap"}:
            before = {"x": step.get("x"), "y": step.get("y")}
            scaled = _scale_point("x", "y", step)
        elif kind in {"input", "type", "text"}:
            before = {"x": step.get("x"), "y": step.get("y")}
            scaled = _scale_point("x", "y", step)
        elif kind in {"swipe", "scroll"}:
            before = {
                "start_x": step.get("start_x"),
                "start_y": step.get("start_y"),
                "end_x": step.get("end_x"),
                "end_y": step.get("end_y"),
            }
            scaled = (
                _scale_point("start_x", "start_y", step)
                or _scale_point("end_x", "end_y", step)
            )
        if before:
            samples.append(
                {
                    "kind": kind,
                    "scaled": scaled,
                    "before": before,
                    "after": {k: step.get(k) for k in before},
                }
            )
        scaled_steps.append(step)

    plan["steps"] = scaled_steps
    return plan, {
        "applied": True,
        "coord_space": coord_space,
        "device": {"width": orig_w, "height": orig_h},
        "preview": {"width": prev_w, "height": prev_h},
        "compress_image": bool(screen.get("compress_image")),
        "compress_ratio": ratio if use_ratio_scale else None,
        "scale_x": round(sx, 6),
        "scale_y": round(sy, 6),
        "samples": samples[:5],
    }


def _screen_context_public(screen: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in (screen or {}).items() if k not in {"base64", "data_url"}}


def _screen_context_for_llm(
    screen: Dict[str, Any],
    *,
    provider_id: str = "",
    model: str = "",
) -> Dict[str, Any]:
    """Screen metadata sent to LLM. Doubao 仅暴露 preview_width/height，不含设备 width/height。"""
    from server.services.ai.plan.prompt import _is_volcengine_doubao_provider

    if not screen:
        return {}
    base = _screen_context_public(screen)
    fg_fields = {
        k: base[k]
        for k in ("foreground_package", "expected_package", "foreground_drift", "foreground_note")
        if k in base
    }
    pw = int(base.get("preview_width") or base.get("width") or 0)
    ph = int(base.get("preview_height") or base.get("height") or 0)

    if _is_volcengine_doubao_provider(provider_id, model):
        if pw <= 0 or ph <= 0:
            return {
                k: v
                for k, v in base.items()
                if k not in {"width", "height", "base64", "data_url"}
            }
        return {
            "image_path": base.get("image_path"),
            "preview_width": pw,
            "preview_height": ph,
            "mime_type": base.get("mime_type"),
            "coord_mode": "normalized_1000",
            "note": (
                f"附图 {pw}×{ph} 像素（压缩后发给模型的 JPEG，与附件一致）。"
                "坐标必须输出 0~1000 归一化整数：左上角(0,0)、右下角(1000,1000)，"
                "与附图宽高成比例、与设备分辨率无关；Server 会映射到设备像素。"
                "禁止输出超过 1000 的绝对像素坐标。"
            ),
            **fg_fields,
        }

    if not base.get("compress_image"):
        return base
    if pw <= 0 or ph <= 0:
        return base
    return {
        "image_path": base.get("image_path"),
        "width": pw,
        "height": ph,
        "mime_type": base.get("mime_type"),
        "note": (
            f"附图即为 {pw}×{ph} 像素；screen.width={pw}、screen.height={ph} 是坐标唯一依据。"
            f"x∈[1,{pw}]、y∈[1,{ph}]，超出则拒绝执行。"
            "禁止按其他分辨率或比例换算坐标。"
        ),
        **fg_fields,
    }


def _ai_image_pipeline_debug(screen: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Clarify configured vs effective Plan screenshot compression (Doubao overrides to 1.0)."""
    if not screen:
        return {}
    return {
        "device": {
            "width": int(screen.get("width") or 0),
            "height": int(screen.get("height") or 0),
        },
        "preview": {
            "width": int(screen.get("preview_width") or 0),
            "height": int(screen.get("preview_height") or 0),
        },
        "compress_image": bool(screen.get("compress_image")),
        "configured_compress_ratio": screen.get("configured_compress_ratio"),
        "effective_compress_ratio": screen.get("effective_compress_ratio"),
    }


def _ai_debug_screen_context(
    screen: Optional[Dict[str, Any]],
    *,
    provider_id: str = "",
    model: str = "",
) -> Dict[str, Any]:
    """ai_debug / 观察卡片中的 screen：豆包与发给模型的一致，不含设备 width/height。"""
    if not screen:
        return {}
    from server.services.ai.plan.prompt import _is_volcengine_doubao_provider

    if _is_volcengine_doubao_provider(provider_id, model):
        base = _screen_context_for_llm(screen, provider_id=provider_id, model=model)
        return {
            **base,
            "compress_image": bool(screen.get("compress_image")),
            "configured_compress_ratio": screen.get("configured_compress_ratio"),
            "effective_compress_ratio": screen.get("effective_compress_ratio"),
        }
    return _screen_context_public(screen)


def _ai_known_apps_context(platform: str, *, limit: int = 48) -> List[Dict[str, str]]:
    """Compact app catalog for LLM planning (name → package)."""
    try:
        from server.services.local.locate.app_packages import list_known_apps, package_for_app_key

        rows: List[Dict[str, str]] = []
        for app in list_known_apps()[:limit]:
            pkg = package_for_app_key(app.key, platform=platform or "android")
            if not pkg:
                continue
            rows.append({"name": app.name, "key": app.key, "package": pkg})
        return rows
    except Exception:
        return []


def _build_preview_coord_correction_message(
    violations: List[Dict[str, Any]],
    screen: Optional[Dict[str, Any]],
    *,
    provider_id: str = "",
    model: str = "",
) -> str:
    from server.services.ai.plan.prompt import (
        _is_volcengine_doubao_provider,
        screen_coord_examples_hint,
        screen_coord_examples_hint_normalized,
        screen_coord_space_hint,
        screen_coord_space_hint_normalized,
    )

    pw = int((screen or {}).get("preview_width") or 0)
    ph = int((screen or {}).get("preview_height") or 0)
    issue_lines: List[str] = []
    for item in violations[:3]:
        issue_lines.extend(str(x) for x in (item.get("issues") or []))
    issues_text = "；".join(issue_lines) or "坐标越界"
    is_doubao = _is_volcengine_doubao_provider(provider_id, model)
    parts = [
        f"【坐标错误，必须重算】上次返回无效：{issues_text}。",
        (
            "坐标必须为 0~1000 归一化整数，禁止输出绝对像素。请只返回修正后的一个 JSON。"
            if is_doubao
            else (
                f"附图宽 {pw}px、高 {ph}px；坐标必须满足 x∈[1,{pw}]、y∈[1,{ph}]。"
                "直接在附图像素上定位目标中心，禁止用设备分辨率、禁止乘除比例。"
                "请只返回修正后的一个 JSON。"
            )
        ),
        screen_coord_space_hint_normalized(screen) if is_doubao else screen_coord_space_hint(screen),
        screen_coord_examples_hint_normalized(screen) if is_doubao else screen_coord_examples_hint(screen),
    ]
    return "\n".join(part for part in parts if part)


def _append_openai_image(messages: List[Dict[str, Any]], screen: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not screen.get("data_url"):
        return messages
    out = list(messages)
    # Plan 截图尺寸已由 Server 控制；detail=low 会让部分厂商再次缩小图片，
    # 导致模型所见分辨率与 preview_width/height 不一致，坐标会系统性偏移。
    image_detail = "high"
    for idx in range(len(out) - 1, -1, -1):
        if out[idx].get("role") == "user":
            text = str(out[idx].get("content") or "")
            out[idx] = {
                **out[idx],
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": screen["data_url"], "detail": image_detail}},
                ],
            }
            return out
    return out


def _ai_plan_request_timeout(channel: str) -> int:
    from server.services.ai.plan.prompt import _ai_plan_request_timeout as timeout_for_channel

    return timeout_for_channel(channel)


def _call_openai_compatible_plan(
    *,
    provider: Dict[str, Any],
    instruction: str,
    channel: str,
    platform: str,
    context: Dict[str, Any],
    screen: Optional[Dict[str, Any]] = None,
    extra_user_messages: Optional[List[Dict[str, str]]] = None,
) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    from server.services.ai.plan.prompt import (
        _is_volcengine_doubao_provider,
        build_ai_plan_messages,
        volcengine_chat_payload_extras,
    )

    base = str(provider.get("base_url") or "").rstrip("/")
    api_key = str(provider.get("api_key") or "").strip()
    model = str(provider.get("model") or "").strip()
    if not base or not api_key or not model:
        return None, {}
    ctx_text = json.dumps(context or {}, ensure_ascii=False, default=str)[:4000]
    drift_replan = (context or {}).get("drift_replan")
    if not isinstance(drift_replan, dict):
        drift_replan = None
    goal_continue_replan = (context or {}).get("goal_continue_replan")
    if not isinstance(goal_continue_replan, dict):
        goal_continue_replan = None
    messages = build_ai_plan_messages(
        instruction=instruction,
        platform=platform,
        channel=channel,
        context=ctx_text,
        provider_id=str(provider.get("id") or ""),
        model=model,
        screen=screen,
        drift_replan=drift_replan,
        goal_continue_replan=goal_continue_replan,
    )
    messages.append(
        {
            "role": "user",
            "content": (
                "请只返回 JSON，不要 Markdown。"
                + (
                    "坐标使用 0~1000 归一化整数（左上角 0,0，右下角 1000,1000）。"
                    if _is_volcengine_doubao_provider(str(provider.get("id") or ""), model)
                    else "坐标基于附图 screen.width×screen.height（像素）。"
                )
                + "格式："
                "{\"reply\":\"...\",\"steps\":[{\"kind\":\"click|input|swipe|open_app|close_app|back|system_key|ability\","
                "\"x\":123,\"y\":456,\"coords_explicit\":true,\"label\":\"审计文案\",\"summary\":\"...\"}],\"auto_run\":true,\"plan_complete\":true}。"
            ),
        }
    )
    for extra in extra_user_messages or []:
        if isinstance(extra, dict) and extra.get("content"):
            messages.append({"role": "user", "content": str(extra["content"])})
    messages = _append_openai_image(messages, screen or {})
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": 2048,
        **volcengine_chat_payload_extras(provider_id=str(provider.get("id") or ""), model=model),
    }
    resp = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=_ai_plan_request_timeout(channel),
    )
    resp.raise_for_status()
    resp_json = resp.json()
    raw_plan, parse_meta = _parse_openai_chat_completion(resp_json)
    if not raw_plan:
        img_b64_len = len((screen or {}).get("base64") or "")
        SLog.w(
            TAG,
            f"AI plan JSON parse failed provider={provider.get('id')} channel={channel} "
            f"finish={parse_meta.get('finish_reason')!r} content_len={parse_meta.get('content_len')} "
            f"compress={(screen or {}).get('compress_image')} image_b64_len={img_b64_len} "
            f"preview={parse_meta.get('content_preview')!r}",
        )
    return raw_plan, parse_meta


def _call_anthropic_compatible_plan(
    *,
    provider: Dict[str, Any],
    instruction: str,
    channel: str,
    platform: str,
    context: Dict[str, Any],
    screen: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    from server.services.ai.plan.prompt import build_ai_plan_messages

    base = str(provider.get("base_url") or "").rstrip("/")
    api_key = str(provider.get("api_key") or "").strip()
    model = str(provider.get("model") or "").strip()
    if not base or not api_key or not model:
        return None

    ctx_text = json.dumps(context or {}, ensure_ascii=False, default=str)[:4000]
    drift_replan = (context or {}).get("drift_replan")
    if not isinstance(drift_replan, dict):
        drift_replan = None
    goal_continue_replan = (context or {}).get("goal_continue_replan")
    if not isinstance(goal_continue_replan, dict):
        goal_continue_replan = None
    plan_messages = build_ai_plan_messages(
        instruction=instruction,
        platform=platform,
        channel=channel,
        context=ctx_text,
        provider_id=str(provider.get("id") or ""),
        model=model,
        screen=screen,
        drift_replan=drift_replan,
        goal_continue_replan=goal_continue_replan,
    )
    system_parts: List[str] = []
    user_parts: List[str] = []
    for item in plan_messages:
        content = str(item.get("content") or "")
        if not content:
            continue
        if item.get("role") == "system":
            system_parts.append(content)
        else:
            user_parts.append(content)
    user_parts.append(
        "请只返回 JSON，不要 Markdown。坐标基于 screen.preview_width×preview_height。格式："
        "{\"reply\":\"...\",\"steps\":[{\"kind\":\"click|input|swipe|open_app|close_app|back|system_key|ability\","
        "\"x\":123,\"y\":456,\"coords_explicit\":true,\"label\":\"审计文案\",\"summary\":\"...\"}],\"auto_run\":true,\"plan_complete\":true}。"
    )

    content_blocks: List[Dict[str, Any]] = [{"type": "text", "text": "\n\n".join(user_parts)}]
    if (screen or {}).get("base64"):
        content_blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": screen.get("mime_type") or "image/jpeg",
                    "data": screen.get("base64"),
                },
            }
        )

    resp = requests.post(
        f"{base}/v1/messages",
        headers={
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 2048,
            "temperature": 0.1,
            "system": "\n\n".join(system_parts),
            "messages": [{"role": "user", "content": content_blocks}],
        },
        timeout=_ai_plan_request_timeout(channel),
    )
    resp.raise_for_status()
    blocks = resp.json().get("content") or []
    content = "\n".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict))
    return _extract_json_object(content)


def _sanitize_llm_error(err: Exception) -> str:
    text = str(err)
    text = re.sub(r"([?&]key=)[^&\s]+", r"\1***", text)
    text = re.sub(r"(Bearer\s+)[A-Za-z0-9._\-]+", r"\1***", text)
    return text


def _llm_error_info(err: Exception, provider: Dict[str, Any]) -> Dict[str, str]:
    status = getattr(getattr(err, "response", None), "status_code", None)
    text = _sanitize_llm_error(err)
    low = text.lower()
    provider_name = str(provider.get("name") or provider.get("id") or "大模型服务")
    if status == 429 or "too many requests" in low or "quota" in low or "rate limit" in low:
        return {
            "type": "quota_or_rate_limit",
            "title": f"{provider_name} 额度或频率受限",
            "message": "当前大模型请求过多、免费额度耗尽，或账号未开通足够的付费额度。",
            "suggestion": "请到对应模型平台检查 API Key 的计费状态、余额/配额和速率限制；也可以稍后重试或切换其他已配置模型。",
            "raw": text,
        }
    if status in {401, 403} or "permission" in low or "unauthorized" in low or "forbidden" in low:
        return {
            "type": "auth_or_permission",
            "title": f"{provider_name} Key 无权限",
            "message": "当前 API Key 无效、权限不足，或未开通该模型访问权限。",
            "suggestion": "请检查 Key 是否正确、是否启用模型 API，以及当前账号是否有该模型访问权限。",
            "raw": text,
        }
    if status == 404 or "not found" in low:
        return {
            "type": "model_not_found",
            "title": f"{provider_name} 模型不可用",
            "message": "当前选择的模型在该账号或区域不可用。",
            "suggestion": "请在密钥配置中切换平台支持的模型，或确认该账号是否开通对应模型。",
            "raw": text,
        }
    return {
        "type": "unknown",
        "title": f"{provider_name} 调用失败",
        "message": "大模型服务返回异常，当前没有生成可执行计划。",
        "suggestion": "请检查网络、Key、模型配置和服务商状态后重试。",
        "raw": text,
    }


def _gemini_model_candidates(model: str) -> List[str]:
    primary = (model or "gemini-2.5-flash").strip()
    if primary.startswith("models/"):
        primary = primary.split("/", 1)[1]
    candidates = [primary, "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    return list(dict.fromkeys([item for item in candidates if item]))


def _call_gemini_plan(
    *,
    provider: Dict[str, Any],
    instruction: str,
    channel: str,
    platform: str,
    context: Dict[str, Any],
    screen: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    from server.services.ai.plan.prompt import build_ai_plan_messages

    base = str(provider.get("base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
    api_key = str(provider.get("api_key") or "").strip()
    model = str(provider.get("model") or "gemini-2.5-flash").strip()
    if not api_key or not model:
        return None

    ctx_text = json.dumps(context or {}, ensure_ascii=False, default=str)[:4000]
    drift_replan = (context or {}).get("drift_replan")
    if not isinstance(drift_replan, dict):
        drift_replan = None
    goal_continue_replan = (context or {}).get("goal_continue_replan")
    if not isinstance(goal_continue_replan, dict):
        goal_continue_replan = None
    messages = build_ai_plan_messages(
        instruction=instruction,
        platform=platform,
        channel=channel,
        context=ctx_text,
        provider_id=str(provider.get("id") or ""),
        model=model,
        screen=screen,
        drift_replan=drift_replan,
        goal_continue_replan=goal_continue_replan,
    )
    prompt = "\n\n".join(
        f"{item.get('role', 'user')}: {item.get('content', '')}"
        for item in messages
        if item.get("content")
    )
    prompt += (
        "\n\n请只返回 JSON，不要 Markdown。坐标基于 screen.preview_width×preview_height。格式："
        "{\"reply\":\"...\",\"steps\":[{\"kind\":\"click|input|swipe|open_app|close_app|back|system_key|ability\","
        "\"x\":123,\"y\":456,\"coords_explicit\":true,\"label\":\"审计文案\",\"summary\":\"...\"}],\"auto_run\":true,\"plan_complete\":true}。"
    )
    parts: List[Dict[str, Any]] = [{"text": prompt}]
    if (screen or {}).get("base64"):
        parts.append(
            {
                "inline_data": {
                    "mime_type": screen.get("mime_type") or "image/jpeg",
                    "data": screen.get("base64"),
                }
            }
        )

    last_err: Optional[Exception] = None
    resp = None
    for candidate in _gemini_model_candidates(model):
        try:
            resp = requests.post(
                f"{base}/models/{candidate}:generateContent",
                params={"key": api_key},
                headers={"Content-Type": "application/json"},
                json={
                    "contents": [{"role": "user", "parts": parts}],
                    "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
                },
                timeout=_ai_plan_request_timeout(channel),
            )
            resp.raise_for_status()
            break
        except requests.HTTPError as e:
            last_err = e
            status = getattr(e.response, "status_code", None)
            if status != 404:
                raise e
            SLog.w(TAG, f"Gemini model not found, try fallback model={candidate}")
    else:
        raise RuntimeError(_sanitize_llm_error(last_err or RuntimeError("Gemini model not found")))

    data = resp.json()
    parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    content = "\n".join(str(part.get("text") or "") for part in parts if part.get("text"))
    return _extract_json_object(content)


def verify_expectation_with_ai(
    expected_text: str,
    *,
    sn: Optional[str],
    platform: str = "android",
    context: Optional[Dict[str, Any]] = None,
    provider_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Use configured LLM + screenshot to verify a case expectation."""
    from server.services import system_settings_service as ss
    from server.services.ai.plan.prompt import build_ai_assert_messages

    exp = re.sub(r"^\d+[.、．)\）]\s*", "", (expected_text or "").strip())
    if not exp or not sn:
        return None

    gate = ss.should_use_ai_planning("case_execution", provider_id=provider_id)
    if not gate.get("enabled"):
        return None

    selected_provider_id = provider_id or (gate.get("provider") or {}).get("id") or ""
    provider = ss.get_ai_provider_credentials(selected_provider_id)
    ctx = dict(context or {})
    platform = str(ctx.get("platform") or platform or "android").lower()
    llm_ctx = {k: v for k, v in ctx.items() if k != "icon_targets"}
    screen = _build_ai_screen_context(
        sn,
        platform,
        provider_id=selected_provider_id,
        expected_package=str(ctx.get("package") or ""),
    )
    if screen:
        llm_ctx["screen"] = _screen_context_for_llm(
            screen,
            provider_id=selected_provider_id,
            model=str(provider.get("model") or ""),
        )

    instruction = f"验证预期：{exp}"
    ctx_text = json.dumps({**llm_ctx, "sn": sn}, ensure_ascii=False, default=str)[:4000]
    plan_messages = build_ai_assert_messages(
        expected_text=exp,
        platform=platform,
        channel="case_execution",
        context=ctx_text,
    )

    raw: Optional[Dict[str, Any]] = None
    err_info: Optional[Dict[str, Any]] = None
    try:
        api_type = str(provider.get("api_type") or "").strip().lower()
        base_url = str(provider.get("base_url") or "")
        if api_type == "gemini" or provider.get("id") == "google" or "generativelanguage.googleapis.com" in base_url:
            messages = plan_messages
            prompt = "\n\n".join(
                f"{item.get('role', 'user')}: {item.get('content', '')}"
                for item in messages
                if item.get("content")
            )
            prompt += (
                '\n\n请只返回 JSON：{"passed":true,"reply":"...","reason":"...","evidence":"..."}'
            )
            parts: List[Dict[str, Any]] = [{"text": prompt}]
            if screen.get("base64"):
                parts.append(
                    {
                        "inline_data": {
                            "mime_type": screen.get("mime_type") or "image/jpeg",
                            "data": screen.get("base64"),
                        }
                    }
                )
            base = str(provider.get("base_url") or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
            api_key = str(provider.get("api_key") or "").strip()
            model = str(provider.get("model") or "gemini-2.5-flash").strip()
            resp = requests.post(
                f"{base}/models/{model}:generateContent?key={api_key}",
                json={
                    "contents": [{"role": "user", "parts": parts}],
                    "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
                },
                timeout=_ai_plan_request_timeout("case_execution"),
            )
            resp.raise_for_status()
            data = resp.json()
            content_parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
            content = "\n".join(str(part.get("text") or "") for part in content_parts if part.get("text"))
            raw = _extract_json_object(content)
        elif api_type == "anthropic" or provider.get("id") in {"anthropic", "umodelverse"}:
            base = str(provider.get("base_url") or "").rstrip("/")
            api_key = str(provider.get("api_key") or "").strip()
            model = str(provider.get("model") or "").strip()
            system_parts = [m["content"] for m in plan_messages if m.get("role") == "system"]
            user_parts = [m["content"] for m in plan_messages if m.get("role") != "system"]
            user_parts.append('请只返回 JSON：{"passed":true,"reply":"...","reason":"...","evidence":"..."}')
            content_blocks: List[Dict[str, Any]] = [{"type": "text", "text": "\n\n".join(user_parts)}]
            if screen.get("base64"):
                content_blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": screen.get("mime_type") or "image/jpeg",
                            "data": screen.get("base64"),
                        },
                    }
                )
            resp = requests.post(
                f"{base}/v1/messages",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": 1024,
                    "temperature": 0.1,
                    "system": "\n\n".join(system_parts),
                    "messages": [{"role": "user", "content": content_blocks}],
                },
                timeout=_ai_plan_request_timeout("case_execution"),
            )
            resp.raise_for_status()
            blocks = resp.json().get("content") or []
            content = "\n".join(str(block.get("text") or "") for block in blocks if isinstance(block, dict))
            raw = _extract_json_object(content)
        else:
            messages = list(plan_messages)
            messages.append(
                {
                    "role": "user",
                    "content": '请只返回 JSON：{"passed":true,"reply":"...","reason":"...","evidence":"..."}',
                }
            )
            messages = _append_openai_image(messages, screen or {})
            base = str(provider.get("base_url") or "").rstrip("/")
            api_key = str(provider.get("api_key") or "").strip()
            model = str(provider.get("model") or "").strip()
            from server.services.ai.plan.prompt import volcengine_chat_payload_extras

            resp = requests.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": messages,
                    "temperature": 0.1,
                    "max_tokens": 2048,
                    **volcengine_chat_payload_extras(
                        provider_id=str(provider.get("id") or ""),
                        model=model,
                    ),
                },
                timeout=_ai_plan_request_timeout("case_execution"),
            )
            resp.raise_for_status()
            raw, _parse_meta = _parse_openai_chat_completion(resp.json())
    except Exception as e:
        err_info = _llm_error_info(e, provider)
        SLog.w(TAG, f"AI assert failed provider={provider.get('id')}: {_sanitize_llm_error(e)}")
        return {
            "ok": False,
            "msg": err_info.get("message") or str(e),
            "reply": err_info.get("title") or "大模型预期校验失败",
            "checks": [{"text": exp, "ok": False, "reason": err_info.get("message") or "AI 校验失败"}],
            "planner": {
                "mode": "ai",
                "provider_id": provider.get("id"),
                "model": provider.get("model"),
                "channel": "case_execution",
                "task": "assert",
            },
            "ai_debug": {
                "provider": provider.get("id"),
                "model": provider.get("model"),
                "screen": _ai_debug_screen_context(
                    screen,
                    provider_id=str(provider.get("id") or ""),
                    model=str(provider.get("model") or ""),
                ),
                "error": _sanitize_llm_error(e),
                "error_info": err_info,
            },
        }

    if not raw:
        return {
            "ok": False,
            "msg": "大模型未返回可解析的预期校验结果",
            "reply": "大模型未返回预期校验结果",
            "checks": [{"text": exp, "ok": False, "reason": "AI 返回空结果"}],
            "planner": {
                "mode": "ai",
                "provider_id": provider.get("id"),
                "model": provider.get("model"),
                "channel": "case_execution",
                "task": "assert",
            },
            "ai_debug": {
                "provider": provider.get("id"),
                "model": provider.get("model"),
                "screen": _ai_debug_screen_context(
                    screen,
                    provider_id=str(provider.get("id") or ""),
                    model=str(provider.get("model") or ""),
                ),
                "raw_response": None,
            },
        }

    passed = raw.get("passed")
    if passed is None:
        passed = raw.get("ok")
    ok = bool(passed)
    reason = str(raw.get("reason") or raw.get("evidence") or raw.get("reply") or "").strip()
    reply = str(raw.get("reply") or (f"预期{'达成' if ok else '未达成'}：{exp}")).strip()
    ai_debug = {
        "provider": provider.get("id"),
        "model": provider.get("model"),
        "screen": _ai_debug_screen_context(
            screen,
            provider_id=str(provider.get("id") or ""),
            model=str(provider.get("model") or ""),
        ),
        "raw_response": raw,
        "task": "assert",
    }
    return {
        "ok": ok,
        "msg": reply if ok else (reason or reply or "预期未达成"),
        "reply": reply,
        "checks": [{"text": exp, "ok": ok, "reason": reason or reply, "method": "ai_assert"}],
        "planner": {
            "mode": "ai",
            "provider_id": provider.get("id"),
            "model": provider.get("model"),
            "channel": "case_execution",
            "task": "assert",
        },
        "ai_debug": ai_debug,
    }


def _plan_message_ai(
    text: str,
    *,
    sn: Optional[str],
    context: Optional[Dict],
    channel: str,
    provider_id: str,
) -> Optional[Dict[str, Any]]:
    from server.services import system_settings_service as ss

    ctx = dict(context or {})
    platform = str(ctx.get("platform") or "android").lower()
    normalized_channel = (channel or "copilot").strip().lower()
    case_channels = {"case", "case_execution", "regression", "feishu"}
    if text and not ctx.get("case_step_text"):
        ctx["case_step_text"] = text
    known_apps = _ai_known_apps_context(platform)
    if known_apps:
        ctx["known_apps"] = known_apps
    llm_ctx = {k: v for k, v in ctx.items() if k != "icon_targets"}
    provider = ss.get_ai_provider_credentials(provider_id)
    screen = _build_ai_screen_context(
        sn,
        platform,
        provider_id=provider_id,
        expected_package=str(ctx.get("package") or ""),
    )
    if screen:
        llm_ctx["screen"] = _screen_context_for_llm(
            screen,
            provider_id=str(provider.get("id") or provider_id),
            model=str(provider.get("model") or ""),
        )
    plan_parse_meta: Dict[str, Any] = {}
    try:
        api_type = str(provider.get("api_type") or "").strip().lower()
        base_url = str(provider.get("base_url") or "")
        if api_type == "gemini" or provider.get("id") == "google" or "generativelanguage.googleapis.com" in base_url:
            raw_plan = _call_gemini_plan(
                provider=provider,
                instruction=text,
                channel=channel,
                platform=platform,
                context={**llm_ctx, "sn": sn},
                screen=screen,
            )
        elif api_type == "anthropic" or provider.get("id") in {"anthropic", "umodelverse"}:
            raw_plan = _call_anthropic_compatible_plan(
                provider=provider,
                instruction=text,
                channel=channel,
                platform=platform,
                context={**llm_ctx, "sn": sn},
                screen=screen,
            )
        else:
            raw_plan, plan_parse_meta = _call_openai_compatible_plan(
                provider=provider,
                instruction=text,
                channel=channel,
                platform=platform,
                context={**llm_ctx, "sn": sn},
                screen=screen,
            )
    except Exception as e:
        source_err = e.__cause__ if isinstance(e, RuntimeError) and e.__cause__ else e
        err_text = _sanitize_llm_error(source_err)
        err_info = _llm_error_info(source_err, provider)
        SLog.w(TAG, f"AI plan failed provider={provider.get('id')}: {err_text}")
        return {
            "reply": f"{err_info['title']}：{err_info['message']}\n建议：{err_info['suggestion']}",
            "steps": [],
            "navigate": None,
            "auto_run": False,
            "ai_error": err_text,
            "ai_error_info": err_info,
        }
    if not raw_plan:
        compress_on = bool((screen or {}).get("compress_image", True))
        hint = ""
        if not compress_on:
            hint = "（已关闭 Plan 截图压缩，原图较大时部分模型可能返回空内容，可尝试开启压缩）"
        SLog.w(
            TAG,
            f"AI plan empty JSON provider={provider.get('id')} channel={channel} "
            f"instruction={text[:120]!r} compress={compress_on}",
        )
        return {
            "reply": f"大模型未返回可解析的 Plan JSON{hint}",
            "steps": [],
            "navigate": None,
            "auto_run": False,
            "planner": {
                "mode": "ai",
                "provider_id": provider.get("id"),
                "model": provider.get("model"),
                "channel": channel,
                "reason": "json_parse_failed",
            },
            "ai_debug": {
                "provider": provider.get("id"),
                "model": provider.get("model"),
                "screen": _ai_debug_screen_context(
                    screen or {},
                    provider_id=str(provider.get("id") or ""),
                    model=str(provider.get("model") or ""),
                ),
                "compress_image": compress_on,
                "parse_failed": True,
                "parse_meta": plan_parse_meta,
                "raw_content_preview": plan_parse_meta.get("content_preview"),
            },
        }
    raw_plan = _coerce_ai_plan_steps(raw_plan)
    from server.services.ai.plan.prompt import _is_volcengine_doubao_provider, build_ai_plan_prompt_preview

    is_doubao = _is_volcengine_doubao_provider(
        str(provider.get("id") or provider_id or ""),
        str(provider.get("model") or ""),
    )
    raw_plan_model_response = json.loads(json.dumps(raw_plan, ensure_ascii=False, default=str))
    doubao_coord_convert: Dict[str, Any] = {}
    if is_doubao and screen:
        raw_plan, doubao_coord_convert = _convert_doubao_plan_to_device_coords(raw_plan, screen)
    raw_plan_before_scale = json.loads(json.dumps(raw_plan, ensure_ascii=False, default=str))
    prompt_preview = build_ai_plan_prompt_preview(
        provider_id=str(provider.get("id") or provider_id or ""),
        model=str(provider.get("model") or ""),
        screen=screen,
        drift_replan=ctx.get("drift_replan") if isinstance(ctx.get("drift_replan"), dict) else None,
        goal_continue_replan=(
            ctx.get("goal_continue_replan")
            if isinstance(ctx.get("goal_continue_replan"), dict)
            else None
        ),
    )
    coord_retry_log: List[Dict[str, Any]] = []
    openai_compatible = not (
        api_type == "gemini"
        or provider.get("id") == "google"
        or "generativelanguage.googleapis.com" in base_url
        or api_type == "anthropic"
        or provider.get("id") in {"anthropic", "umodelverse"}
    )
    if is_doubao:
        preview_coord_violations = _validate_device_coord_violations(
            raw_plan_before_scale.get("steps"),
            screen,
        )
        coord_space = "device"
    else:
        preview_coord_violations, coord_space = _analyze_ai_plan_coordinates(
            raw_plan_before_scale.get("steps"),
            screen,
        )
    coord_retry_log.append(
        {
            "attempt": 1,
            "violations": preview_coord_violations,
            "coord_space": coord_space,
            "raw_plan": raw_plan_before_scale,
            "raw_plan_model": raw_plan_model_response,
            "doubao_coord_convert": doubao_coord_convert,
        }
    )
    if coord_space == "device" and not is_doubao:
        SLog.i(
            TAG,
            f"AI plan coords treated as device space channel={channel} "
            f"preview={(screen or {}).get('preview_width')}x{(screen or {}).get('preview_height')} "
            f"device={(screen or {}).get('width')}x{(screen or {}).get('height')}",
        )
    if preview_coord_violations and openai_compatible:
        correction = _build_preview_coord_correction_message(
            preview_coord_violations,
            screen,
            provider_id=str(provider.get("id") or ""),
            model=str(provider.get("model") or ""),
        )
        SLog.w(
            TAG,
            f"AI plan preview coords out of bounds, retrying once channel={channel} "
            f"violations={preview_coord_violations[:2]}",
        )
        try:
            retry_plan, retry_meta = _call_openai_compatible_plan(
                provider=provider,
                instruction=text,
                channel=channel,
                platform=platform,
                context={**llm_ctx, "sn": sn},
                screen=screen,
                extra_user_messages=[{"content": correction}],
            )
            plan_parse_meta = {**plan_parse_meta, "coord_retry_parse_meta": retry_meta}
            if retry_plan:
                raw_plan = _coerce_ai_plan_steps(retry_plan)
                raw_plan_model_response = json.loads(
                    json.dumps(raw_plan, ensure_ascii=False, default=str)
                )
                if is_doubao and screen:
                    raw_plan, doubao_coord_convert = _convert_doubao_plan_to_device_coords(
                        raw_plan, screen
                    )
                raw_plan_before_scale = json.loads(
                    json.dumps(raw_plan, ensure_ascii=False, default=str)
                )
                if is_doubao:
                    preview_coord_violations = _validate_device_coord_violations(
                        raw_plan_before_scale.get("steps"),
                        screen,
                    )
                    coord_space = "device"
                else:
                    preview_coord_violations, coord_space = _analyze_ai_plan_coordinates(
                        raw_plan_before_scale.get("steps"),
                        screen,
                    )
                coord_retry_log.append(
                    {
                        "attempt": 2,
                        "violations": preview_coord_violations,
                        "coord_space": coord_space,
                        "raw_plan": raw_plan_before_scale,
                        "raw_plan_model": raw_plan_model_response,
                        "doubao_coord_convert": doubao_coord_convert,
                        "correction": correction,
                    }
                )
        except Exception as e:
            SLog.w(TAG, f"AI plan coord correction retry failed: {e}")
            coord_retry_log.append({"attempt": 2, "error": str(e)})
    if preview_coord_violations:
        first = preview_coord_violations[0]
        issues_text = "；".join(first.get("issues") or [])
        SLog.w(
            TAG,
            f"AI plan preview coords out of bounds channel={channel} "
            f"violations={preview_coord_violations[:3]}",
        )
        return {
            "reply": (
                "大模型返回的坐标超出附图范围，已拒绝执行。"
                f"（附图 {first.get('preview', {}).get('width')}×"
                f"{first.get('preview', {}).get('height')}，{issues_text}）"
            ),
            "display_reply": "坐标超出附图范围",
            "steps": [],
            "navigate": None,
            "sn": sn,
            "auto_run": False,
            "plan_complete": False,
            "planner": {
                "mode": "ai",
                "provider_id": provider.get("id"),
                "model": provider.get("model"),
                "channel": channel,
                "reason": "preview_coords_out_of_bounds",
            },
            "ai_debug": {
                "provider": provider.get("id"),
                "model": provider.get("model"),
                "screen": _ai_debug_screen_context(
                    screen or {},
                    provider_id=str(provider.get("id") or ""),
                    model=str(provider.get("model") or ""),
                ),
                "raw_plan": raw_plan_before_scale,
                "raw_plan_model": raw_plan_model_response,
                "prompt_preview": prompt_preview,
                "image_pipeline": _ai_image_pipeline_debug(screen),
                "doubao_coord_convert": doubao_coord_convert,
                "coord_retry_log": coord_retry_log,
                "preview_coord_violations": preview_coord_violations,
            },
            "ai_error_info": {
                "type": "preview_coords_out_of_bounds",
                "title": "坐标超出附图范围",
                "message": (
                    "视觉坐标必须基于发给模型的附图像素（screen.width × screen.height），"
                    "禁止使用设备分辨率或自行换算比例。"
                ),
                "suggestion": (
                    f"坐标须满足 x∈[1,{first.get('preview', {}).get('width')}]、"
                    f"y∈[1,{first.get('preview', {}).get('height')}]。"
                ),
            },
        }
    raw_plan, coordinate_scale = _scale_ai_plan_coordinates(
        json.loads(json.dumps(raw_plan_before_scale, ensure_ascii=False, default=str)),
        screen,
        coord_space=coord_space,
    )
    if coord_retry_log:
        coordinate_scale = {**coordinate_scale, "coord_retry_log": coord_retry_log}
    raw_steps = raw_plan.get("steps") or []
    normalized_steps = [
        s
        for s in (
            _normalize_ai_step(item, require_visual_coordinates=True)
            for item in raw_steps
        )
        if s
    ]
    visual_kinds = {"click", "tap", "input", "type", "text", "swipe", "scroll"}
    invalid_visual_steps = [
        item
        for item in raw_steps
        if isinstance(item, dict)
        and str(item.get("kind") or item.get("type") or item.get("action") or "").strip().lower() in visual_kinds
        and not _normalize_ai_step(item, require_visual_coordinates=True)
    ]
    if raw_steps and not normalized_steps:
        SLog.w(
            TAG,
            f"AI plan normalize dropped all steps channel={channel} "
            f"raw_count={len(raw_steps)} invalid={invalid_visual_steps[:2]}",
        )
    ai_debug = {
        "provider": provider.get("id"),
        "model": provider.get("model"),
        "screen": _ai_debug_screen_context(
            screen,
            provider_id=str(provider.get("id") or ""),
            model=str(provider.get("model") or ""),
        ),
        "raw_plan": raw_plan_before_scale,
        "raw_plan_model": raw_plan_model_response,
        "prompt_preview": prompt_preview,
        "image_pipeline": _ai_image_pipeline_debug(screen),
        "doubao_coord_convert": doubao_coord_convert,
        "coordinate_scale": coordinate_scale,
        "normalized_steps": normalized_steps,
        "invalid_visual_steps": invalid_visual_steps,
    }
    if invalid_visual_steps:
        return {
            "reply": "大模型未返回可直接执行的坐标：视觉动作必须包含 x/y 或起止坐标，不能只返回 label/direction。",
            "display_reply": "大模型未返回坐标",
            "steps": [],
            "navigate": None,
            "sn": sn,
            "auto_run": False,
            "plan_complete": False,
            "planner": {
                "mode": "ai",
                "provider_id": provider.get("id"),
                "model": provider.get("model"),
                "channel": channel,
                "reason": "AI visual steps missing coordinates",
            },
            "ai_debug": ai_debug,
            "ai_error_info": {
                "type": "missing_coordinates",
                "title": "大模型未返回坐标",
                "message": "AI 模式下 click/input/swipe 必须返回坐标，label 只能用于审计展示。",
                "suggestion": "请重试或换用支持视觉输入的模型；如果截图不清晰，可先等待页面稳定后再执行。",
            },
        }
    blockers = raw_plan.get("blockers") or []
    from server.services.executor.plan_execute_service import infer_plan_complete, step_fulfills_case_command

    plan_complete_raw = raw_plan.get("plan_complete")
    inferred_complete = infer_plan_complete(
        text,
        normalized_steps,
        str(raw_plan.get("reply") or ""),
    )
    fulfills_command = step_fulfills_case_command(text, normalized_steps)
    if plan_complete_raw is None:
        plan_complete = inferred_complete
    else:
        plan_complete = bool(plan_complete_raw)
        if plan_complete and not inferred_complete and not fulfills_command:
            SLog.i(
                TAG,
                f"AI plan_complete overridden to false channel={channel} "
                f"instruction={text[:48]!r}",
            )
            plan_complete = False
    if fulfills_command:
        plan_complete = True
    # Copilot 对话：步骤已被 Server 规范化（含包名纠错）后应自动执行；
    # 模型因包名猜测不确定而设 auto_run=false 不应阻断执行。
    auto_run = bool(normalized_steps) or raw_plan.get("auto_run", True)
    if normalized_steps and raw_plan.get("auto_run") is False:
        SLog.i(
            TAG,
            f"AI plan override auto_run=true steps={len(normalized_steps)} "
            f"blockers={len(blockers)}",
        )
    return {
        "reply": raw_plan.get("reply") or ("大模型已生成计划" if normalized_steps else "大模型未生成可执行步骤"),
        "display_reply": raw_plan.get("reply") or "",
        "steps": normalized_steps,
        "navigate": raw_plan.get("navigate"),
        "sn": sn,
        "auto_run": auto_run,
        "plan_complete": plan_complete,
        "planner": {
            "mode": "ai",
            "provider_id": provider.get("id"),
            "model": provider.get("model"),
            "channel": channel,
        },
        "ai_debug": {**ai_debug, "blockers": blockers, "auto_run_raw": raw_plan.get("auto_run")},
    }


def plan_message(
    text: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
    channel: str = "copilot",
    provider_id: Optional[str] = None,
    planning_mode: str = "local",
) -> Dict[str, Any]:
    """统一 Planner：Copilot 与用例执行共享；用例预期检查由调用方额外处理。"""
    from server.services import system_settings_service as ss

    mode = (planning_mode or "local").strip().lower()
    normalized_channel = (channel or "copilot").strip().lower()
    case_channels = {"case", "case_execution", "regression", "feishu"}
    ai_gate: Optional[Dict[str, Any]] = None

    # 用例/回归通道未显式指定 planning_mode=ai 时，读取密钥配置决定是否走大模型。
    if mode == "local" and normalized_channel in case_channels:
        ai_gate = ss.should_use_ai_planning(normalized_channel, provider_id=provider_id)
        if ai_gate.get("enabled"):
            mode = "ai"
            if not provider_id:
                provider_id = (ai_gate.get("provider") or {}).get("id") or None
            SLog.i(
                TAG,
                f"case planning use AI channel={normalized_channel} provider={provider_id}",
            )

    if mode == "local":
        local_plan = _plan_message_local(text, sn=sn, context=context)
        local_plan["planner"] = {"mode": "local", "channel": channel}
        return local_plan

    ai_gate = ai_gate or ss.should_use_ai_planning(normalized_channel, provider_id=provider_id)
    if ai_gate.get("enabled"):
        selected_provider_id = provider_id or (ai_gate.get("provider") or {}).get("id") or ""
        ai_plan = _plan_message_ai(
            text,
            sn=sn,
            context=context,
            channel=channel,
            provider_id=selected_provider_id,
        )
        if ai_plan and ai_plan.get("steps"):
            return ai_plan
        usage = ss.get_ai_usage_settings()
        allow_local_fallback = (
            ai_gate.get("mode") == "local_first"
            and normalized_channel in case_channels
            and not usage.get("case_execution_enabled")
        )
        if allow_local_fallback:
            SLog.i(
                TAG,
                f"case AI plan has no steps, fallback to local channel={normalized_channel}",
            )
            local_plan = _plan_message_local(text, sn=sn, context=context)
            local_plan["planner"] = {
                "mode": "local",
                "channel": channel,
                "ai_fallback": True,
                "ai_reply": (ai_plan or {}).get("reply"),
            }
            if ai_plan and ai_plan.get("ai_debug"):
                local_plan["ai_debug"] = ai_plan["ai_debug"]
            return local_plan
        if normalized_channel in case_channels and usage.get("case_execution_enabled"):
            SLog.w(
                TAG,
                f"case AI plan failed, no local fallback (case_execution_enabled) "
                f"reply={(ai_plan or {}).get('reply')!r}",
            )
        return ai_plan or {
            "reply": "大模型未返回可执行步骤，请调整指令或切换 Local Plan。",
            "display_reply": "大模型未返回可执行步骤",
            "steps": [],
            "navigate": None,
            "sn": sn,
            "auto_run": False,
            "plan_complete": False,
            "planner": {
                "mode": "ai",
                "channel": channel,
                "reason": "AI did not return executable steps",
                "requested_provider": selected_provider_id,
            },
        }

    return {
        "reply": f"当前选择的是大模型能力，但不可用：{ai_gate.get('reason') or 'provider unavailable'}",
        "display_reply": "大模型能力不可用",
        "steps": [],
        "navigate": None,
        "sn": sn,
        "auto_run": False,
        "plan_complete": False,
        "planner": {
            "mode": "ai",
            "channel": channel,
        "reason": ai_gate.get("reason") or "AI planning disabled",
        "requested_provider": provider_id or (ai_gate.get("provider") or {}).get("id"),
        },
    }


