# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
页面类型配置注册表。

login / home / feed / profile 等均为同一套 PageProfile 的实例；
识别结果来自 page_context（图谱/Figma/OCR），再映射到 profile 调整通道权重。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ChannelWeights:
    """多通道打分权重（和为 1 附近即可，仲裁器会做归一）。"""

    clip: float = 0.30
    ocr: float = 0.30
    hierarchy: float = 0.25
    gallery: float = 0.35
    icon_row: float = 0.35
    anchor: float = 0.55

    def as_dict(self) -> Dict[str, float]:
        return {
            "clip": self.clip,
            "ocr": self.ocr,
            "hierarchy": self.hierarchy,
            "gallery": self.gallery,
            "icon_row": self.icon_row,
            "anchor": self.anchor,
        }


@dataclass(frozen=True)
class PageProfile:
    key: str
    title: str
    label_patterns: List[str]
    screen_text_patterns: List[str] = field(default_factory=list)
    weights: ChannelWeights = field(default_factory=ChannelWeights)
    prefer_icon_row: bool = False
    description: str = ""

    def matches(self, page_label: str, screen_text: str = "") -> bool:
        blob = f"{page_label}\n{screen_text}"
        for pat in self.label_patterns:
            if re.search(pat, page_label or "", re.I):
                return True
        for pat in self.screen_text_patterns:
            if re.search(pat, blob, re.I):
                return True
        return False


# 内置 profile；应用可在 DB/配置中扩展（见 docs/locate/extending.md）
_BUILTIN_PROFILES: List[PageProfile] = [
    PageProfile(
        key="consent",
        title="隐私同意弹窗",
        label_patterns=[r"consent", r"隐私", r"协议弹"],
        screen_text_patterns=[r"不同意", r"造物者", r"隐私条款", r"用户协议", r"点击.*同意"],
        weights=ChannelWeights(clip=0.24, ocr=0.42, hierarchy=0.38, gallery=0.12, icon_row=0.08),
        description="底部同意/不同意按钮，优先 OCR/hierarchy",
    ),
    PageProfile(
        key="system_dialog",
        title="系统权限 / 系统弹窗",
        label_patterns=[r"权限", r"permission", r"允许"],
        screen_text_patterns=[
            r"仅在使用中允许",
            r"始终允许",
            r"是否.*允许",
            r"获取.*信息",
            r"拒绝",
        ],
        weights=ChannelWeights(clip=0.22, ocr=0.38, hierarchy=0.42, gallery=0.18, icon_row=0.12),
        description="系统权限等短文案按钮，优先 hierarchy/OCR",
    ),
    PageProfile(
        key="login",
        title="登录页",
        label_patterns=[r"登录", r"login", r"sign\s*in", r"一键登录"],
        screen_text_patterns=[r"一键登录", r"本机号码", r"验证码", r"访客浏览"],
        weights=ChannelWeights(clip=0.32, ocr=0.28, hierarchy=0.22, gallery=0.38, icon_row=0.40),
        prefer_icon_row=True,
        description="含第三方无字图标行、协议勾选、一键登录主按钮",
    ),
    PageProfile(
        key="home",
        title="首页 / Feed",
        label_patterns=[r"首页", r"home", r"feed", r"推荐", r"发现"],
        screen_text_patterns=[r"造物秀", r"关注", r"推荐流"],
        weights=ChannelWeights(clip=0.28, ocr=0.35, hierarchy=0.30, gallery=0.30, icon_row=0.25),
        description="信息流、卡片、顶部分段",
    ),
    PageProfile(
        key="profile",
        title="我的 / 个人中心",
        label_patterns=[r"我的", r"个人", r"profile", r"mine", r"设置入口"],
        screen_text_patterns=[r"设置", r"账号", r"退出登录"],
        weights=ChannelWeights(clip=0.28, ocr=0.34, hierarchy=0.30, gallery=0.32, icon_row=0.22),
    ),
    PageProfile(
        key="settings",
        title="设置页",
        label_patterns=[r"设置", r"settings", r"偏好"],
        weights=ChannelWeights(clip=0.22, ocr=0.40, hierarchy=0.35, gallery=0.20, icon_row=0.15),
        description="以文字列表项为主",
    ),
    PageProfile(
        key="generic",
        title="通用页面",
        label_patterns=[],
        weights=ChannelWeights(),
        description="未识别页面类型时的默认权重",
    ),
]

_PROFILE_BY_KEY: Dict[str, PageProfile] = {p.key: p for p in _BUILTIN_PROFILES}


def list_page_profiles() -> List[PageProfile]:
    return list(_BUILTIN_PROFILES)


def get_page_profile(key: str) -> PageProfile:
    return _PROFILE_BY_KEY.get(key) or _PROFILE_BY_KEY["generic"]


def resolve_page_profile(
    *,
    page_context: Optional[Dict] = None,
    screen_text: str = "",
) -> PageProfile:
    """
    根据 page_context（identify_page_for_trace 返回值）解析页面类型。
    优先 label，其次 OCR 全文；均未命中则 generic。
    """
    pc = page_context or {}
    label = (
        pc.get("current_page_label")
        or pc.get("label")
        or pc.get("figma_best")
        or ""
    )
    blob = screen_text or pc.get("screen_text") or ""

    if re.search(
        r"仅在使用中允许|始终允许|是否.{0,8}允许|获取已安装|获取.*应用.*信息",
        blob,
        re.I,
    ):
        return get_page_profile("system_dialog")

    for prof in _BUILTIN_PROFILES:
        if prof.key == "generic":
            continue
        if prof.matches(label, blob):
            return prof
    return get_page_profile("generic")


def register_page_profile(profile: PageProfile) -> None:
    """运行时注册自定义页面类型（测试或插件）。"""
    global _BUILTIN_PROFILES, _PROFILE_BY_KEY
    _BUILTIN_PROFILES = [p for p in _BUILTIN_PROFILES if p.key != profile.key] + [profile]
    _PROFILE_BY_KEY = {p.key: p for p in _BUILTIN_PROFILES}
