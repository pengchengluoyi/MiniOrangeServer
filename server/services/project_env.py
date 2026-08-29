# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""项目级运行环境：可增删环境 / 渠道，上线路径可配。"""
from __future__ import annotations

import copy
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from server.models.project import Project, App
from server.models.workflow import Workflow
from server.models.AppGraph.app_structure import AppGraph, AppSOP

ENV_PROFILE_KEYS = ("dev", "test", "pre", "prod")

ENV_PROFILE_LABELS = {
    "dev": "开发",
    "test": "测试",
    "pre": "预发",
    "prod": "正式",
}

DEFAULT_CHANNELS = [
    {"id": "android", "label": "安卓", "field": "package", "placeholder": "com.example.app"},
    {"id": "ios", "label": "iOS", "field": "bundle", "placeholder": "com.example.app"},
    {"id": "web", "label": "Web", "field": "base_url", "placeholder": "https://test.example.com"},
    {"id": "pc", "label": "PC", "field": "path", "placeholder": "安装路径或启动命令"},
    {"id": "mac", "label": "Mac", "field": "bundle", "placeholder": "com.example.desktop"},
    {"id": "server", "label": "Server", "field": "base_url", "placeholder": "https://api.example.com"},
]

DEFAULT_ENVIRONMENTS = [
    {"key": "test", "label": "测试"},
    {"key": "pre", "label": "预发"},
    {"key": "prod", "label": "正式"},
]

EMPTY_PLATFORM = {
    "android": {"package": ""},
    "ios": {"bundle": ""},
    "web": {"base_url": ""},
}

_KEY_RE = re.compile(r"[^a-z0-9_-]+")


def _slug(text: str, fallback: str = "env") -> str:
    s = _KEY_RE.sub("", str(text or "").strip().lower())[:24]
    return s or fallback


def empty_profile(channels: List[dict]) -> dict:
    out: Dict[str, Any] = {}
    for ch in channels:
        cid = ch.get("id")
        field = ch.get("field") or "value"
        if cid:
            out[cid] = {field: ""}
    return out


def default_project_env() -> dict:
    channels = [copy.deepcopy(c) for c in DEFAULT_CHANNELS]
    environments = [copy.deepcopy(e) for e in DEFAULT_ENVIRONMENTS]
    profiles = {e["key"]: empty_profile(channels) for e in environments}
    return {
        "default_profile": "test",
        "environments": environments,
        "channels": channels,
        "pipeline": [e["key"] for e in environments],
        "profiles": profiles,
    }


def _norm_channel(raw: Any, seen: set) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    cid = _slug(raw.get("id") or raw.get("key") or "", "")
    label_slug = _slug(raw.get("label") or "", "")
    preset = next(
        (
            c
            for c in DEFAULT_CHANNELS
            if c["id"] == cid
            or c["id"] == label_slug
            or c["label"] == str(raw.get("label") or "").strip()
        ),
        None,
    )
    if preset:
        cid = preset["id"]
    elif not cid:
        cid = label_slug
    if not cid or cid in seen:
        return None
    field = _slug(raw.get("field") or (preset or {}).get("field") or "value", "value")
    if field[0].isdigit() if field else True:
        field = (preset or {}).get("field") or "value"
    label = str(raw.get("label") or (preset or {}).get("label") or cid).strip()[:20] or cid
    if preset and label in {cid, preset["id"]}:
        label = preset["label"]
    placeholder = str(raw.get("placeholder") or (preset or {}).get("placeholder") or "").strip()[:80]
    seen.add(cid)
    return {"id": cid, "label": label, "field": field, "placeholder": placeholder}


def _norm_environment(raw: Any, seen: set) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None
    key = _slug(raw.get("key") or raw.get("id") or raw.get("label"), "")
    if not key or key in seen:
        return None
    label = str(raw.get("label") or ENV_PROFILE_LABELS.get(key) or key).strip()[:20] or key
    seen.add(key)
    return {"key": key, "label": label}


