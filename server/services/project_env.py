# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""项目级运行环境配置（多环境 profile）。"""
from __future__ import annotations

import copy
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from server.models.project import Project, App
from server.models.workflow import Workflow
from server.models.AppGraph.app_structure import AppGraph, AppSOP

ENV_PROFILE_KEYS = ("dev", "test", "pre", "prod")

ENV_PROFILE_LABELS = {
    "dev": "开发环境",
    "test": "测试环境",
    "pre": "预发环境",
    "prod": "正式环境",
}

EMPTY_PLATFORM = {
    "android": {"package": ""},
    "ios": {"bundle": ""},
    "web": {"base_url": ""},
}


def default_project_env() -> dict:
    profiles = {k: copy.deepcopy(EMPTY_PLATFORM) for k in ENV_PROFILE_KEYS}
    return {"default_profile": "test", "profiles": profiles}


def normalize_project_env(raw: Any) -> dict:
    """合并为规范结构。"""
    base = default_project_env()
    if not isinstance(raw, dict):
        return base

    if raw.get("default_profile") in ENV_PROFILE_KEYS:
        base["default_profile"] = raw["default_profile"]

    profiles_in = raw.get("profiles")
    if isinstance(profiles_in, dict):
        for key in ENV_PROFILE_KEYS:
            p = profiles_in.get(key)
            if isinstance(p, dict):
                for plat in ("android", "ios", "web"):
                    if isinstance(p.get(plat), dict):
                        base["profiles"][key][plat].update(p[plat])
    else:
        # 兼容旧版 apps.env 扁平结构 → 写入 test
        legacy = {}
        if isinstance(raw.get("android"), dict):
            legacy["android"] = raw["android"]
        if isinstance(raw.get("ios"), dict):
            legacy["ios"] = raw["ios"]
        if isinstance(raw.get("web"), dict):
            legacy["web"] = raw["web"]
        if legacy:
            base["profiles"]["test"].update(legacy)

    return base


def resolve_profile_name(env_doc: dict, env_profile: Optional[str] = None) -> str:
    if env_profile and env_profile in ENV_PROFILE_KEYS:
        return env_profile
    default = env_doc.get("default_profile")
    if default in ENV_PROFILE_KEYS:
        return default
    return "test"


def profile_snapshot(env_doc: dict, env_profile: Optional[str] = None) -> dict:
    """当前 Run 使用的 app.* 数据源（某一 profile 下的平台配置）。"""
    name = resolve_profile_name(env_doc, env_profile)
    profiles = env_doc.get("profiles") or {}
    snap = profiles.get(name)
    if isinstance(snap, dict):
        return copy.deepcopy(snap)
    return copy.deepcopy(EMPTY_PLATFORM)


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
