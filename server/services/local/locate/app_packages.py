# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
主流应用包名注册表（资源包驱动）。

资源文件：server/resources/locate/app_packages.yaml
环境变量：LOCATE_APP_PACKAGES_PATH
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from script.log import SLog

TAG = "AppPackages"


@dataclass(frozen=True)
class AppPageSupplement:
    """某 App 前台时，额外的屏文 → profile 映射（在通用 page_profiles 之后、之前视优先级）。"""

    profile: str
    screen_text_patterns: List[str] = field(default_factory=list)
    label_patterns: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class KnownApp:
    key: str
    name: str
    name_en: str = ""
    category: str = ""
    android_packages: Tuple[str, ...] = ()
    ios_bundle_ids: Tuple[str, ...] = ()
    aliases: Tuple[str, ...] = ()
    page_supplements: Tuple[AppPageSupplement, ...] = ()

    def matches_package(self, package: str) -> bool:
        pkg = (package or "").strip().lower()
        if not pkg:
            return False
        for p in self.android_packages:
            if pkg == p.lower():
                return True
        for p in self.ios_bundle_ids:
            if pkg == p.lower():
                return True
        return False

    def matches_alias(self, text: str) -> bool:
        raw = (text or "").strip().lower()
        if not raw:
            return False
        if raw == self.key.lower():
            return True
        for a in self.aliases:
            if raw == a.lower():
                return True
        if self.name and self.name.lower() in raw:
            return True
        if self.name_en and self.name_en.lower() in raw:
            return True
        return False


_BY_PKG: Dict[str, KnownApp] = {}
_BY_KEY: Dict[str, KnownApp] = {}
_ALL_APPS: List[KnownApp] = []
_LOADED_FROM: str = ""


def default_app_packages_resource_path() -> Path:
    return Path(__file__).resolve().parents[3] / "resources" / "locate" / "app_packages.yaml"


def _supplement_from_row(row: Dict[str, Any]) -> AppPageSupplement:
    return AppPageSupplement(
        profile=str(row.get("profile") or "").strip(),
        screen_text_patterns=[
            str(p) for p in (row.get("screen_text_patterns") or []) if str(p).strip()
        ],
        label_patterns=[str(p) for p in (row.get("label_patterns") or []) if str(p).strip()],
    )


def _app_from_row(row: Dict[str, Any]) -> KnownApp:
    key = str(row.get("key") or "").strip()
    if not key:
        raise ValueError("app entry missing key")
    supplements = tuple(
        _supplement_from_row(s) for s in (row.get("page_supplements") or []) if s.get("profile")
    )
    return KnownApp(
        key=key,
        name=str(row.get("name") or key),
        name_en=str(row.get("name_en") or ""),
        category=str(row.get("category") or ""),
        android_packages=tuple(
            str(p).strip().lower()
            for p in (row.get("android_packages") or [])
            if str(p).strip()
        ),
        ios_bundle_ids=tuple(
            str(p).strip().lower()
            for p in (row.get("ios_bundle_ids") or [])
            if str(p).strip()
        ),
        aliases=tuple(str(a).strip() for a in (row.get("aliases") or []) if str(a).strip()),
        page_supplements=supplements,
    )


def load_app_packages_resource(
    path: Optional[os.PathLike | str] = None,
) -> List[KnownApp]:
    resource = Path(path) if path else default_app_packages_resource_path()
    if not resource.is_file():
        raise FileNotFoundError(f"app packages resource not found: {resource}")

    import yaml

    with resource.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}

    apps = [_app_from_row(row) for row in (doc.get("apps") or [])]
    if not apps:
        raise ValueError(f"app packages resource empty: {resource}")
    return apps


