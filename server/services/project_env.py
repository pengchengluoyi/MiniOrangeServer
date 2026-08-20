# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""项目级运行环境：可增删环境 / 渠道，上线路径可配。"""
from __future__ import annotations

import copy
import re
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
    }


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
    if isinstance(snap, dict):
        return copy.deepcopy(snap)
    channels = env_doc.get("channels") if isinstance(env_doc.get("channels"), list) else DEFAULT_CHANNELS
    return empty_profile(channels)


def target_id_from_snapshot(snap: dict, platform: str = "android") -> str:
    """从某一环境 profile 快照取启动标识：Android 包名 / iOS Bundle。"""
    data = snap if isinstance(snap, dict) else {}
    plat = str(platform or "android").lower()
    if plat in ("ios", "iphone", "ipad"):
        ios = data.get("ios") if isinstance(data.get("ios"), dict) else {}
        return str(ios.get("bundle") or ios.get("bundle_id") or "").strip()
    android = data.get("android") if isinstance(data.get("android"), dict) else {}
    return str(android.get("package") or "").strip()


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
