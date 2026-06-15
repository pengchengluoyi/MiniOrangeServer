# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
CLIP / OCR 分路查询表（一期：造好物登录链路 curated；二期：resolver 直接消费 ClipQueryPlan）。

设计：
- clip_query / clip_aliases：视觉语义，供 CLIP 通道与图标库 embedding
- ocr_queries：屏上文字匹配，供 OCR / Hierarchy 文本通道
- 不按业务写专用定位器；新控件靠表项 + 图标库 aliases 扩展
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ClipQueryPlan:
    label_key: str
    clip_query: str
    clip_aliases: Tuple[str, ...] = ()
    ocr_queries: Tuple[str, ...] = ()
    region: Optional[str] = None
    icon_row: bool = False


def _plan(
    label_key: str,
    clip_query: str,
    *,
    clip_aliases: Optional[List[str]] = None,
    ocr_queries: Optional[List[str]] = None,
    region: Optional[str] = None,
    icon_row: bool = False,
) -> ClipQueryPlan:
    return ClipQueryPlan(
        label_key=label_key,
        clip_query=clip_query,
        clip_aliases=tuple(clip_aliases or ()),
        ocr_queries=tuple(ocr_queries or ()),
        region=region,
        icon_row=icon_row,
    )


# 造好物 com.mathmagic.zaohaowu 登录链路（一期）
ZAOHAOWU_LOGIN_CHAIN: Dict[str, ClipQueryPlan] = {
    "consent_agree": _plan(
        "同意",
        "同意",
        clip_aliases=["agree button", "同意按钮", "确认同意"],
        ocr_queries=["同意", "确认", "接受"],
        region="full",
    ),
    "system_permission_while_using": _plan(
        "仅在使用中允许",
        "仅在使用中允许",
        clip_aliases=["while using the app", "allow while using", "使用时允许"],
        ocr_queries=["仅在使用中允许", "使用时允许", "仅使用期间"],
        region="full",
    ),
    "system_permission_always": _plan(
        "始终允许",
        "始终允许",
        clip_aliases=["always allow", "allow always"],
        ocr_queries=["始终允许", "一律允许"],
        region="full",
    ),
    "agreement_checkbox": _plan(
        "底部协议勾选框",
        "empty checkbox",
        clip_aliases=[
            "round checkbox",
            "unchecked checkbox",
            "agreement checkbox bottom",
            "small circle checkbox",
        ],
        ocr_queries=["协议", "勾选", "用户协议", "隐私政策"],
        region="bottom",
    ),
    "one_click_login": _plan(
        "本机号码一键登录",
        "本机号码一键登录",
        clip_aliases=[
            "一键登录",
            "one click login button",
            "本机号码",
            "登录按钮",
            "手机号一键登录",
        ],
        ocr_queries=["本机号码", "一键登录", "手机号"],
        region="full",
    ),
    "login_confirm_continue": _plan(
        "同意并继续",
        "同意并继续",
        clip_aliases=[
            "agree and continue button",
            "同意并继续按钮",
            "continue button purple",
        ],
        ocr_queries=["同意并继续", "同意并继续按钮"],
        region="bottom",
    ),
    "wechat_icon": _plan(
        "微信图标",
        "wechat green chat bubble icon",
        clip_aliases=["微信", "微信登录", "WeChat icon", "wechat icon"],
        ocr_queries=[],
        region="login_row",
        icon_row=True,
    ),
    "account_input": _plan(
        "账号输入框",
        "account username email text input field",
        clip_aliases=["请输入账号", "username input", "email input"],
        ocr_queries=["请输入账号", "请输入邮箱", "请输入手机号", "账号", "用户名"],
        region="full",
        icon_row=False,
    ),
    "password_input": _plan(
        "密码输入框",
        "password text input field",
        clip_aliases=["请输入密码", "password input"],
        ocr_queries=["请输入密码", "密码"],
        region="full",
        icon_row=False,
    ),
    "bottom_tab_mine": _plan(
        "我的",
        "我的",
        clip_aliases=[
            "我的 tab",
            "mine tab",
            "profile tab",
            "profile mine tab icon",
            "个人中心",
        ],
        ocr_queries=["我的", "我"],
        region="bottom",
        icon_row=True,
    ),
}

_LABEL_MATCHERS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"勾选.*协议|协议.*勾选|底部.*勾选", re.I), "agreement_checkbox"),
    (re.compile(r"同意并继续", re.I), "login_confirm_continue"),
    (re.compile(r"^同意$|点击[^并]*同意$|点[^并]*同意$", re.I), "consent_agree"),
    (re.compile(r"微信.*图标|微信图标", re.I), "wechat_icon"),
    (re.compile(r"账号.*输入框|帐号.*输入框|邮箱.*输入框", re.I), "account_input"),
    (re.compile(r"密码.*输入框", re.I), "password_input"),
    (re.compile(r"手机.*输入框|手机号.*输入框", re.I), "account_input"),
    (re.compile(r"底部.*我的|我的.*tab|点击.*我的", re.I), "bottom_tab_mine"),
    (re.compile(r"仅在使用中允许|使用时允许", re.I), "system_permission_while_using"),
    (re.compile(r"始终允许|一律允许", re.I), "system_permission_always"),
    (re.compile(r"一键登录|本机号码", re.I), "one_click_login"),
]


def lookup_clip_query_plan(label: str) -> Optional[ClipQueryPlan]:
    """按自然语言 label 匹配一期 query 表；未命中返回 None（走通用 _clip_search_params）。"""
    raw = (label or "").strip()
    if not raw:
        return None
    for pat, key in _LABEL_MATCHERS:
        if pat.search(raw):
            return ZAOHAOWU_LOGIN_CHAIN.get(key)
    return None


def is_form_input_label(label: str) -> bool:
    """是否为表单输入框类步骤（走 query 表 + 多通道，但排除键盘区/icon_row）。"""
    plan = lookup_clip_query_plan(label or "")
    if plan and plan.label_key in ("account_input", "password_input"):
        return True
    return bool(
        re.search(r"(账号|帐号|密码|手机号|验证码|邮箱).*输入框", (label or "").strip(), re.I)
    )


def form_input_keyboard_max_cy(screen_h: int) -> int:
    """软键盘弹出时，表单输入框通常在屏面上半区。"""
    return int(screen_h * 0.72)


def clip_params_from_plan(plan: ClipQueryPlan, raw_label: str) -> Tuple[str, List[str], Optional[str]]:
    """供 _clip_search_params / resolver 使用的 (query, aliases, region)。region 恒为 None，方位只走 spatial。"""
    aliases = list(plan.clip_aliases)
    if raw_label and raw_label not in aliases and raw_label != plan.clip_query:
        aliases.append(raw_label)
    return plan.clip_query, list(dict.fromkeys(aliases)), None


def ocr_query_from_plan(plan: ClipQueryPlan, fallback: str = "") -> str:
    if plan.ocr_queries:
        return plan.ocr_queries[0]
    return fallback