def reload_app_packages(path: Optional[os.PathLike | str] = None) -> None:
    global _BY_PKG, _BY_KEY, _ALL_APPS, _LOADED_FROM

    env_path = os.getenv("LOCATE_APP_PACKAGES_PATH", "").strip()
    resource = path or env_path or default_app_packages_resource_path()
    try:
        apps = load_app_packages_resource(resource)
        _LOADED_FROM = str(Path(resource).resolve())
        SLog.i(TAG, f"loaded {len(apps)} known apps from {_LOADED_FROM}")
    except Exception as e:
        SLog.w(TAG, f"load app packages failed ({e})")
        apps = []
        _LOADED_FROM = "fallback"

    by_pkg: Dict[str, KnownApp] = {}
    by_key: Dict[str, KnownApp] = {}
    for app in apps:
        by_key[app.key.lower()] = app
        for pkg in app.android_packages:
            by_pkg[pkg.lower()] = app
        for pkg in app.ios_bundle_ids:
            by_pkg[pkg.lower()] = app
    _BY_PKG = by_pkg
    _BY_KEY = by_key
    _ALL_APPS = apps


def app_packages_resource_path() -> str:
    return _LOADED_FROM


reload_app_packages()


def list_known_apps() -> List[KnownApp]:
    return list(_ALL_APPS)


def get_known_app(key: str) -> Optional[KnownApp]:
    return _BY_KEY.get((key or "").strip().lower())


def resolve_known_app_by_package(package: str) -> Optional[KnownApp]:
    return _BY_PKG.get((package or "").strip().lower())


def resolve_known_app_by_alias(text: str) -> Optional[KnownApp]:
    raw = (text or "").strip()
    if not raw:
        return None
    low = raw.lower()
    if low in _BY_KEY:
        return _BY_KEY[low]
    best: Optional[KnownApp] = None
    best_len = 0
    for app in _ALL_APPS:
        if app.matches_alias(raw):
            n = max(len(app.name), len(app.name_en or ""), len(app.key))
            if n >= best_len:
                best = app
                best_len = n
    return best


def package_for_app_key(key: str, *, platform: str = "android") -> str:
    app = get_known_app(key)
    if not app:
        return ""
    if platform.lower().startswith("ios") and app.ios_bundle_ids:
        return app.ios_bundle_ids[0]
    return app.android_packages[0] if app.android_packages else ""


def get_foreground_package(engine) -> str:
    if not engine:
        return ""
    try:
        if hasattr(engine, "current_package"):
            return (engine.current_package() or "").strip()
        if hasattr(engine, "_ensure_u2"):
            d = engine._ensure_u2()
            if d:
                info = d.app_current() or {}
                return (info.get("package") or "").strip()
    except Exception:
        pass
    return ""


def resolve_profile_from_app_supplements(
    known_app: Optional[KnownApp],
    *,
    page_label: str = "",
    screen_text: str = "",
) -> Optional[str]:
    """前台为已知 App 时，用 page_supplements 尝试更细的 profile。"""
    if not known_app or not known_app.page_supplements:
        return None
    blob = f"{page_label}\n{screen_text}"
    for sup in known_app.page_supplements:
        if not sup.profile:
            continue
        for pat in sup.label_patterns:
            if re.search(pat, page_label or "", re.I):
                return sup.profile
        for pat in sup.screen_text_patterns:
            if re.search(pat, blob, re.I):
                return sup.profile
    return None


def attach_foreground_app_to_context(
    page_ctx: Dict[str, Any],
    engine,
) -> Dict[str, Any]:
    """为 page_context / trace 附加前台包名与已知 App 信息。"""
    out = dict(page_ctx or {})
    pkg = get_foreground_package(engine)
    if not pkg:
        return out
    known = resolve_known_app_by_package(pkg)
    out["foreground_package"] = pkg
    if known:
        out["foreground_app"] = known.key
        out["foreground_app_name"] = known.name
        out["foreground_app_category"] = known.category
    return out


def enrich_locate_debug_app(
    debug: Dict[str, Any],
    *,
    engine=None,
    foreground_package: str = "",
) -> Dict[str, Any]:
    out = dict(debug or {})
    pkg = (foreground_package or get_foreground_package(engine) or "").strip()
    if not pkg:
        return out
    known = resolve_known_app_by_package(pkg)
    out["foreground_package"] = pkg
    if known:
        out["foreground_app"] = known.key
        out["foreground_app_name"] = known.name
        out["foreground_app_category"] = known.category
    return out
