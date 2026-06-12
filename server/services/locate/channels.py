# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""定位通道：CLIP / OCR / Hierarchy / 图标库 / 通用图标行 — 统一产出 LocateCandidate。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from script.log import SLog

from server.services.locate.icon_row import detect_icon_rows, flatten_icon_row_candidates
from server.services.locate.page_profiles import PageProfile
from server.services.locate.spatial import SpatialConstraint, point_in_zones

TAG = "LocateChannels"


@dataclass
class LocateCandidate:
    cx: int
    cy: int
    w: int
    h: int
    score: float
    channel: str
    method: str
    label: str = ""
    detail: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def spatial_ok(self, spatial: SpatialConstraint, screen_w: int, screen_h: int) -> bool:
        return point_in_zones(self.cx, self.cy, screen_w, screen_h, spatial.zones)


def _rect_from_hit(hit: Dict[str, Any]) -> tuple[int, int, int, int]:
    center = hit.get("center") or [0, 0]
    cx, cy = int(center[0]), int(center[1])
    w = int(hit.get("w") or hit.get("width") or 48)
    h = int(hit.get("h") or hit.get("height") or 48)
    x = int(hit.get("x") or max(0, cx - w // 2))
    y = int(hit.get("y") or max(0, cy - h // 2))
    return x, y, w, h


def collect_clip_channel(
    engine,
    screen_w: int,
    screen_h: int,
    query: str,
    *,
    aliases: Optional[Sequence[str]] = None,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    region: str = "full",
) -> List[LocateCandidate]:
    out: List[LocateCandidate] = []
    try:
        from server.services.clip_locate_service import try_clip_locate

        pos, method, detail, rect = try_clip_locate(
            engine,
            screen_w,
            screen_h,
            label=query,
            query=query,
            aliases=list(aliases or []),
            icon_targets=icon_targets,
            region=region,
        )
        if pos:
            cx, cy = int(pos[0]), int(pos[1])
            w = int((rect or {}).get("width") or 48)
            h = int((rect or {}).get("height") or 48)
            score = 0.32
            if "score=" in str(detail):
                try:
                    score = float(str(detail).split("score=")[-1].split()[0])
                except ValueError:
                    pass
            out.append(
                LocateCandidate(
                    cx=cx,
                    cy=cy,
                    w=w,
                    h=h,
                    score=score,
                    channel="clip",
                    method=method or "clip",
                    label=query,
                    detail=str(detail),
                    extra={"target_rect": rect},
                )
            )
    except Exception as e:
        SLog.w(TAG, f"clip channel failed: {e}")
    return out


def collect_anchor_channel(
    query: str,
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    screen_w: int = 0,
    screen_h: int = 0,
) -> List[LocateCandidate]:
    """手动标注 / 图标库：按名称或别名精确匹配（历史记忆兜底）。"""
    out: List[LocateCandidate] = []
    try:
        from server.services.copilot_service import (
            _icon_names_match_label,
            _make_target_rect,
        )
        from server.services.figma_icon_service import scale_icon_target_rect

        for t in icon_targets or []:
            names = [(t.get("name") or "").strip()]
            for a in t.get("aliases") or []:
                if a:
                    names.append(str(a).strip())
            if not _icon_names_match_label(query, names):
                continue
            if screen_w > 0 and screen_h > 0:
                x, y, w, h = scale_icon_target_rect(t, screen_w, screen_h)
            else:
                x, y = int(t.get("x") or 0), int(t.get("y") or 0)
                w, h = int(t.get("w") or 0), int(t.get("h") or 0)
            if w <= 0 or h <= 0:
                continue
            cx, cy = x + w // 2, y + h // 2
            name = names[0]
            rect = _make_target_rect(x, y, w, h, label=name)
            note = (t.get("note") or "").strip()
            manual = "手动标注" in note or "回放定位导入" in note or "从执行回放" in note
            out.append(
                LocateCandidate(
                    cx=cx,
                    cy=cy,
                    w=w,
                    h=h,
                    score=0.98 if manual else 0.92,
                    channel="anchor",
                    method="manual_anchor" if manual else "icon_anchor",
                    label=name,
                    detail=f"图标库锚点「{name}」@({cx},{cy})",
                    extra={
                        "target_rect": rect,
                        "anchor_id": t.get("id"),
                        "manual": manual,
                    },
                )
            )
    except Exception as e:
        SLog.w(TAG, f"anchor channel failed: {e}")
    return out


def collect_gallery_channel(
    engine,
    screen_w: int,
    screen_h: int,
    query: str,
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
) -> List[LocateCandidate]:
    out: List[LocateCandidate] = []
    try:
        from server.services.clip_locate_service import try_clip_locate

        hit = try_clip_locate(
            engine,
            screen_w,
            screen_h,
            label=query,
            query=query,
            icon_targets=icon_targets or [],
            region="full",
        )
        if hit and hit[0] and (hit[1] or "").startswith("clip_gallery"):
            cx, cy = int(hit[0][0]), int(hit[0][1])
            out.append(
                LocateCandidate(
                    cx=cx,
                    cy=cy,
                    w=48,
                    h=48,
                    score=0.36,
                    channel="gallery",
                    method=hit[1],
                    label=query,
                    detail=str(hit[2]),
                )
            )
    except Exception:
        pass
    return out


def collect_text_channels(
    engine,
    screen_w: int,
    screen_h: int,
    query: str,
    spatial: SpatialConstraint,
    *,
    ocr_query: Optional[str] = None,
    instruction_label: str = "",
) -> List[LocateCandidate]:
    """OCR + Hierarchy 全屏文字匹配；与 TargetKind 无关，只比文本相似度。"""
    from server.services.copilot_service import (
        _label_variants,
        _make_target_rect,
        _pick_best_text_clickable,
    )
    from driver.agent.Crawl.ui_discovery import (
        clickables_from_ocr_items,
        discover_clickables_from_hierarchy,
        discover_clickables_ocr,
    )

    out: List[LocateCandidate] = []
    text_queries: List[str] = []
    for q in (ocr_query, query, spatial.core_text, instruction_label):
        q = (q or "").strip()
        if q and q not in text_queries:
            text_queries.append(q)
    for q in list(text_queries):
        for v in _label_variants(q):
            if v and v not in text_queries:
                text_queries.append(v)

    def _emit(pool, channel: str, method: str) -> None:
        if not pool:
            return
        best_pick = None
        best_score = 0.0
        consent_modal = any(q in ("同意", "同意并继续") for q in text_queries)
        for tq in text_queries:
            pick = _pick_best_text_clickable(
                tq,
                pool,
                screen_h=screen_h,
                consent_modal=consent_modal,
            )
            if pick and float(pick[3]) > best_score:
                best_score = float(pick[3])
                best_pick = pick
        if not best_pick:
            return
        cx, cy, txt, score, t = best_pick
        out.append(
            LocateCandidate(
                cx=cx,
                cy=cy,
                w=int(t.w),
                h=int(t.h),
                score=float(score),
                channel=channel,
                method=method,
                label=txt,
                detail=f"{method}「{txt}」@({cx},{cy}) sim={score:.2f}",
                extra={"target_rect": _make_target_rect(t.x, t.y, t.w, t.h, label=txt)},
            )
        )

    try:
        hier = list(
            discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=96)
        )
        _emit(hier, "hierarchy", "hierarchy")
    except Exception as e:
        SLog.w(TAG, f"hierarchy channel failed: {e}")

    ocr_pool: List[Any] = []
    try:
        from server.services.page_context_service import get_cached_ocr_items

        ocr_items = get_cached_ocr_items(engine)
        if ocr_items:
            ocr_pool = list(
                clickables_from_ocr_items(
                    ocr_items, screen_w, screen_h, max_items=96
                )
            )
    except Exception as e:
        SLog.w(TAG, f"ocr cache channel failed: {e}")

    if not ocr_pool:
        try:
            from server.services.screen_frame_service import get_screen_frame

            frame = get_screen_frame(engine)
            shot = frame.get("shot") if frame else None
            if shot is not None:
                ocr_pool = list(
                    discover_clickables_ocr(shot, screen_w, screen_h, max_items=96)
                )
        except Exception as e:
            SLog.w(TAG, f"ocr screenshot channel failed: {e}")

    _emit(ocr_pool, "ocr", "ocr")
    return out


def collect_icon_row_channel(
    engine,
    screen_w: int,
    screen_h: int,
    query: str,
    *,
    instruction_label: str = "",
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    profile: PageProfile,
    spatial: Optional[SpatialConstraint] = None,
) -> List[LocateCandidate]:
    """全屏无字图标行聚类 + CLIP patch 打分（与页面 profile / 登录无关）。"""
    _ = instruction_label, icon_targets, profile, spatial
    out: List[LocateCandidate] = []
    try:
        from driver.agent.Crawl.ui_discovery import discover_clickables_from_hierarchy
        from server.core.vision.clip_service import clip_enabled, get_clip_service
        from server.services.clip_locate_service import _score_patch_candidates

        clickables = list(
            discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=96)
        )
        rows = detect_icon_rows(clickables, screen_w=screen_w, screen_h=screen_h)
        candidates = flatten_icon_row_candidates(rows)
        if not candidates or not clip_enabled():
            return out

        from server.services.screen_frame_service import get_frame_bgr

        frame = get_frame_bgr(engine)
        if frame is None:
            return out
        svc = get_clip_service()
        if not svc.available():
            return out
        text_vec = svc.encode_text_mixed(query, [query])
        if text_vec is None:
            return out

        scored = _score_patch_candidates(frame, candidates, text_vec, svc, query=query)
        for score, c in scored[:5]:
            cx = int(c.get("cx") or c["x"] + c["w"] // 2)
            cy = int(c.get("cy") or c["y"] + c["h"] // 2)
            out.append(
                LocateCandidate(
                    cx=cx,
                    cy=cy,
                    w=int(c["w"]),
                    h=int(c["h"]),
                    score=float(score),
                    channel="icon_row",
                    method="clip_icon_row",
                    label=(c.get("label") or "").strip(),
                    detail=f"icon_row clip score={score:.3f}",
                )
            )
    except Exception as e:
        SLog.w(TAG, f"icon_row channel failed: {e}")
    return out


def collect_checkable_channel(
    engine,
    screen_w: int,
    screen_h: int,
    instruction_label: str,
    spatial: SpatialConstraint,
) -> List[LocateCandidate]:
    """无障碍 CheckBox / checkable 节点 → hierarchy 通道候选（与 clip/ocr 同一套仲裁）。"""
    out: List[LocateCandidate] = []
    if not instruction_label:
        return out
    try:
        from server.services.copilot_service import _make_target_rect
        from server.services.toggle_locate_service import (
            _find_toggle_anchor_bounds,
            _spatial_pick_toggle,
            _toggle_search_anchors,
            discover_toggle_candidates,
            is_toggle_intent,
            parse_toggle_intent,
        )

        if not is_toggle_intent(instruction_label):
            return out
        intent = parse_toggle_intent(instruction_label)
        if not intent:
            return out
        anchors = _toggle_search_anchors(instruction_label, intent.anchor)
        anchor_bounds = _find_toggle_anchor_bounds(
            engine, screen_w, screen_h, anchors
        )
        nodes = discover_toggle_candidates(engine, screen_w, screen_h)

        def _emit(pick: Dict[str, Any], method: str, score: float) -> LocateCandidate:
            cx = int(pick["x"] + pick["w"] // 2)
            cy = int(pick["y"] + pick["h"] // 2)
            w, h = int(pick["w"]), int(pick["h"])
            rect = _make_target_rect(pick["x"], pick["y"], w, h, label=instruction_label)
            return LocateCandidate(
                cx=cx,
                cy=cy,
                w=w,
                h=h,
                score=score,
                channel="hierarchy",
                method=method,
                label=instruction_label,
                detail=f"hierarchy checkable「{instruction_label}」@({cx},{cy}) [{method}]",
                extra={"target_rect": rect},
            )

        if intent.wants_checked:
            checked = [c for c in nodes if c.get("checked")]
            if checked:
                pick = _spatial_pick_toggle(
                    checked,
                    anchor_bounds,
                    index=intent.index,
                    wants_checked=True,
                )
                if pick:
                    out.append(_emit(pick, "already_checked", 0.95))
                    return out

        pick = _spatial_pick_toggle(
            nodes,
            anchor_bounds,
            index=intent.index,
            wants_checked=intent.wants_checked,
        )
        if pick:
            cx = int(pick["x"] + pick["w"] // 2)
            cy = int(pick["y"] + pick["h"] // 2)
            if not spatial.active or point_in_zones(
                cx, cy, screen_w, screen_h, spatial.zones
            ):
                src = str(pick.get("source") or "u2_checkable")
                out.append(_emit(pick, src, 0.42))
    except Exception as e:
        SLog.w(TAG, f"checkable channel failed: {e}")
    return out


def gather_all_candidates(
    engine,
    screen_w: int,
    screen_h: int,
    query: str,
    *,
    instruction_label: str = "",
    ocr_query: Optional[str] = None,
    aliases: Optional[Sequence[str]] = None,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    spatial: SpatialConstraint,
    profile: PageProfile,
    clip_region: str = "full",
    enable_icon_row: bool = True,
) -> List[LocateCandidate]:
    all_c: List[LocateCandidate] = []
    label_raw = instruction_label or query
    text_query = (ocr_query or "").strip() or query
    all_c.extend(
        collect_checkable_channel(
            engine, screen_w, screen_h, label_raw, spatial
        )
    )
    all_c.extend(
        collect_anchor_channel(
            query,
            icon_targets=icon_targets,
            screen_w=screen_w,
            screen_h=screen_h,
        )
    )
    all_c.extend(
        collect_clip_channel(
            engine,
            screen_w,
            screen_h,
            query,
            aliases=aliases,
            icon_targets=icon_targets,
            region="full",
        )
    )
    all_c.extend(
        collect_text_channels(
            engine,
            screen_w,
            screen_h,
            text_query,
            spatial,
            ocr_query=ocr_query,
            instruction_label=label_raw,
        )
    )
    all_c.extend(
        collect_gallery_channel(
            engine, screen_w, screen_h, query, icon_targets=icon_targets
        )
    )
    if enable_icon_row:
        all_c.extend(
            collect_icon_row_channel(
                engine,
                screen_w,
                screen_h,
                query,
                instruction_label=label_raw,
                icon_targets=icon_targets,
                profile=profile,
                spatial=spatial,
            )
        )

    # 空间硬过滤
    if spatial.active:
        all_c = [c for c in all_c if c.spatial_ok(spatial, screen_w, screen_h)]
    return all_c
