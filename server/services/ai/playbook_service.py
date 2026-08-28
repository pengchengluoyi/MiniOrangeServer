# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""应用基础逻辑（playbook）：按应用存在库里的说明书。

代码侧（已登录 tab 判定等）仍可读结构化字段。
Agent / 分析模型按知识库检索「应用基础逻辑」分类；prompt_block 只作没有知识条目时的兜底。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm.attributes import flag_modified

from script.log import SLog

TAG = "AppPlaybook"

_LIST_KEYS = (
    "login_triggers",
    "bottom_tabs",
    "segment_tabs",
    "segment_tab_aliases",
    "logged_in_tabs",
    "logged_in_pages",
    "login_page_markers",
    "guest_markers",
    "home_markers",
    "legal_page_markers",
    "foreground_markers",
    "packages",
    "surfaces",
)
_TEXT_KEYS = (
    "version",
    "label",
    "profile_key",
    "login_how",
    "logout_how",
    "env_switch_how",
    "session_how",
    "guest_how",
    "identity_page",
    "new_user_how",
)
_CONTENT_KEYS = _TEXT_KEYS + _LIST_KEYS + ("lexicon", "guest_exists")


def empty_playbook() -> Dict[str, Any]:
    return {
        "enabled": True,
        "version": "",
        "label": "",
        "profile_key": "",
        "packages": [],
        "login_how": "",
        "login_triggers": [],
        "logout_how": "",
        "env_switch_how": "",
        "session_how": "",
        "guest_exists": False,
        "guest_how": "",
        "identity_page": "",
        "bottom_tabs": [],
        "segment_tabs": [],
        "segment_tab_aliases": [],
        "logged_in_tabs": [],
        "logged_in_tab_hits": 3,
        "logged_in_pages": [],
        "login_page_markers": [],
        "guest_markers": [],
        "new_user_how": "",
        "home_markers": [],
        "legal_page_markers": [],
        "foreground_markers": [],
        "lexicon": [],
        "surfaces": ["app", "web"],
        "updated_at": "",
    }


