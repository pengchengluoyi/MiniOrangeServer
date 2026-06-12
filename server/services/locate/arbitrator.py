# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""多通道打分：各通道独立算分，仲裁只取得分最高者（无准入门槛、无 kind 互斥）。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from script.log import SLog

from server.services.locate.channels import LocateCandidate
from server.services.locate.page_profiles import PageProfile

TAG = "LocateArbitrator"


class TargetKind(str, Enum):
    TEXT = "text"
    ICON = "icon"
    BUTTON = "button"
    CHECKBOX = "checkbox"
    UNKNOWN = "unknown"


def _boosted_raw(c: LocateCandidate) -> float:
    raw = float(c.score)
    if c.channel in ("clip", "icon_row", "gallery") and raw < 0.5:
        raw = min(1.0, raw * 1.35)
    return raw


def classify_target_kind(label: str, core_text: str) -> TargetKind:
    """仅用于回放诊断展示，不参与打分与是否可点击。"""
    raw = (label or "").strip()
    if re.search(r"勾选|勾上|checkbox|复选|协议.*框|单选|radio", raw, re.I):
        return TargetKind.CHECKBOX
    if re.search(r"图标|\bicon\b", raw, re.I):
        return TargetKind.ICON
    if re.search(r"按钮|一键|提交|确定|继续|下一步", raw):
        return TargetKind.BUTTON
    core = core_text or raw
    if core and re.search(r"[\u4e00-\u9fff]", core) and len(core) <= 24:
        return TargetKind.TEXT
    return TargetKind.UNKNOWN


@dataclass
class ArbitrationResult:
    winner: Optional[LocateCandidate]
    ranked: List[Tuple[LocateCandidate, float]]
    target_kind: TargetKind
    page_profile_key: str


def score_candidate(
    c: LocateCandidate,
    *,
    profile: PageProfile,
    target_kind: TargetKind,
) -> float:
    _ = target_kind
    weights = profile.weights.as_dict()
    w = weights.get(c.channel, 0.25)
    return _boosted_raw(c) * w


def arbitrate(
    candidates: List[LocateCandidate],
    *,
    profile: PageProfile,
    target_kind: TargetKind,
) -> ArbitrationResult:
    ranked: List[Tuple[LocateCandidate, float]] = [
        (c, score_candidate(c, profile=profile, target_kind=target_kind)) for c in candidates
    ]
    ranked.sort(key=lambda x: x[1], reverse=True)

    if profile.key == "consent":
        try:
            from server.services.copilot_service import _is_disagree_label

            ranked = [
                (c, s) for c, s in ranked if not _is_disagree_label(c.label or "")
            ]
        except Exception:
            pass

    winner: Optional[LocateCandidate] = ranked[0][0] if ranked else None

    if winner:
        SLog.i(
            TAG,
            f"pick channel={winner.channel} method={winner.method} "
            f"raw={_boosted_raw(winner):.3f} weighted={ranked[0][1]:.3f} "
            f"profile={profile.key}",
        )

    return ArbitrationResult(
        winner=winner,
        ranked=ranked,
        target_kind=target_kind,
        page_profile_key=profile.key,
    )


def debug_payload(
    result: ArbitrationResult,
    *,
    query: str,
    spatial_zones: Optional[set] = None,
    all_candidates: Optional[List[LocateCandidate]] = None,
    profile: Optional[PageProfile] = None,
    target_kind: Optional[TargetKind] = None,
) -> dict:
    """供回放截图叠加与右侧面板展示的多通道诊断数据。"""
    items = []
    winner = result.winner
    for c, final_s in result.ranked[:12]:
        items.append(_candidate_debug_row(c, final_s, selected=winner is c))

    overlay: List[dict] = []
    if all_candidates and profile is not None and target_kind is not None:
        by_channel: dict = {}
        for c in all_candidates:
            fs = score_candidate(c, profile=profile, target_kind=target_kind)
            by_channel.setdefault(c.channel, []).append((c, fs))
        for ch, rows in by_channel.items():
            rows.sort(key=lambda x: x[1], reverse=True)
            for c, fs in rows[:4]:
                overlay.append(_candidate_debug_row(c, fs, selected=winner is c))

    return {
        "query": query,
        "profile": result.page_profile_key,
        "target_kind": result.target_kind.value,
        "spatial_zones": sorted(spatial_zones or []),
        "candidates": items,
        "overlay": overlay,
        "winner_channel": winner.channel if winner else None,
    }


def _candidate_debug_row(c: LocateCandidate, final_s: float, *, selected: bool) -> dict:
    return {
        "channel": c.channel,
        "method": c.method,
        "label": c.label,
        "raw_score": round(float(c.score), 4),
        "final_score": round(float(final_s), 4),
        "cx": c.cx,
        "cy": c.cy,
        "w": c.w,
        "h": c.h,
        "selected": selected,
        "detail": c.detail,
    }
