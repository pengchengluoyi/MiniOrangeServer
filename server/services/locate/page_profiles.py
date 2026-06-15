# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
页面类型配置注册表。

关键词与权重默认从资源包加载：
  server/resources/locate/page_profiles.yaml
可通过环境变量 LOCATE_PROFILES_PATH 覆盖。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from script.log import SLog

TAG = "PageProfiles"

_DEFAULT_WEIGHTS = {
    "clip": 0.30,
    "ocr": 0.30,
    "hierarchy": 0.25,
    "gallery": 0.35,
    "icon_row": 0.35,
    "anchor": 0.55,
}


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


@dataclass(frozen=True)
class BootstrapRule:
    profile: str
    screen_text_patterns: List[str]


def default_profiles_resource_path() -> Path:
    return Path(__file__).resolve().parents[2] / "resources" / "locate" / "page_profiles.yaml"


def _weights_from_mapping(raw: Optional[Dict[str, Any]]) -> ChannelWeights:
    src = raw or {}
    return ChannelWeights(
        clip=float(src.get("clip", _DEFAULT_WEIGHTS["clip"])),
        ocr=float(src.get("ocr", _DEFAULT_WEIGHTS["ocr"])),
        hierarchy=float(src.get("hierarchy", _DEFAULT_WEIGHTS["hierarchy"])),
        gallery=float(src.get("gallery", _DEFAULT_WEIGHTS["gallery"])),
        icon_row=float(src.get("icon_row", _DEFAULT_WEIGHTS["icon_row"])),
        anchor=float(src.get("anchor", _DEFAULT_WEIGHTS["anchor"])),
    )


def _profile_from_mapping(row: Dict[str, Any]) -> PageProfile:
    key = str(row.get("key") or "").strip()
    if not key:
        raise ValueError("profile missing key")
    return PageProfile(
        key=key,
        title=str(row.get("title") or key),
        label_patterns=[str(p) for p in (row.get("label_patterns") or []) if str(p).strip()],
        screen_text_patterns=[
            str(p) for p in (row.get("screen_text_patterns") or []) if str(p).strip()
        ],
        weights=_weights_from_mapping(row.get("weights")),
        prefer_icon_row=bool(row.get("prefer_icon_row")),
        description=str(row.get("description") or ""),
    )


def _bootstrap_from_mapping(row: Dict[str, Any]) -> BootstrapRule:
    profile = str(row.get("profile") or "").strip()
    if not profile:
        raise ValueError("bootstrap_rule missing profile")
    return BootstrapRule(
        profile=profile,
        screen_text_patterns=[
            str(p) for p in (row.get("screen_text_patterns") or []) if str(p).strip()
        ],
    )


def load_profiles_resource(
    path: Optional[os.PathLike | str] = None,
) -> Tuple[List[PageProfile], List[BootstrapRule]]:
    resource = Path(path) if path else default_profiles_resource_path()
    if not resource.is_file():
        raise FileNotFoundError(f"page profiles resource not found: {resource}")

    import yaml

    with resource.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    profiles = [_profile_from_mapping(row) for row in (doc.get("profiles") or [])]
    if not profiles:
        raise ValueError(f"page profiles resource empty: {resource}")
    if not any(p.key == "generic" for p in profiles):
        profiles.append(
            PageProfile(
                key="generic",
                title="通用页面",
                label_patterns=[],
                weights=ChannelWeights(),
                description="fallback",
            )
        )

    bootstrap = [
        _bootstrap_from_mapping(row) for row in (doc.get("bootstrap_rules") or [])
    ]
    return profiles, bootstrap


_BUILTIN_PROFILES: List[PageProfile] = []
_PROFILE_BY_KEY: Dict[str, PageProfile] = {}
_BOOTSTRAP_RULES: List[BootstrapRule] = []
_LOADED_FROM: str = ""


def reload_page_profiles(path: Optional[os.PathLike | str] = None) -> None:
    """从资源包重载 profile（测试或热更新）。"""
    global _BUILTIN_PROFILES, _PROFILE_BY_KEY, _BOOTSTRAP_RULES, _LOADED_FROM

    env_path = os.getenv("LOCATE_PROFILES_PATH", "").strip()
    resource = path or env_path or default_profiles_resource_path()
    try:
        profiles, bootstrap = load_profiles_resource(resource)
        _LOADED_FROM = str(Path(resource).resolve())
        SLog.i(TAG, f"loaded {len(profiles)} profiles from {_LOADED_FROM}")
    except Exception as e:
        SLog.w(TAG, f"load page profiles failed ({e}), using empty generic fallback")
        profiles = [
            PageProfile(
                key="generic",
                title="通用页面",
                label_patterns=[],
                weights=ChannelWeights(),
                description="load failed fallback",
            )
        ]
        bootstrap = []
        _LOADED_FROM = "fallback"

    _BUILTIN_PROFILES = profiles
    _BOOTSTRAP_RULES = bootstrap
    _PROFILE_BY_KEY = {p.key: p for p in profiles}


