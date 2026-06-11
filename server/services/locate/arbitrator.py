# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""多通道打分仲裁：按页面 profile 权重 + 目标类型加成，取最高分。"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from script.log import SLog

from server.services.locate.channels import LocateCandidate
from server.services.locate.page_profiles import ChannelWeights, PageProfile

TAG = "LocateArbitrator"


class TargetKind(str, Enum):
    TEXT = "text"
    ICON = "icon"
    BUTTON = "button"
    CHECKBOX = "checkbox"
    UNKNOWN = "unknown"


def _min_final_score(
    kind: TargetKind = TargetKind.UNKNOWN,
    *,
    profile_key: str = "generic",
) -> float:
    if profile_key == "consent":
        if kind in (TargetKind.TEXT, TargetKind.BUTTON):
            return 0.18
    if profile_key == "system_dialog":
        if kind in (TargetKind.TEXT, TargetKind.BUTTON):
            return 0.18
    if kind == TargetKind.CHECKBOX:
        try:
            return float(os.getenv("LOCATE_MIN_SCORE_CHECKBOX", "0.14"))
        except ValueError:
            return 0.14
    try:
        return float(os.getenv("LOCATE_MIN_SCORE", "0.55"))
    except ValueError:
        return 0.55


def classify_target_kind(label: str, core_text: str) -> TargetKind:
    raw = (label or "").strip()
    if re.search(r"勾选|勾上|checkbox|复选|协议.*框|单选|radio", raw, re.I):
        return TargetKind.CHECKBOX
    if re.search(r"图标|icon", raw, re.I):
        return TargetKind.ICON
    if re.search(r"方式$", raw) and re.search(r"登录|微信|手机|邮箱|apple|账号", raw, re.I):
        return TargetKind.ICON
    try:
        from server.services.copilot_service import _classify_login_method_intent

        if _classify_login_method_intent(raw):
            return TargetKind.ICON
    except Exception:
        pass
    if re.search(r"按钮|一键|提交|确定|继续|下一步", raw):
        return TargetKind.BUTTON
    core = core_text or raw
    if core and re.search(r"[\u4e00-\u9fff]", core) and len(core) <= 24:
        return TargetKind.TEXT
    return TargetKind.UNKNOWN


def _kind_channel_boost(kind: TargetKind, channel: str) -> float:
    boosts = {
        TargetKind.TEXT: {"ocr": 1.15, "hierarchy": 1.12, "clip": 0.85, "icon_row": 0.7, "gallery": 0.9},
        TargetKind.ICON: {
            "anchor": 1.35,
            "icon_row": 1.2,
            "gallery": 1.15,
            "clip": 1.1,
            "ocr": 0.75,
            "hierarchy": 0.8,
        },
        TargetKind.BUTTON: {"clip": 1.1, "ocr": 1.05, "hierarchy": 1.0, "gallery": 0.95, "icon_row": 0.85},
        TargetKind.CHECKBOX: {
            "clip": 1.45,
            "hierarchy": 1.25,
            "ocr": 0.85,
            "gallery": 1.1,
            "icon_row": 0.65,
        },
        TargetKind.UNKNOWN: {},
    }
    return boosts.get(kind, {}).get(channel, 1.0)


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
    weights = profile.weights.as_dict()
    w = weights.get(c.channel, 0.25)
    boost = _kind_channel_boost(target_kind, c.channel)
    raw = float(c.score)
    if c.channel in ("clip", "icon_row", "gallery") and raw < 0.5:
        raw = min(1.0, raw * 1.35)
    return raw * w * boost


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
    min_score = _min_final_score(target_kind, profile_key=profile.key)
    winner: Optional[LocateCandidate] = None
    margin_need = 0.008 if target_kind == TargetKind.CHECKBOX else 0.04

    if ranked:
        if profile.key == "consent":
            try:
                from server.services.copilot_service import _is_disagree_label

                ranked = [
                    (c, s)
                    for c, s in ranked
                    if not _is_disagree_label(c.label or "")
                ]
            except Exception:
                pass
        best_c, best_s = ranked[0] if ranked else (None, 0.0)
        second_s = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = best_s - second_s
        if best_c and best_s >= min_score and (
            margin >= margin_need or best_s >= min_score + 0.08 or len(ranked) == 1
        ):
            winner = best_c
        else:
            SLog.i(
                TAG,
                f"reject ambiguous arbitration top={best_s:.3f} margin={margin:.3f} "
                f"kind={target_kind.value} profile={profile.key}",
            )

    if winner:
        SLog.i(
            TAG,
            f"pick channel={winner.channel} method={winner.method} "
            f"score={ranked[0][1]:.3f} profile={profile.key} kind={target_kind.value}",
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
