# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""统一点击定位入口：多通道收集 + 页面 profile + 空间约束 + 仲裁。"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from script.log import SLog

from server.services.locate.arbitrator import arbitrate, classify_target_kind, debug_payload
from server.services.locate.channels import gather_all_candidates
from server.services.locate.page_profiles import resolve_page_profile
from server.services.locate.spatial import clip_region_hint, parse_spatial_constraint
from server.services.locate.types import LocateResult

TAG = "LocateResolver"


def _locate_arbitrator_enabled() -> bool:
    return os.getenv("LOCATE_ARBITRATOR", "1").strip().lower() in ("1", "true", "yes", "on")


def _screen_text_snippet(engine, limit: int = 400) -> str:
    try:
        from server.services.page_navigation_service import _collect_ocr_text_only

        return (_collect_ocr_text_only(engine) or "")[:limit]
    except Exception:
        return ""


def resolve_locate_target(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    label: str = "",
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    page_context: Optional[Dict[str, Any]] = None,
    screen_text: str = "",
) -> LocateResult:
    """多通道定位；失败时仍返回 debug 供回放展示。"""
    empty = LocateResult(method="none", detail="locate arbitrator disabled")
    if not label or not _locate_arbitrator_enabled():
        empty.detail = "locate arbitrator disabled" if not _locate_arbitrator_enabled() else "empty label"
        return empty

    spatial = parse_spatial_constraint(label)
    query = spatial.core_text or label
    aliases: List[str] = []
    ocr_query: Optional[str] = None

    region_hint: Optional[str] = None
    try:
        from server.services.locate.clip_query_plan import (
            clip_params_from_plan,
            lookup_clip_query_plan,
            ocr_query_from_plan,
        )

        plan = lookup_clip_query_plan(label)
        if plan:
            clip_q, aliases, region_hint = clip_params_from_plan(plan, label)
            if clip_q:
                query = clip_q
            ocr_query = ocr_query_from_plan(plan, spatial.core_text or label)
        else:
            from server.services.copilot_service import _clip_search_params

            clip_query, aliases, region_hint = _clip_search_params(label)
            if clip_query:
                query = clip_query
    except Exception:
        pass

    blob = screen_text or _screen_text_snippet(engine)
    profile = resolve_page_profile(page_context=page_context, screen_text=blob)
    try:
        from server.services.copilot_service import (
            _classify_login_method_intent,
            _is_consent_action_label,
        )
        from server.services.locate.page_profiles import get_page_profile
        from server.services.page_navigation_service import (
            _screen_is_system_permission_dialog,
            is_blocking_consent_screen,
        )

        if _classify_login_method_intent(label):
            profile = get_page_profile("login")
        elif _screen_is_system_permission_dialog(blob, engine=engine):
            profile = get_page_profile("system_dialog")
        elif _is_consent_action_label(label) and is_blocking_consent_screen(
            screen_text=blob, engine=engine
        ):
            profile = get_page_profile("consent")
    except Exception:
        pass
    target_kind = classify_target_kind(label, spatial.core_text)
    clip_region = clip_region_hint(spatial.zones)
    if clip_region == "full" and region_hint:
        clip_region = region_hint

    candidates = gather_all_candidates(
        engine,
        screen_w,
        screen_h,
        query,
        instruction_label=label,
        ocr_query=ocr_query,
        aliases=aliases,
        icon_targets=icon_targets,
        spatial=spatial,
        profile=profile,
        clip_region=clip_region,
        enable_icon_row=True,
    )

    result = arbitrate(candidates, profile=profile, target_kind=target_kind)
    debug = debug_payload(
        result,
        query=query,
        spatial_zones=spatial.zones,
        all_candidates=candidates,
        profile=profile,
        target_kind=target_kind,
    )

    winner = result.winner
    if not winner:
        SLog.i(
            TAG,
            f"miss query={query!r} profile={profile.key} kind={target_kind.value} "
            f"candidates={len(candidates)}",
        )
        return LocateResult(
            method="none",
            detail=f"多通道未命中「{query}」(profile={profile.key}, kind={target_kind.value})",
            debug=debug,
        )

    from server.services.copilot_service import _make_target_rect

    half = max(22, min(winner.w, winner.h) // 2)
    rect_label = label or winner.label or query
    rect = winner.extra.get("target_rect") or _make_target_rect(
        winner.cx - half,
        winner.cy - half,
        half * 2,
        half * 2,
        label=rect_label,
    )
    if rect and label:
        rect = {**rect, "label": label}
    method = winner.method
    if winner.channel == "anchor":
        method = winner.method or "manual_anchor"

    detail = winner.detail or f"{winner.channel}「{winner.label or query}」@({winner.cx},{winner.cy})"
    debug["used_anchor"] = winner.channel == "anchor"
    debug["anchor_manual"] = bool(winner.extra.get("manual"))

    return LocateResult(
        position=(winner.cx, winner.cy),
        method=method,
        detail=detail,
        target_rect=rect,
        debug=debug,
    )