def _profile_value(block: Any, field: str) -> str:
    if not isinstance(block, dict):
        return str(block or "").strip() if block not in (None, "") else ""
    for k in (field, "value", "package", "bundle", "base_url", "path", "url", "host"):
        v = str(block.get(k) or "").strip()
        if v:
            return v
    return ""


def _norm_profile(raw: Any, channels: List[dict]) -> dict:
    src = raw if isinstance(raw, dict) else {}
    out = empty_profile(channels)
    for ch in channels:
        cid = ch["id"]
        field = ch["field"]
        out[cid][field] = _profile_value(src.get(cid), field)
    return out


def normalize_project_env(raw: Any) -> dict:
    """规范结构：environments + channels + pipeline + profiles。兼容旧四套 android/ios/web。"""
    if not isinstance(raw, dict):
        return default_project_env()

    profiles_in = raw.get("profiles") if isinstance(raw.get("profiles"), dict) else None
    if profiles_in is None and any(k in raw for k in ("android", "ios", "web")):
        profiles_in = {"test": {k: raw[k] for k in ("android", "ios", "web") if isinstance(raw.get(k), dict)}}

    channels: List[dict] = []
    seen_ch: set = set()
    for row in raw.get("channels") or []:
        ch = _norm_channel(row, seen_ch)
        if ch:
            channels.append(ch)
    if not channels:
        inferred = set()
        if isinstance(profiles_in, dict):
            for snap in profiles_in.values():
                if isinstance(snap, dict):
                    inferred.update(k for k, v in snap.items() if isinstance(v, dict))
        if not inferred:
            inferred = {"android", "ios", "web"}
        for preset in DEFAULT_CHANNELS:
            if preset["id"] in inferred:
                channels.append(copy.deepcopy(preset))
        for cid in sorted(inferred):
            if cid not in seen_ch:
                ch = _norm_channel({"id": cid, "label": cid, "field": "value"}, seen_ch)
                if ch:
                    channels.append(ch)

    environments: List[dict] = []
    seen_env: set = set()
    for row in raw.get("environments") or []:
        env = _norm_environment(row, seen_env)
        if env:
            environments.append(env)
    if not environments:
        keys = list(profiles_in.keys()) if isinstance(profiles_in, dict) and profiles_in else list(ENV_PROFILE_KEYS)
        for key in keys:
            env = _norm_environment({"key": key, "label": ENV_PROFILE_LABELS.get(key, key)}, seen_env)
            if env:
                environments.append(env)
    if not environments:
        environments = [copy.deepcopy(e) for e in DEFAULT_ENVIRONMENTS]
        seen_env = {e["key"] for e in environments}

    pipeline: List[str] = []
    for item in raw.get("pipeline") or []:
        key = _slug(item, "")
        if key in seen_env and key not in pipeline:
            pipeline.append(key)
    if not pipeline:
        # 旧数据默认三步走：测试 → 预发 → 正式；其余环境可单独开测但不在上线路径里
        for key in ("test", "pre", "prod"):
            if key in seen_env:
                pipeline.append(key)
        if not pipeline:
            pipeline = [e["key"] for e in environments]

    default_profile = _slug(raw.get("default_profile") or "", "")
    if default_profile not in seen_env:
        default_profile = pipeline[0] if pipeline else environments[0]["key"]

    profiles = {}
    src_profiles = profiles_in or {}
    for env in environments:
        profiles[env["key"]] = _norm_profile(src_profiles.get(env["key"]), channels)

    return {
        "default_profile": default_profile,
        "environments": environments,
        "channels": channels,
        "pipeline": pipeline,
        "profiles": profiles,
        "test_accounts": _norm_test_accounts(raw.get("test_accounts")),
    }


def account_ident(row: dict | None) -> str:
    """号池条目的对外标识：手机号优先，其次邮箱/用户名。不用自定义名称。"""
    row = row if isinstance(row, dict) else {}
    phone = re.sub(r"\s+", "", str(row.get("phone") or ""))
    if phone:
        return phone
    email = str(row.get("email") or "").strip()
    if email:
        return email
    return str(row.get("username") or "").strip()