def _as_str_list(raw: Any) -> List[str]:
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.replace("，", ",").replace("、", ",").split(",")]
        return [p for p in parts if p]
    rows = raw if isinstance(raw, list) else []
    out: List[str] = []
    seen: set[str] = set()
    for item in rows:
        s = str(item or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _as_lexicon(raw: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(raw, dict):
        items = [{"term": k, **(v if isinstance(v, dict) else {"means": v})} for k, v in raw.items()]
    elif isinstance(raw, list):
        items = [x for x in raw if isinstance(x, dict)]
    else:
        items = []
    for item in items:
        term = str(item.get("term") or "").strip()
        if not term or term in seen:
            continue
        seen.add(term)
        out.append(
            {
                "term": term,
                "means": str(item.get("means") or "").strip(),
                "archetype": str(item.get("archetype") or "").strip(),
            }
        )
    return out


def normalize_playbook(raw: Any) -> Dict[str, Any]:
    base = empty_playbook()
    src = raw if isinstance(raw, dict) else {}
    base["enabled"] = src.get("enabled", True) is not False
    try:
        hits = int(src.get("logged_in_tab_hits") or 3)
    except (TypeError, ValueError):
        hits = 3
    base["logged_in_tab_hits"] = max(1, min(8, hits))
    base["guest_exists"] = bool(src.get("guest_exists"))
    for key in _TEXT_KEYS:
        base[key] = str(src.get(key) or "").strip()
    for key in _LIST_KEYS:
        base[key] = _as_str_list(src.get(key))
    if not base["surfaces"]:
        base["surfaces"] = ["app", "web"]
    base["lexicon"] = _as_lexicon(src.get("lexicon"))
    base["updated_at"] = str(src.get("updated_at") or "").strip()
    return base


def has_content(playbook: Optional[dict]) -> bool:
    pb = playbook if isinstance(playbook, dict) else {}
    if pb.get("login_triggers") or pb.get("bottom_tabs") or pb.get("segment_tabs"):
        return True
    if pb.get("logged_in_tabs") or pb.get("logged_in_pages") or pb.get("lexicon"):
        return True
    for key in _TEXT_KEYS:
        if key in ("version", "label", "profile_key"):
            continue
        if str(pb.get(key) or "").strip():
            return True
    for key in ("login_page_markers", "guest_markers", "home_markers", "legal_page_markers", "foreground_markers"):
        if pb.get(key):
            return True
    return bool(pb.get("guest_exists"))


def prompt_block(playbook: Optional[dict]) -> str:
    """整份说明书。enabled 且有内容才返回；执行侧直接注入，不走检索。"""
    pb = normalize_playbook(playbook)
    if not pb.get("enabled") or not has_content(pb):
        return ""
    lines = ["==== 应用基础逻辑（本应用说明书，默认生效；与当前屏幕冲突时以屏幕为准）===="]
    if pb.get("label") or pb.get("version"):
        bits = [x for x in (pb.get("label"), pb.get("version") and f"版本 {pb['version']}") if x]
        if bits:
            lines.append("应用：" + " · ".join(bits))
    if pb.get("login_how"):
        lines.append(f"如何登录：{pb['login_how']}")
    if pb.get("login_triggers"):
        lines.append("会弹出登录的页面：" + "、".join(pb["login_triggers"]))
    if pb.get("logout_how"):
        lines.append(f"如何退出登录：{pb['logout_how']}")
    if pb.get("env_switch_how"):
        lines.append(f"如何在应用内切换环境：{pb['env_switch_how']}")
    if pb.get("session_how"):
        lines.append(f"如何判断登录态：{pb['session_how']}")
    guest = "有" if pb.get("guest_exists") else "未声明有"
    guest_line = f"访客浏览：{guest}"
    if pb.get("guest_how"):
        guest_line += f"；{pb['guest_how']}"
    if pb.get("guest_markers"):
        guest_line += "；游客特征文案：" + "、".join(pb["guest_markers"])
    lines.append(guest_line)
    if pb.get("identity_page"):
        lines.append(f"身份页（看当前是谁）：{pb['identity_page']}")
    nav = _as_str_list(list(pb.get("bottom_tabs") or []) + list(pb.get("segment_tabs") or []))
    if nav:
        lines.append("主导航：" + "、".join(nav))
    if pb.get("logged_in_tabs"):
        lines.append(
            f"已登录底栏信号：命中「{'、'.join(pb['logged_in_tabs'])}」至少 "
            f"{pb.get('logged_in_tab_hits') or 3} 个"
        )
    if pb.get("logged_in_pages"):
        lines.append("视为已登录的页面：" + "、".join(pb["logged_in_pages"]))
    if pb.get("login_page_markers"):
        lines.append("登录页特征：" + "、".join(pb["login_page_markers"]))
    if pb.get("new_user_how"):
        lines.append(f"新用户如何注册：{pb['new_user_how']}")
    if pb.get("lexicon"):
        rows = []
        for item in pb["lexicon"]:
            term = item.get("term") or ""
            means = item.get("means") or ""
            rows.append(f"- {term}：{means}" if means else f"- {term}")
        if rows:
            lines.append("业务术语：\n" + "\n".join(rows))
    return "\n".join(lines)


def to_ui_override(playbook: Optional[dict]) -> Dict[str, Any]:
    """转成 app_profile.merge_override 能吃的字段。空说明书返回 {}，不冒充某个应用。"""
    pb = normalize_playbook(playbook)
    if not has_content(pb):
        return {}
    lex: Dict[str, dict] = {}
    for item in pb.get("lexicon") or []:
        term = str(item.get("term") or "").strip()
        if term:
            lex[term] = {"means": item.get("means") or "", "archetype": item.get("archetype") or ""}
    return {
        "key": pb.get("profile_key") or "app",
        "label": pb.get("label") or "",
        "packages": list(pb.get("packages") or []),
        "bottom_tabs": list(pb.get("bottom_tabs") or []),
        "segment_tabs": list(pb.get("segment_tabs") or []),
        "segment_tab_aliases": list(pb.get("segment_tab_aliases") or []),
        "home_markers": list(pb.get("home_markers") or []),
        "logged_in_tabs": list(pb.get("logged_in_tabs") or []),
        "logged_in_tab_hits": int(pb.get("logged_in_tab_hits") or 3),
        "logged_in_pages": list(pb.get("logged_in_pages") or []),
        "login_page_markers": list(pb.get("login_page_markers") or []),
        "legal_page_markers": list(pb.get("legal_page_markers") or []),
        "foreground_markers": list(pb.get("foreground_markers") or []),
        "surfaces": list(pb.get("surfaces") or []),
        "lexicon": lex,
    }


def yaml_to_playbook(raw: dict) -> Dict[str, Any]:
    """把历史 ui_profile YAML 迁成说明书（一次性种子）。"""
    row = raw if isinstance(raw, dict) else {}
    pb = empty_playbook()
    pb["profile_key"] = str(row.get("key") or "").strip()
    pb["label"] = str(row.get("label") or "").strip()
    pb["packages"] = _as_str_list(row.get("packages"))
    pb["bottom_tabs"] = _as_str_list(row.get("bottom_tabs"))
    pb["segment_tabs"] = _as_str_list(row.get("segment_tabs"))
    pb["segment_tab_aliases"] = _as_str_list(row.get("segment_tab_aliases"))
    pb["home_markers"] = _as_str_list(row.get("home_markers"))
    pb["logged_in_tabs"] = _as_str_list(row.get("logged_in_tabs"))
    try:
        pb["logged_in_tab_hits"] = int(row.get("logged_in_tab_hits") or 3)
    except (TypeError, ValueError):
        pb["logged_in_tab_hits"] = 3
    pb["logged_in_pages"] = _as_str_list(row.get("logged_in_pages"))
    pb["login_page_markers"] = _as_str_list(row.get("login_page_markers"))
    pb["legal_page_markers"] = _as_str_list(row.get("legal_page_markers"))
    pb["foreground_markers"] = _as_str_list(row.get("foreground_markers"))
    pb["lexicon"] = _as_lexicon(row.get("lexicon"))
    surfaces = row.get("surfaces")
    if isinstance(surfaces, list) and surfaces:
        ids = []
        for item in surfaces:
            if isinstance(item, str) and item.strip():
                ids.append(item.strip())
            elif isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]).strip())
        if ids:
            pb["surfaces"] = ids
    bits = []
    if pb["logged_in_tabs"]:
        bits.append(
            f"底栏出现「{'、'.join(pb['logged_in_tabs'])}」中至少 "
            f"{pb['logged_in_tab_hits']} 个，视为已登录"
        )
    if pb["logged_in_pages"]:
        bits.append(f"处于「{'、'.join(pb['logged_in_pages'])}」视为已登录")
    if pb["login_page_markers"]:
        bits.append(f"出现「{'、'.join(pb['login_page_markers'])}」视为登录页")
    else:
        bits.append("出现一键登录、验证码登录、手机号登录、访客浏览等通用登录文案，视为未登录")
    pb["session_how"] = "；".join(bits)
    if "我的" in pb["bottom_tabs"]:
        pb["identity_page"] = "我的"
    if "guest_exists" in row:
        pb["guest_exists"] = bool(row.get("guest_exists"))
    for key in _TEXT_KEYS:
        if str(row.get(key) or "").strip():
            pb[key] = str(row.get(key)).strip()
    for key in ("login_triggers", "guest_markers"):
        if row.get(key):
            pb[key] = _as_str_list(row.get(key))
    return normalize_playbook(pb)


