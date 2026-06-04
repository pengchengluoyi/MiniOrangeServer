# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""跑图结果写入 AppGraph（节点、边、截图、骨架）。"""
from __future__ import annotations

import json
import os
import uuid
from typing import Dict, List, Optional

import cv2

from server.core.database import SessionLocal, APP_DATA_DIR
from server.models.AppGraph.app_structure import AppGraph, AppNode, AppEdge
from server.models.AppGraph.app_types import NodeType
from server.core.vision.skeleton_algo import SkeletonAlgo
from script.log import SLog

TAG = "CrawlPersistence"


def _uploads_path(filename: str) -> str:
    d = os.path.join(APP_DATA_DIR, "uploads")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename)


def save_screenshot_file(img, prefix: str = "crawl") -> str:
    """保存截图到 uploads，返回 /static/xxx 路径。"""
    name = f"{prefix}_{uuid.uuid4().hex[:10]}.png"
    path = _uploads_path(name)
    if hasattr(img, "save"):
        img.save(path)
    else:
        cv2.imwrite(path, img)
    return f"/static/{name}"


def ensure_page_node(
    graph_id: int,
    node_id: str,
    label: str,
    screenshot: Optional[str] = None,
    natural_size: Optional[Dict] = None,
    *,
    x: int = 0,
    y: int = 0,
) -> int:
    """创建或更新页面节点，返回 DB 主键 id。"""
    session = SessionLocal()
    try:
        node = session.query(AppNode).filter(
            AppNode.graph_id == graph_id,
            AppNode.node_id == node_id,
        ).first()
        if not node:
            node = AppNode(
                graph_id=graph_id,
                node_id=node_id,
                type=NodeType.PAGE,
                label=label,
                x=x,
                y=y,
            )
            session.add(node)
            session.flush()
        node.label = label
        if screenshot:
            node.screenshot = screenshot
        dom = {}
        if node.dom_tree:
            try:
                dom = json.loads(node.dom_tree)
            except Exception:
                dom = {}
        if natural_size:
            dom["naturalSize"] = natural_size
        node.dom_tree = json.dumps(dom, ensure_ascii=False)
        session.commit()
        return node.id
    except Exception as e:
        session.rollback()
        SLog.e(TAG, f"ensure_page_node: {e}")
        raise
    finally:
        session.close()


def _trigger_to_db(trigger) -> str:
    """app_edges.trigger 为 String 列，需存 JSON 字符串。"""
    if trigger is None:
        return "{}"
    if isinstance(trigger, dict):
        return json.dumps(trigger, ensure_ascii=False)
    if isinstance(trigger, str):
        return trigger
    return json.dumps(trigger, ensure_ascii=False)


def ensure_edge(
    graph_id: int,
    source_id: str,
    target_id: str,
    trigger: Dict,
    *,
    source_handle: Optional[str] = None,
    label: str = "",
) -> None:
    if source_id == target_id:
        SLog.w(TAG, f"skip self-loop edge {source_id}")
        return

    trigger_str = _trigger_to_db(trigger)
    session = SessionLocal()
    try:
        edge_id = f"e-{source_id}-{target_id}-{uuid.uuid4().hex[:6]}"
        existing = session.query(AppEdge).filter(
            AppEdge.graph_id == graph_id,
            AppEdge.source == source_id,
            AppEdge.target == target_id,
        ).first()
        if existing:
            existing.trigger = trigger_str
            if source_handle:
                existing.source_handle = source_handle
            if label:
                existing.label = label
        else:
            session.add(
                AppEdge(
                    graph_id=graph_id,
                    edge_id=edge_id,
                    source=source_id,
                    target=target_id,
                    source_handle=source_handle or "",
                    label=label or (trigger.get("label", "") if isinstance(trigger, dict) else ""),
                    trigger=trigger_str,
                )
            )
        session.commit()
    except Exception as e:
        session.rollback()
        SLog.e(TAG, f"ensure_edge: {e}")
        raise
    finally:
        session.close()


def train_skeleton_for_node(
    graph_id: int,
    node_id: str,
    image_static_paths: List[str],
    threshold: int = 10,
) -> Optional[Dict]:
    """用多张静态路径训练骨架并写入节点 skeleton_config。"""
    if len(image_static_paths) < 1:
        return None

    names = [p.split("/static/")[-1] if "/static/" in p else os.path.basename(p) for p in image_static_paths]
    mask, err, system_bars = SkeletonAlgo.train_skeleton(names, threshold)
    if err or mask is None:
        SLog.w(TAG, f"train_skeleton failed for {node_id}: {err}")
        return None

    mask_name = f"skeleton_{uuid.uuid4().hex}.png"
    mask_path = _uploads_path(mask_name)
    cv2.imwrite(mask_path, mask)
    mask_url = f"/static/{mask_name}"

    sk = {
        "mask_url": mask_url,
        "filename": mask_name,
        "images": names,
        "master_path": names[0],
        "system_bars": system_bars,
    }

    session = SessionLocal()
    try:
        node = session.query(AppNode).filter(
            AppNode.graph_id == graph_id,
            AppNode.node_id == node_id,
        ).first()
        if node:
            node.skeleton_config = sk
            session.commit()
    finally:
        session.close()
    return sk