def account_label(row: dict | None) -> str:
    row = row if isinstance(row, dict) else {}
    ident = account_ident(row) or "未填号码"
    env = str(row.get("env") or "").strip() or "-"
    return f"{ident} · {env}"


def _norm_test_accounts(raw: Any) -> List[dict]:
    rows = raw if isinstance(raw, list) else []
    out: List[dict] = []
    seen: set = set()
    for item in rows:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("id") or "").strip() or uuid.uuid4().hex[:12]
        if aid in seen:
            continue
        seen.add(aid)
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        clean_tags = []
        for t in tags:
            s = str(t or "").strip()[:40]
            if s and s not in clean_tags:
                clean_tags.append(s)
        phone = str(item.get("phone") or "").strip()[:32]
        email = str(item.get("email") or "").strip()[:80]
        username = str(item.get("username") or "").strip()[:80]
        ident = account_ident({"phone": phone, "email": email, "username": username})
        out.append(
            {
                "id": aid,
                "name": ident[:40],
                "env": _slug(item.get("env") or "test", "test"),
                "kind": str(item.get("kind") or "mixed").strip() or "mixed",
                "phone": phone,
                "email": email,
                "username": username,
                "password": str(item.get("password") or "").strip()[:120],
                "tags": clean_tags[:24],
                "note": str(item.get("note") or "").strip()[:200],
                "locked": bool(item.get("locked")),
            }
        )
    return out


def public_test_accounts(rows: List[dict], *, include_password: bool = False) -> List[dict]:
    """列表给前端看。筛号 / 环境接口默认不带明文；号池管理页需要带上才能展示。"""
    out = []
    for row in rows or []:
        pwd = str(row.get("password") or "")
        item = dict(row)
        item["has_password"] = bool(pwd)
        item["password_masked"] = ("••••" + pwd[-2:]) if len(pwd) >= 4 else ("••••" if pwd else "")
        if include_password:
            item["password"] = pwd
        else:
            item.pop("password", None)
        out.append(item)
    return out


def list_test_accounts(env_doc: dict) -> List[dict]:
    raw = env_doc.get("test_accounts") if isinstance(env_doc, dict) else []
    return _norm_test_accounts(raw)


def save_test_accounts(env_doc: dict, rows: List[dict]) -> dict:
    doc = dict(env_doc or {})
    incoming = _norm_test_accounts(rows)
    prev = {str(x.get("id")): x for x in list_test_accounts(doc)}
    merged = []
    for row in incoming:
        old = prev.get(row["id"]) or {}
        if not row.get("password"):
            row["password"] = str(old.get("password") or "")
        merged.append(row)
    doc["test_accounts"] = merged
    return doc


_ENV_HINTS = (
    ("正式", "prod"),
    ("生产", "prod"),
    ("预发", "pre"),
    ("测试", "test"),
    ("开发", "dev"),
)


def infer_env_from_prompt(prompt: str) -> str:
    q = str(prompt or "")
    for word, key in _ENV_HINTS:
        if word in q:
            return key
    return ""


def _prompt_grams(text: str) -> list[str]:
    s = str(text or "").strip().lower()
    if not s:
        return []
    parts = [t for t in re.split(r"[\s,，、/|；;]+", s) if t]
    out: list[str] = []
    seen: set[str] = set()

    def add(g: str) -> None:
        if len(g) < 2 or g in seen:
            return
        seen.add(g)
        out.append(g)

    for p in parts:
        add(p)
        if len(p) >= 4:
            for n in (2, 3, 4):
                for i in range(len(p) - n + 1):
                    add(p[i : i + n])
    return out


def _tag_fits_query(tag: str, q: str) -> bool:
    """未注册 / 已登录 这类极性标签，不能只因为「注册」「登录」两个字就命中反义号。"""
    t = str(tag or "")
    query = str(q or "")
    for pos, neg in (("已注册", "未注册"), ("已登录", "未登录"), ("已领取", "未领取")):
        if neg in t and neg not in query:
            return False
        if pos in t and neg in query and pos not in query:
            return False
    return True