def profiles_resource_path() -> str:
    return _LOADED_FROM


reload_page_profiles()


def list_page_profiles() -> List[PageProfile]:
    return list(_BUILTIN_PROFILES)


def get_page_profile(key: str) -> PageProfile:
    return _PROFILE_BY_KEY.get(key) or _PROFILE_BY_KEY.get("generic") or _BUILTIN_PROFILES[0]


def _match_bootstrap(screen_text: str) -> Optional[PageProfile]:
    blob = screen_text or ""
    for rule in _BOOTSTRAP_RULES:
        for pat in rule.screen_text_patterns:
            if re.search(pat, blob, re.I):
                return get_page_profile(rule.profile)
    return None


def resolve_page_profile(
    *,
    page_context: Optional[Dict] = None,
    screen_text: str = "",
    foreground_package: str = "",
) -> PageProfile:
    """
    根据 page_context（identify_page_for_trace 返回值）解析页面类型。
    优先 label，其次 OCR 全文；均未命中则 generic。
    若已知前台包名，可应用 app_packages.yaml 中的 page_supplements。
    """
    pc = page_context or {}
    label = (
        pc.get("current_page_label")
        or pc.get("label")
        or pc.get("figma_best")
        or ""
    )
    blob = screen_text or pc.get("screen_text") or ""
    fg_pkg = (foreground_package or pc.get("foreground_package") or "").strip()

    boot = _match_bootstrap(blob)
    if boot is not None:
        return boot

    if fg_pkg:
        try:
            from server.services.locate.app_packages import (
                resolve_known_app_by_package,
                resolve_profile_from_app_supplements,
            )

            known = resolve_known_app_by_package(fg_pkg)
            sup_key = resolve_profile_from_app_supplements(
                known, page_label=label, screen_text=blob
            )
            if sup_key:
                return get_page_profile(sup_key)
        except Exception:
            pass

    for prof in _BUILTIN_PROFILES:
        if prof.key == "generic":
            continue
        if prof.matches(label, blob):
            return prof
    return get_page_profile("generic")


def register_page_profile(profile: PageProfile) -> None:
    """运行时注册自定义页面类型（测试或插件）；写入内存，不修改资源包文件。"""
    global _BUILTIN_PROFILES, _PROFILE_BY_KEY
    _BUILTIN_PROFILES = [p for p in _BUILTIN_PROFILES if p.key != profile.key] + [profile]
    _PROFILE_BY_KEY = {p.key: p for p in _BUILTIN_PROFILES}


def profile_key_for_login_step(label: str) -> Optional[str]:
    """
    按步骤文案细化登录链路 profile（覆盖 resolve 前的粗粒度 login）。
    返回 None 表示非登录相关步骤，不覆盖。
    """
    raw = (label or "").strip()
    if not raw:
        return None
    try:
        from server.services.copilot_service import parse_bottom_tab_label

        if parse_bottom_tab_label(raw) and re.search(r"底部|底栏|\btab\b", raw, re.I):
            return "home"
    except Exception:
        pass
    if re.search(r"微信.*图标|微信图标", raw):
        return "login"
    if re.search(r"手机.*图标|手机号.*图标", raw):
        return "phone_login"
    if re.search(r"绑定手机|换绑手机|更换手机", raw):
        return "bind_phone"
    if re.search(r"(账号|帐号|密码|手机号|验证码|邮箱).*输入框", raw):
        return "password_login"
    if re.search(r"手机号注册|新用户注册|注册账号|注册页", raw) and not re.search(
        r"登录注册", raw
    ):
        return "phone_register"
    if re.search(r"验证码", raw):
        return "verify_code"
    if re.search(r"密码登录|账号密码|邮箱密码|帐号密码", raw):
        return "password_login"
    try:
        from server.services.copilot_service import (
            _classify_login_method_intent,
            _is_one_click_login_label,
        )

        if _is_one_click_login_label(raw):
            return "one_click_login"
        intent = _classify_login_method_intent(raw)
        if intent == "one_click":
            return "one_click_login"
        if intent == "phone_sms":
            return "phone_login"
        if intent == "email_password":
            return "password_login"
        if intent in ("wechat", "apple"):
            return "login"
        if intent:
            return "login"
    except Exception:
        pass
    if re.search(r"手机号.*登录|手机登录|短信登录", raw):
        return "phone_login"
    if re.search(r"一键登录|本机号码", raw):
        return "one_click_login"
    if re.search(r"登录|登陆|sign\s*in", raw, re.I):
        return "login"
    return None
