# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""跑图结果通过 ServerBridge 写入服务端图谱（设备节点执行时使用）。"""
from __future__ import annotations

import base64
import uuid
from typing import Dict, List, Optional

import cv2
import numpy as np

from driver.agent.Common.bridge import ServerBridge
from script.log import SLog

TAG = "RemoteCrawlPersistence"


class RemoteCrawlPersistence:
    def __init__(self, graph_id: int):
        self.graph_id = int(graph_id)

    def save_screenshot_file(self, img, prefix: str = "crawl") -> str:
        name = f"{prefix}_{uuid.uuid4().hex[:10]}.png"
        if hasattr(img, "save"):
            import io
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            raw = buf.getvalue()
        else:
            arr = np.asarray(img)
            ok, enc = cv2.imencode(".png", arr)
            raw = enc.tobytes() if ok else b""
        b64 = base64.b64encode(raw).decode("ascii")
        resp = ServerBridge.query("upload", {"name": name, "content": b64}, timeout=90)
        if not resp:
            SLog.e(TAG, "upload failed: empty response")
            return ""
        if resp.get("code") not in (None, 200) and not resp.get("url") and not resp.get("filename"):
            SLog.e(TAG, f"upload failed: {resp}")
            return ""
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        return data.get("url") or (f"/static/{data['filename']}" if data.get("filename") else "")

    def ensure_page_node(
        self,
        graph_id: int,
        node_id: str,
        label: str,
        screenshot: Optional[str] = None,
        natural_size: Optional[Dict] = None,
        *,
        x: int = 0,
        y: int = 0,
    ) -> int:
        resp = ServerBridge.query(
            "app_graph/crawl_save_page",
            {
                "graph_id": graph_id,
                "node_id": node_id,
                "label": label,
                "screenshot": screenshot,
                "natural_size": natural_size,
                "x": x,
                "y": y,
            },
            timeout=30,
        )
        return (resp or {}).get("data", {}).get("id", 0)

    def ensure_edge(
        self,
        graph_id: int,
        source_id: str,
        target_id: str,
        trigger: Dict,
        *,
        source_handle: Optional[str] = None,
        label: str = "",
    ) -> None:
        ServerBridge.query(
            "app_graph/crawl_save_edge",
            {
                "graph_id": graph_id,
                "source_id": source_id,
                "target_id": target_id,
                "trigger": trigger,
                "source_handle": source_handle,
                "label": label,
            },
            timeout=30,
        )

    def train_skeleton_for_node(
        self,
        graph_id: int,
        node_id: str,
        image_static_paths: List[str],
        threshold: int = 10,
    ) -> Optional[Dict]:
        resp = ServerBridge.query(
            "app_graph/crawl_train_skeleton",
            {
                "graph_id": graph_id,
                "node_id": node_id,
                "images": image_static_paths,
                "threshold": threshold,
            },
            timeout=120,
        )
        if resp and resp.get("code") == 200:
            return resp.get("data")
        return None