def pick_test_accounts(rows: List[dict], *, prompt: str = "", env: str = "") -> List[dict]:
    raw = str(prompt or "").strip()
    q = raw.lower()
    env_key = _slug(env, "") or infer_env_from_prompt(raw)
    grams = _prompt_grams(raw)
    scored = []
    for row in rows or []:
        row_env = str(row.get("env") or "")
        if env_key and row_env and row_env != env_key:
            continue
        tags = [str(t or "").strip() for t in (row.get("tags") or []) if str(t or "").strip()]
        ident = account_ident(row)
        note = str(row.get("note") or "")
        blob = " ".join(
            [ident, note, str(row.get("email") or ""), str(row.get("username") or ""), " ".join(tags)]
        ).lower()
        score = 0
        reasons = []
        if env_key and row_env == env_key:
            score += 6
            reasons.append("环境匹配")
        if row.get("locked"):
            score -= 8
            reasons.append("占用中")
        if q and q in blob:
            score += 16
            reasons.append("整句命中")
        tag_hits = [
            t for t in tags
            if t and _tag_fits_query(t, q)
            and (t.lower() in q or any(len(g) >= 2 and g in t.lower() for g in grams))
        ]
        if tag_hits:
            score += 10 + 4 * min(3, len(tag_hits))
            reasons.append("标签「" + "、".join(tag_hits[:3]) + "」")
        ident_hits = [g for g in grams if len(g) >= 4 and g in ident.lower()]
        if ident_hits:
            score += 8
            reasons.append("号码命中")
        extra = [
            g for g in grams
            if len(g) >= 3 and g in blob
            and g not in ident.lower()
            and not any(g in t.lower() for t in tags)
        ]
        if extra:
            score += 2 * min(4, len(extra))
        scored.append({**row, "score": int(score), "reason": " · ".join(reasons) or "无明显匹配"})
    scored.sort(key=lambda x: (-int(x.get("score") or 0), account_ident(x), str(x.get("env") or "")))
    return scored[:12]


def profile_keys(env_doc: dict) -> List[str]:
    envs = env_doc.get("environments") if isinstance(env_doc, dict) else None
    if isinstance(envs, list) and envs:
        return [str(e.get("key")) for e in envs if isinstance(e, dict) and e.get("key")]
    profiles = (env_doc or {}).get("profiles") if isinstance(env_doc, dict) else {}
    if isinstance(profiles, dict) and profiles:
        return [str(k) for k in profiles.keys()]
    return list(ENV_PROFILE_KEYS)


def pipeline_keys(env_doc: dict) -> List[str]:
    keys = set(profile_keys(env_doc))
    pipe = env_doc.get("pipeline") if isinstance(env_doc, dict) else None
    if isinstance(pipe, list) and pipe:
        out = [str(k) for k in pipe if str(k) in keys]
        if out:
            return out
    return profile_keys(env_doc)


def resolve_profile_name(env_doc: dict, env_profile: Optional[str] = None) -> str:
    keys = set(profile_keys(env_doc))
    if env_profile and str(env_profile) in keys:
        return str(env_profile)
    default = str((env_doc or {}).get("default_profile") or "")
    if default in keys:
        return default
    pipe = pipeline_keys(env_doc)
    if pipe:
        return pipe[0]
    return next(iter(keys), "test")


def profile_snapshot(env_doc: dict, env_profile: Optional[str] = None) -> dict:
    """当前 Run 使用的 app.* 数据源（某一环境下的渠道配置）。"""
    name = resolve_profile_name(env_doc, env_profile)
    profiles = env_doc.get("profiles") or {}
    snap = profiles.get(name)
    channels = env_doc.get("channels") if isinstance(env_doc.get("channels"), list) else DEFAULT_CHANNELS
    if not isinstance(snap, dict):
        snap = empty_profile(channels)
    else:
        snap = copy.deepcopy(snap)
    order = pipeline_keys(env_doc)
    for ch in channels or []:
        cid = str(ch.get("id") or "")
        field = str(ch.get("field") or "value")
        if cid not in ("android", "ios"):
            continue
        if _profile_value((snap.get(cid) if isinstance(snap.get(cid), dict) else {}), field):
            continue
        filled = _inherit_mobile_value(profiles, order, name, cid, field)
        if filled:
            snap.setdefault(cid, {})
            snap[cid][field] = filled
    return snap


