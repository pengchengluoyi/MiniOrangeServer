# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Run 长期记忆上下文：从 DB 组装 app / graph / sop / workflow / device。"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session, joinedload

from server.models.workflow import Workflow
from server.models.AppGraph.app_structure import AppGraph, AppSOP, AppNode
from server.services.device_service import DeviceService
from server.services.project_env import load_project_env_for_flow, resolve_profile_name
from server.core.vision.skeleton_algo import SkeletonAlgo


def resolve_dot_path(data: Any, path: str) -> Any:
    if not path or data is None:
        return None
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        else:
            return None
    return cur


def _serialize_skeleton_config(config: Any) -> dict:
    if not config:
        return {}
    sk = dict(config)
    images = sk.get("images") or []
    if images and not sk.get("master_path"):
        sk["master_path"] = images[0]
    mask = sk.get("mask_path") or sk.get("mask_url") or sk.get("filename")
    if mask:
        sk["mask_path"] = SkeletonAlgo._normalize_filename(mask)
    if sk.get("master_path"):
        sk["master_path"] = SkeletonAlgo._normalize_filename(sk["master_path"])
    return sk


def _serialize_app_graph_node(node) -> dict:
    comps_list = []
    for comp in node.components:
        comps_list.append({
            "uid": comp.uid,
            "label": comp.label,
            "category": comp.category,
            "sub_type": comp.sub_type,
            "rules": comp.rules,
            "x": comp.x, "y": comp.y, "width": comp.width, "height": comp.height,
        })
    return {
        "id": node.node_id,
        "label": node.label,
        "type": node.type,
        "screenshot": node.screenshot,
        "skeleton_config": _serialize_skeleton_config(node.skeleton_config),
        "components": comps_list,
        "anchors": [
            {"uid": c["uid"], "type": c["sub_type"], "value": c["label"],
             "rect": [c["x"], c["y"], c["width"], c["height"]]}
            for c in comps_list if c["category"] == "anchor"
        ],
        "mask_areas": [
            {"rect": [c["x"], c["y"], c["width"], c["height"]]}
            for c in comps_list if c["category"] == "mask"
        ],
    }


def _serialize_app_graph_structure(session: Session, graph: AppGraph) -> dict:
    """与 handle_get_app_graph 一致的结构，供 Planner / Page 使用。"""
    graph = (
        session.query(AppGraph)
        .filter(AppGraph.id == graph.id)
        .options(
            joinedload(AppGraph.nodes).joinedload(AppNode.components),
            joinedload(AppGraph.edges),
        )
        .first()
    )
    if not graph:
        return {"nodes": [], "edges": []}

    nodes_data = []
    for node in graph.nodes:
        if node.type != "case":
            nodes_data.append(_serialize_app_graph_node(node))

    return {
        "nodes": nodes_data,
        "edges": [
            {"source": e.source, "target": e.target, "trigger": e.trigger}
            for e in graph.edges
        ],
    }


def build_run_context(
    session: Session,
    flow_id: int,
    sn: Optional[str] = None,
    env_profile: Optional[str] = None,
) -> dict:
    """
    返回::
        {
          "context": { app, graph, sop, workflow, device, world },
          "app_graph": { nodes, edges },
        }
    """
    empty = {
        "context": {
            "app": {},
            "graph": {"variables": {}},
            "sop": {"variables": {}},
            "workflow": {"variables": {}},
            "device": {},
            "world": _default_world_model(),
        },
        "app_graph": {"nodes": [], "edges": []},
    }

    wf = session.query(Workflow).filter(Workflow.id == int(flow_id)).first()
    if not wf:
        return empty

    ctx = empty["context"]
    ctx["workflow"]["variables"] = wf.variables or {}

    graph = None
    if wf.sop_id:
        sop = session.query(AppSOP).filter(AppSOP.id == wf.sop_id).first()
        if sop:
            ctx["sop"]["variables"] = sop.variables or {}
            graph = session.query(AppGraph).filter(AppGraph.id == sop.graph_id).first()
    else:
        graph = (
            session.query(AppGraph)
            .join(AppSOP)
            .join(Workflow)
            .filter(Workflow.id == int(flow_id))
            .first()
        )

    if graph:
        ctx["graph"]["variables"] = graph.variables or {}
        env_doc, app_snap, _pid = load_project_env_for_flow(session, int(flow_id), env_profile)
        active = resolve_profile_name(env_doc, env_profile)
        ctx["app"] = app_snap
        ctx["env_profile"] = active
        ctx["env_profiles"] = list(env_doc.get("profiles", {}).keys())
        empty["app_graph"] = _serialize_app_graph_structure(session, graph)

    if sn and DeviceService.get_by_sn(sn):
        dev = DeviceService.get_by_sn(sn)
        ctx["device"]["password"] = DeviceService.get_password(sn)
        ctx["device"]["sn"] = sn
        if dev and dev.device_type:
            ctx["device"]["device_type"] = str(dev.device_type).lower()

    return empty


def _default_world_model() -> dict:
    return {
        "system_reflexes": [
            {
                "id": "SYS_UNLOCK",
                "priority": 100,
                "trigger": {
                    "logic": "OR",
                    "conditions": [
                        {"type": "text", "value": "滑动来解锁"},
                        {"type": "visual", "value": "ICON_LOCK_CLOSED"},
                    ],
                },
                "action": {
                    "component": "public/gesture",
                    "params": {"sub_type": "drag", "start_ref": "bottom", "end_offset": [0, -0.4]},
                },
            }
        ],
        "category_knowledge": {},
    }
