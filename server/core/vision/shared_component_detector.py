# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
跨页面共有组件检测：基于归一化区域视觉相似度，识别底部 Tab、顶栏等重复结构。
"""
from __future__ import annotations

import uuid
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

from script.log import SLog
from server.core.vision.skeleton_algo import SkeletonAlgo

TAG = "SharedComponentDetector"

REGION_PRESETS = {
    "bottom_tab": {
        "label": "底部导航栏",
        "category": "navigation",
        "y_start_ratio": 0.86,
        "y_end_ratio": 1.0,
        "use_tab_heuristic": True,
    },
    "top_header": {
        "label": "顶部导航栏",
        "category": "navigation",
        "y_start_ratio": 0.045,
        "y_end_ratio": 0.14,
        "use_tab_heuristic": False,
    },
}


def _load_gray(screenshot: str) -> Optional[np.ndarray]:
    if not screenshot:
        return None
    return SkeletonAlgo._fetch_remote_image(screenshot)


def _crop_normalized(gray: np.ndarray, y0_ratio: float, y1_ratio: float) -> np.ndarray:
    h, _ = gray.shape[:2]
    y0 = max(0, int(h * y0_ratio))
    y1 = min(h, int(h * y1_ratio))
    if y1 <= y0:
        y1 = min(h, y0 + max(24, int(h * 0.08)))
    return gray[y0:y1, :]


def _region_signature(crop: np.ndarray, width: int = 320) -> Optional[np.ndarray]:
    if crop is None or crop.size == 0:
        return None
    h, w = crop.shape[:2]
    if h < 4 or w < 4:
        return None
    scale = width / max(w, 1)
    resized = cv2.resize(crop, (width, max(4, int(h * scale))))
    edges = cv2.Canny(resized, 40, 120)
    return edges.astype(np.float32) / 255.0


def _signature_similarity(sig_a: np.ndarray, sig_b: np.ndarray) -> float:
    if sig_a is None or sig_b is None:
        return 0.0
    if sig_a.shape != sig_b.shape:
        sig_b = cv2.resize(sig_b, (sig_a.shape[1], sig_a.shape[0]))
    diff = cv2.absdiff(sig_a, sig_b)
    return float(max(0.0, 1.0 - np.mean(diff)))


def _detect_tab_slots(crop: np.ndarray, labels: Optional[List[str]] = None) -> List[Dict]:
    """在底栏区域内检测 Tab 槽位（横向峰）。"""
    if crop is None or crop.size == 0:
        return []
    h, w = crop.shape[:2]
    edges = cv2.Canny(crop, 40, 120)
    col_energy = np.sum(edges, axis=0).astype(np.float32)
    kernel = max(7, w // 40)
    smoothed = np.convolve(col_energy, np.ones(kernel) / kernel, mode="same")
    threshold = float(np.max(smoothed)) * 0.25
    peaks = []
    min_dist = w * 0.1
    for i in range(1, len(smoothed) - 1):
        if smoothed[i] >= threshold and smoothed[i] >= smoothed[i - 1] and smoothed[i] >= smoothed[i + 1]:
            if not peaks or i - peaks[-1] >= min_dist:
                peaks.append(i)
    if len(peaks) < 2:
        return []

    default_labels = ["首页", "Tab 2", "Tab 3", "Tab 4", "Tab 5"]
    slots = []
    for idx, px in enumerate(peaks[:5]):
        nx = px / max(w, 1)
        slots.append({
            "index": idx,
            "label": (labels[idx] if labels and idx < len(labels) else default_labels[idx]),
            "normalized_x": round(nx, 4),
            "normalized_w": round(1.0 / max(len(peaks), 1), 4),
        })
    return slots


def _find_matching_interaction(
    interactions: List[Dict],
    natural_h: float,
    y0_ratio: float,
    y1_ratio: float,
) -> Optional[Dict]:
    if not interactions or not natural_h:
        return None
    y0 = natural_h * y0_ratio
    y1 = natural_h * y1_ratio
    best = None
    best_overlap = 0.0
    for comp in interactions:
        cy = float(comp.get("y") or 0) + float(comp.get("h") or comp.get("height") or 0) / 2
        if y0 <= cy <= y1:
            overlap = float(comp.get("h") or comp.get("height") or 0)
            if overlap > best_overlap:
                best_overlap = overlap
                best = comp
    return best


class SharedComponentDetector:
    @staticmethod
    def detect(
        nodes: List[Dict],
        region_hints: Optional[List[str]] = None,
        min_similarity: float = 0.72,
        min_cluster_size: int = 2,
    ) -> Dict:
        region_hints = region_hints or ["bottom_tab"]
        page_nodes = [
            n for n in nodes
            if n.get("type") == "page" and (n.get("data") or {}).get("screenshot")
        ]

        clusters: List[Dict] = []
        for hint in region_hints:
            preset = REGION_PRESETS.get(hint)
            if not preset:
                continue
            clusters.extend(
                SharedComponentDetector._cluster_region(
                    page_nodes, hint, preset, min_similarity, min_cluster_size
                )
            )

        saved = SharedComponentDetector._merge_with_existing(clusters, [])
        return {
            "clusters": clusters,
            "shared_components": saved,
            "page_count": len(page_nodes),
            "region_hints": region_hints,
        }

    @staticmethod
    def _cluster_region(
        page_nodes: List[Dict],
        region_key: str,
        preset: Dict,
        min_similarity: float,
        min_cluster_size: int,
    ) -> List[Dict]:
        entries = []
        y0 = preset["y_start_ratio"]
        y1 = preset["y_end_ratio"]

        for node in page_nodes:
            data = node.get("data") or {}
            gray = _load_gray(data.get("screenshot"))
            if gray is None:
                continue
            h, w = gray.shape[:2]

            if preset.get("use_tab_heuristic") and not SkeletonAlgo._has_app_bottom_tab_bar(gray):
                continue

            crop = _crop_normalized(gray, y0, y1)
            sig = _region_signature(crop)
            if sig is None:
                continue

            natural = data.get("naturalSize") or {}
            natural_h = float(natural.get("h") or h)
            natural_w = float(natural.get("w") or w)
            matched = _find_matching_interaction(data.get("interactions") or [], natural_h, y0, y1)

            entries.append({
                "node_id": node.get("id"),
                "node_label": data.get("label") or node.get("id"),
                "signature": sig,
                "crop_h": crop.shape[0],
                "natural_w": natural_w,
                "natural_h": natural_h,
                "component_uid": matched.get("id") if matched else None,
                "component_label": matched.get("label") if matched else None,
            })

        if len(entries) < min_cluster_size:
            return []

        used = set()
        result_clusters = []

        for i, base in enumerate(entries):
            if i in used:
                continue
            group = [base]
            used.add(i)
            for j, other in enumerate(entries):
                if j in used or j == i:
                    continue
                score = _signature_similarity(base["signature"], other["signature"])
                if score >= min_similarity:
                    group.append({**other, "similarity": round(score, 3)})
                    used.add(j)

            if len(group) < min_cluster_size:
                continue

            scores = [m.get("similarity", 1.0) for m in group[1:]]
            avg_score = float(np.mean(scores)) if scores else 1.0

            sample_gray = _load_gray(
                next(
                    (n.get("data", {}).get("screenshot") for n in page_nodes if n.get("id") == group[0]["node_id"]),
                    None,
                )
            )
            tab_slots = []
            if sample_gray is not None and preset.get("use_tab_heuristic"):
                crop = _crop_normalized(sample_gray, y0, y1)
                tab_slots = _detect_tab_slots(crop)

            members = []
            for m in group:
                members.append({
                    "node_id": m["node_id"],
                    "node_label": m["node_label"],
                    "similarity": round(m.get("similarity", 1.0), 3),
                    "component_uid": m.get("component_uid"),
                    "component_label": m.get("component_label"),
                })

            result_clusters.append({
                "uid": f"shared-{region_key}-{uuid.uuid4().hex[:8]}",
                "name": preset["label"],
                "region": region_key,
                "category": preset["category"],
                "scope": "graph",
                "normalized_rect": [0.0, y0, 1.0, y1 - y0],
                "avg_similarity": round(avg_score, 3),
                "member_count": len(members),
                "members": members,
                "tabs": tab_slots,
                "status": "detected",
            })

        SLog.i(TAG, f"Region {region_key}: found {len(result_clusters)} shared cluster(s)")
        return result_clusters

    @staticmethod
    def _merge_with_existing(detected: List[Dict], existing: List[Dict]) -> List[Dict]:
        if not existing:
            return detected
        by_region = {c.get("region"): c for c in existing}
        merged = []
        for cluster in detected:
            old = by_region.get(cluster.get("region"))
            if old:
                cluster["uid"] = old.get("uid") or cluster["uid"]
                cluster["status"] = "confirmed" if old.get("status") == "confirmed" else cluster.get("status")
            merged.append(cluster)
        for old in existing:
            if old.get("region") not in {c.get("region") for c in detected}:
                merged.append(old)
        return merged