def _yaml_seed_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "resources" / "app_profiles"


def load_yaml_seed_for_package(package: str) -> Dict[str, Any]:
    pkg = str(package or "").strip().lower()
    if not pkg:
        return {}
    folder = _yaml_seed_dir()
    if not folder.is_dir():
        return {}
    try:
        import yaml
    except Exception:
        return {}
    for path in sorted(folder.glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        packages = [str(p).strip().lower() for p in (raw.get("packages") or []) if str(p).strip()]
        if pkg in packages:
            return yaml_to_playbook(raw)
    return {}


def get_playbook(app) -> Dict[str, Any]:
    env = dict(app.env) if app is not None and isinstance(getattr(app, "env", None), dict) else {}
    auto = env.get("automation") if isinstance(env.get("automation"), dict) else {}
    return normalize_playbook(auto.get("playbook"))


def save_playbook(app, playbook: dict) -> Dict[str, Any]:
    env = dict(app.env) if isinstance(getattr(app, "env", None), dict) else {}
    auto = dict(env.get("automation") if isinstance(env.get("automation"), dict) else {})
    prev = normalize_playbook(auto.get("playbook"))
    pb = normalize_playbook(playbook)
    if not pb.get("packages") and prev.get("packages"):
        pb["packages"] = list(prev["packages"])
    if not pb.get("profile_key") and prev.get("profile_key"):
        pb["profile_key"] = prev["profile_key"]
    if not pb.get("label") and prev.get("label"):
        pb["label"] = prev["label"]
    pb["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    auto["playbook"] = pb
    env["automation"] = auto
    app.env = env
    flag_modified(app, "env")
    return pb


def ensure_playbook(app, *, package: str = "") -> Dict[str, Any]:
    """库里为空时，按包名从遗留 YAML 种子导入一次。"""
    pb = get_playbook(app)
    if has_content(pb):
        return pb
    pkg = str(package or "").strip()
    if not pkg:
        try:
            from server.services.app_automation_service import package_for_app

            pkg = package_for_app(app) or ""
        except Exception:
            pkg = ""
    seeded = load_yaml_seed_for_package(pkg)
    if not has_content(seeded):
        return pb
    if app is not None and str(getattr(app, "name", "") or "").strip() and not seeded.get("label"):
        seeded["label"] = str(app.name).strip()
    SLog.i(TAG, f"seed playbook from yaml package={pkg} app={getattr(app, 'id', '')}")
    return save_playbook(app, seeded)


def bind_profile(app=None, *, package: str = "", playbook: Optional[dict] = None):
    """执行/分析线程绑定说明书。没有内容时落到通用默认，不借用别的应用。"""
    from server.services.ai import app_profile as ap

    pb = playbook if playbook is not None else (ensure_playbook(app, package=package) if app is not None else {})
    pkg = str(package or "").strip()
    if not pkg and app is not None:
        try:
            from server.services.app_automation_service import package_for_app

            pkg = package_for_app(app) or ""
        except Exception:
            pkg = ""
    return ap.bind(package=pkg, override=to_ui_override(pb), playbook=normalize_playbook(pb))


def current_playbook() -> Dict[str, Any]:
    from server.services.ai import app_profile as ap

    bound = (ap._CTX.get() or {}).get("playbook")
    return normalize_playbook(bound) if isinstance(bound, dict) else empty_playbook()