def _inherit_mobile_value(profiles: dict, order: List[str], env_key: str, channel_id: str, field: str) -> str:
    keys = [k for k in (order or []) if k] or list((profiles or {}).keys())
    for key in keys:
        snap = profiles.get(key) if isinstance(profiles, dict) else None
        if not isinstance(snap, dict):
            continue
        v = _profile_value(snap.get(channel_id), field)
        if v:
            return v
    other = "ios" if channel_id == "android" else "android"
    other_field = "bundle" if other == "ios" else "package"
    for key in [env_key, *keys]:
        snap = profiles.get(key) if isinstance(profiles, dict) else None
        if not isinstance(snap, dict):
            continue
        v = _profile_value(snap.get(other), other_field)
        if v:
            return v
    return ""


def target_id_from_snapshot(snap: dict, platform: str = "android") -> str:
    """从某一环境 profile 快照取启动标识：Android 包名 / iOS Bundle / Web 网址。"""
    data = snap if isinstance(snap, dict) else {}
    plat = str(platform or "android").lower()
    if plat in ("web", "browser", "playwright"):
        web = data.get("web") if isinstance(data.get("web"), dict) else {}
        return str(web.get("base_url") or web.get("url") or "").strip()
    if plat in ("ios", "iphone", "ipad"):
        ios = data.get("ios") if isinstance(data.get("ios"), dict) else {}
        return str(ios.get("bundle") or ios.get("bundle_id") or "").strip()
    android = data.get("android") if isinstance(data.get("android"), dict) else {}
    pkg = str(android.get("package") or "").strip()
    if pkg:
        return pkg
    ios = data.get("ios") if isinstance(data.get("ios"), dict) else {}
    return str(ios.get("bundle") or ios.get("bundle_id") or "").strip()


def resolve_project_id_for_flow(session: Session, flow_id: int) -> Optional[str]:
    wf = session.query(Workflow).filter(Workflow.id == int(flow_id)).first()
    if not wf:
        return None
    graph = None
    if wf.sop_id:
        sop = session.query(AppSOP).filter(AppSOP.id == wf.sop_id).first()
        if sop:
            graph = session.query(AppGraph).filter(AppGraph.id == sop.graph_id).first()
    if not graph:
        graph = (
            session.query(AppGraph)
            .join(AppSOP)
            .join(Workflow)
            .filter(Workflow.id == int(flow_id))
            .first()
        )
    if not graph or not graph.app_id:
        return None
    app = session.query(App).filter(App.id == graph.app_id).first()
    return app.project_id if app else None


def load_project_env(session: Session, project_id: str) -> dict:
    project = session.query(Project).filter(Project.id == project_id).first()
    if not project:
        return default_project_env()
    doc = normalize_project_env(project.env)
    # 若项目无配置，尝试从首个 app.env 迁移到 test
    if not project.env and project.apps:
        for app in project.apps:
            if isinstance(app.env, dict) and app.env:
                doc["profiles"]["test"] = normalize_project_env(app.env)["profiles"]["test"]
                break
    return doc


def load_project_env_for_flow(
    session: Session, flow_id: int, env_profile: Optional[str] = None
) -> Tuple[dict, dict, Optional[str]]:
    """
    返回 (env_doc, active_app_snapshot, project_id)
    """
    project_id = resolve_project_id_for_flow(session, flow_id)
    if not project_id:
        return default_project_env(), copy.deepcopy(EMPTY_PLATFORM), None
    env_doc = load_project_env(session, project_id)
    snap = profile_snapshot(env_doc, env_profile)
    return env_doc, snap, project_id
