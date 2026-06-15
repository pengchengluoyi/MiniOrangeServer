# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""统一点击定位入口：多通道收集 + 页面 profile + 空间约束 + 仲裁。"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from script.log import SLog

from server.services.locate.arbitrator import arbitrate, classify_target_kind, debug_payload
from server.services.locate.channels import gather_all_candidates
from server.services.locate.page_profiles import resolve_page_profile
from server.services.locate.spatial import parse_spatial_constraint
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
    plan = None
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

    # 预建屏快照，后续 profile / 文本通道 / 阻塞检测共用同一次 OCR
    if not screen_text:
        try:
            from server.services.screen_frame_service import get_screen_frame

            get_screen_frame(engine)
        except Exception:
            pass
    blob = screen_text or _screen_text_snippet(engine)
    try:
        from server.services.locate.app_packages import get_foreground_package

        fg_pkg = get_foreground_package(engine)
    except Exception:
        fg_pkg = ""
    profile = resolve_page_profile(
        page_context=page_context,
        screen_text=blob,
        foreground_package=fg_pkg,
    )
    try:
        from server.services.copilot_service import _is_consent_action_label
        from server.services.locate.page_profiles import (
            get_page_profile,
            profile_key_for_login_step,
        )
        from server.services.page_navigation_service import (
            _screen_is_system_permission_dialog,
            is_blocking_consent_screen,
        )

        login_key = profile_key_for_login_step(label)
        if login_key:
            profile = get_page_profile(login_key)
        elif re.search(r"同意并继续", label or ""):
            profile = get_page_profile("modal")
        elif _is_consent_action_label(label) and re.search(
            r"同意并继续", blob or ""
        ):
            profile = get_page_profile("modal")
        elif _screen_is_system_permission_dialog(blob, engine=engine):
            profile = get_page_profile("system_dialog")
        elif _is_consent_action_label(label) and is_blocking_consent_screen(
            screen_text=blob, engine=engine
        ):
            profile = get_page_profile("consent")
    except Exception:
        pass
    if (label or "").strip() in ("同意",) and re.search(r"同意并继续", blob or ""):
        query = "同意并继续"
        ocr_query = "同意并继续"
        try:
            profile = get_page_profile("modal")
        except Exception:
            pass
    target_kind = classify_target_kind(label, spatial.core_text)
    _ = region_hint

    enable_icon_row = bool(plan.icon_row) if plan else True

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
        clip_region="full",
        enable_icon_row=enable_icon_row,
    )

    try:
        from server.services.locate.clip_query_plan import (
            form_input_keyboard_max_cy,
            is_form_input_label,
        )

        if is_form_input_label(label):
            max_cy = form_input_keyboard_max_cy(screen_h)
            candidates = [c for c in candidates if c.cy <= max_cy]
    except Exception:
        pass

    result = arbitrate(candidates, profile=profile, target_kind=target_kind)
    debug = debug_payload(
        result,
        query=query,
        spatial_zones=spatial.zones,
        all_candidates=candidates,
        profile=profile,
        target_kind=target_kind,
    )
    try:
        from server.services.locate.app_packages import enrich_locate_debug_app

        debug = enrich_locate_debug_app(debug, engine=engine, foreground_package=fg_pkg)
    except Exception:
        pass

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
