# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""CLIP 视觉定位：中英文 query → 屏幕区域（底栏/登录图标行/全屏候选）。"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from script.log import SLog
from server.core.vision.clip_service import (
    build_mixed_text_prompts,
    clip_enabled,
    cosine_similarity,
    get_clip_service,
)

TAG = "ClipLocate"

REGION_BANDS = {
    "bottom": (0.86, 1.0),
    "segment": (0.04, 0.28),
    "login_row": (0.66, 0.88),
    "top_right": (0.0, 0.32),
    "top_left": (0.0, 0.32),
    "full": (0.0, 1.0),
}

_GENERIC_ICON_RE = re.compile(r"^icon[_\-]?\d+$", re.I)


def _score_threshold() -> float:
    try:
        return float(os.getenv("CLIP_SCORE_THRESHOLD", "0.28"))
    except ValueError:
        return 0.28


def _login_row_threshold() -> float:
    """登录图标行置信度（配合视觉 query，略低于全屏阈值）。"""
    try:
        return float(os.getenv("CLIP_LOGIN_ROW_THRESHOLD", "0.30"))
    except ValueError:
        return 0.30


def _gallery_threshold() -> float:
    try:
        return float(os.getenv("CLIP_GALLERY_THRESHOLD", "0.28"))
    except ValueError:
        return 0.28


def _is_generic_icon_name(name: str) -> bool:
    return bool(_GENERIC_ICON_RE.match((name or "").strip()))


def _parse_embedding(raw: Any) -> Optional[np.ndarray]:
    if raw is None:
        return None
    if isinstance(raw, np.ndarray):
        return raw.astype(np.float32)
    if isinstance(raw, list) and raw:
        return np.array(raw, dtype=np.float32)
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                return np.array(data, dtype=np.float32)
        except Exception:
            return None
    return None


def _load_bgr_from_static(path: str) -> Optional[np.ndarray]:
    if not path:
        return None
    try:
        import cv2
        from server.core.database import APP_DATA_DIR

        name = path.split("/static/")[-1] if "/static/" in path else os.path.basename(path)
        local = os.path.join(APP_DATA_DIR, "uploads", name)
        if not os.path.isfile(local):
            return None
        img = cv2.imread(local)
        return img
    except Exception as e:
        SLog.w(TAG, f"load image failed {path}: {e}")
        return None


def _crop_bgr(img: np.ndarray, x: int, y: int, w: int, h: int, pad: int = 4) -> Optional[np.ndarray]:
    if img is None or img.size == 0:
        return None
    ih, iw = img.shape[:2]
    x0 = max(0, int(x) - pad)
    y0 = max(0, int(y) - pad)
    x1 = min(iw, int(x) + int(w) + pad)
    y1 = min(ih, int(y) + int(h) + pad)
    if x1 <= x0 or y1 <= y0:
        return None
    return img[y0:y1, x0:x1].copy()


def _crop_or_image(img: np.ndarray, x: int, y: int, w: int, h: int) -> np.ndarray:
    """裁剪区域；失败时退回整图（禁止对 ndarray 做 `or` 布尔判断）。"""
    crop = _crop_bgr(img, x, y, w, h)
    return img if crop is None else crop


def _screenshot_to_bgr(shot) -> Optional[np.ndarray]:
    """engine.screenshot() 可能返回 PIL.Image / 路径 / ndarray，统一为 BGR。"""
    if shot is None:
        return None
    if isinstance(shot, str):
        if not os.path.isfile(shot):
            return None
        try:
            import cv2

            img = cv2.imread(shot)
            return img if img is not None and img.size else None
        except Exception:
            return None
    try:
        from PIL import Image

        if isinstance(shot, Image.Image):
            rgb = np.asarray(shot.convert("RGB"))
            if rgb.size == 0:
                return None
            return rgb[:, :, ::-1].copy()
    except Exception:
        pass
    if isinstance(shot, np.ndarray):
        arr = np.ascontiguousarray(shot)
        if arr.size == 0:
            return None
        if arr.ndim == 2:
            return np.stack([arr] * 3, axis=-1)
        if arr.ndim == 3 and arr.shape[2] >= 3:
            return arr[:, :, :3].copy()
    return None


def infer_region_hint(label: str) -> str:
    raw = (label or "").strip()
    if not raw:
        return "full"
    if re.search(r"底部|底栏|bottom\s*tab|tab\s*bar", raw, re.I):
        return "bottom"
    if re.search(r"tab", raw, re.I) and re.search(r"底部|底栏|bottom", raw, re.I):
        return "bottom"
    for tab in ("首页", "想要", "消息", "我的", "home", "want", "profile"):
        if tab.lower() in raw.lower() and len(raw) <= 12:
            return "bottom"
    for tab in ("造物秀", "AI创意", "想要成真"):
        if tab in raw:
            return "segment"
    if any(k in raw.lower() for k in ("微信", "苹果", "邮箱", "密码", "wechat", "apple")):
        return "login_row"
    if re.search(r"手机(号)?.*(方式|登录)", raw) and "一键" not in raw and "本机" not in raw:
        return "login_row"
    if re.search(r"右上角|右上", raw):
        return "top_right"
    if re.search(r"左上角|左上", raw):
        return "top_left"
    return "full"


def _y_in_band(cy: int, screen_h: int, band: Tuple[float, float]) -> bool:
    y0 = int(screen_h * band[0])
    y1 = int(screen_h * band[1])
    return y0 <= cy <= y1


_LOGIN_ROW_SKIP_FRAGMENTS = (
    "一键",
    "本机",
    "访客",
    "同意",
    "协议",
    "阅读",
    "造好",
    "登录按钮",
    "checkbox",
    "CheckBox",
    "运营商",
    "认证服务",
    "隐私",
    "用户",
)


def _refine_login_row_candidates(
    candidates: List[Dict[str, Any]],
    screen_w: int,
    screen_h: int,
) -> List[Dict[str, Any]]:
    """登录图标行：过滤协议勾选/左侧杂项，聚类为水平图标行。"""
    if not candidates:
        return []
    max_w = int(screen_w * 0.22)
    max_h = int(screen_h * 0.08)
    x_min = int(screen_w * 0.18)

    filtered: List[Dict[str, Any]] = []
    for c in candidates:
        w, h = int(c["w"]), int(c["h"])
        if w < 20 or h < 20 or w > max_w or h > max_h:
            continue
        cx = int(c["x"]) + w // 2
        if cx < x_min:
            continue
        lbl = (c.get("label") or "").strip()
        low = lbl.lower()
        if any(k in lbl or k in low for k in _LOGIN_ROW_SKIP_FRAGMENTS):
            continue
        if len(lbl) > 8 and re.search(r"[\u4e00-\u9fff]{3,}", lbl):
            continue
        filtered.append(c)

    if not filtered:
        return []

    centers_y = sorted(int(c["y"]) + int(c["h"]) // 2 for c in filtered)
    row_y = centers_y[len(centers_y) // 2]
    band = max(36, int(screen_h * 0.035))
    row = [
        c
        for c in filtered
        if abs(int(c["y"]) + int(c["h"]) // 2 - row_y) <= band
    ]
    row.sort(key=lambda c: int(c["x"]))
    if len(row) >= 2:
        return row
    return filtered


def _grid_scan_full(screen_w: int, screen_h: int) -> List[Dict[str, Any]]:
    """全屏网格候选：hierarchy 无节点时仍做 CLIP 视觉匹配（与具体 App 无关）。"""
    y_lo, y_hi = 0.0, 1.0
    x_lo, x_hi = 0.0, 1.0
    cell = max(56, int(screen_w * 0.14))
    step_x = max(20, cell // 2)
    step_y = max(18, cell // 2)
    out: List[Dict[str, Any]] = []
    y = int(screen_h * y_lo)
    y_end = int(screen_h * y_hi)
    x_start = int(screen_w * x_lo)
    x_end = int(screen_w * x_hi)
    while y + cell <= y_end:
        x = x_start
        while x + cell <= x_end:
            out.append(
                {
                    "x": x,
                    "y": y,
                    "w": cell,
                    "h": cell,
                    "label": "",
                    "source": "grid",
                }
            )
            x += step_x
        y += step_y
    return out


def _grid_scan_login_row(
    screen_w: int,
    screen_h: int,
    *,
    row_y: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """登录图标行区域网格扫描候选（hierarchy 无字节点不可靠时的通用兜底）。"""
    y_lo, y_hi = REGION_BANDS["login_row"]
    y_mid = row_y or int(screen_h * (y_lo + y_hi) / 2)
    cell = max(52, int(screen_w * 0.065))
    half = max(32, int(screen_h * 0.03))
    y_start = max(int(screen_h * y_lo), y_mid - half)
    y_end = min(int(screen_h * y_hi), y_mid + half)
    x_start = int(screen_w * 0.22)
    x_end = int(screen_w * 0.78)
    step_x = max(18, cell // 3)
    step_y = max(14, cell // 4)
    out: List[Dict[str, Any]] = []
    y = y_start
    while y + cell <= y_end:
        x = x_start
        while x + cell <= x_end:
            out.append(
                {
                    "x": x,
                    "y": y,
                    "w": cell,
                    "h": cell,
                    "label": "",
                    "source": "grid",
                }
            )
            x += step_x
        y += step_y
    return out


def _login_row_intent_slot(query: str, n: int) -> Optional[int]:
    """图标行槽位 tie-break：按 query 文案启发式，不依赖登录专用分类器。"""
    if n <= 0:
        return None
    q = (query or "").strip()
    if re.search(r"微信", q, re.I):
        return 0
    if re.search(r"手机|电话|sms|phone", q, re.I):
        return n // 2
    if re.search(r"邮箱|密码|账号|email|password", q, re.I):
        return min(2, n - 1)
    if re.search(r"苹果|apple", q, re.I):
        return n - 1
    return None


def _score_patch_candidates(
    frame,
    candidates: List[Dict[str, Any]],
    text_vec: np.ndarray,
    svc,
    *,
    query: str,
    pad: int = 6,
) -> List[Tuple[float, Dict[str, Any]]]:
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for c in candidates:
        crop = _crop_bgr(frame, c["x"], c["y"], c["w"], c["h"], pad=pad)
        if crop is None:
            continue
        patch_emb = svc.encode_image(crop)
        if patch_emb is None:
            continue
        score = float(cosine_similarity(text_vec, patch_emb))
        scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


def _candidate_key(c: Dict[str, Any]) -> Tuple[int, int, int, int]:
    return (int(c["x"]), int(c["y"]), int(c["w"]), int(c["h"]))


def _hit_from_candidate(
    c: Dict[str, Any],
    score: float,
    *,
    query: str,
    method: str = "clip_patch",
) -> Dict[str, Any]:
    cx = int(c["x"]) + int(c["w"]) // 2
    cy = int(c["y"]) + int(c["h"]) // 2
    patch_label = (c.get("label") or "").strip()
    display = query if not patch_label or _is_generic_icon_name(patch_label) else patch_label
    return {
        "x": c["x"],
        "y": c["y"],
        "w": c["w"],
        "h": c["h"],
        "center": [cx, cy],
        "score": score,
        "t_score": score,
        "method": method,
        "detail": (
            f"CLIP「{query}」↔「{display}」score={score:.3f}"
            f" src={c.get('source') or 'patch'}"
        ),
        "label": display,
        "query": query,
    }


def _locate_login_row(
    frame,
    *,
    query: str,
    screen_w: int,
    screen_h: int,
    text_vec: np.ndarray,
    svc,
    threshold: float,
    engine,
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """登录图标行：先对 hierarchy 图标行打分，分数接近时按 intent 槽位 tie-break。"""
    raw = _collect_candidates_from_hierarchy(engine, screen_w, screen_h, region="login_row")
    row = _refine_login_row_candidates(raw, screen_w, screen_h)
    if len(row) < 2:
        return None, row

    scored = _score_patch_candidates(frame, row, text_vec, svc, query=query, pad=8)
    if not scored:
        return None, row

    top_score, top_c = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    margin = top_score - second_score
    pick_c, pick_score = top_c, top_score

    SLog.i(
        TAG,
        f"login_row row_only n={len(row)} top={top_score:.3f} "
        f"margin={margin:.3f} scores={[f'{s:.3f}' for s, _ in scored]}",
    )

    # 禁止按「左→右槽位」硬选图标；仅当 CLIP 最高分达标且领先明显时才点击
    row_threshold = max(threshold, _login_row_threshold())
    if margin < 0.06:
        SLog.i(
            TAG,
            f"login_row ambiguous top={top_score:.3f} margin={margin:.3f} query={query!r}",
        )
        if top_score < row_threshold:
            return None, row

    if pick_score < row_threshold:
        row_y = int(row[0]["y"]) + int(row[0]["h"]) // 2
        grid = _grid_scan_login_row(screen_w, screen_h, row_y=row_y)
        grid_scored = _score_patch_candidates(frame, grid, text_vec, svc, query=query, pad=4)
        if grid_scored and grid_scored[0][0] > pick_score:
            pick_score, pick_c = grid_scored[0]
            SLog.i(TAG, f"login_row grid fallback score={pick_score:.3f}")
        if pick_score < row_threshold:
            return None, row

    method = "clip_patch" if pick_c.get("source") != "grid" else "clip_grid"
    return _hit_from_candidate(pick_c, pick_score, query=query, method=method), row


def _collect_candidates_from_hierarchy(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    region: str = "full",
    max_items: int = 96,
) -> List[Dict[str, Any]]:
    _ = region
    try:
        from driver.agent.Crawl.ui_discovery import discover_clickables_from_hierarchy

        out: List[Dict[str, Any]] = []
        for t in discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=max_items):
            out.append(
                {
                    "x": int(t.x),
                    "y": int(t.y),
                    "w": int(t.w),
                    "h": int(t.h),
                    "label": (t.label or "").strip(),
                    "source": "hierarchy",
                }
            )
        return out
    except Exception as e:
        SLog.w(TAG, f"hierarchy candidates failed: {e}")
        return []


def _gallery_entries(
    icon_targets: Optional[Sequence[Dict[str, Any]]],
    text_vec: np.ndarray,
    *,
    query: str,
) -> List[Tuple[Dict[str, Any], float]]:
    scored: List[Tuple[Dict[str, Any], float]] = []
    for t in icon_targets or []:
        emb = _parse_embedding(t.get("clip_embedding"))
        names = [(t.get("name") or "").strip()]
        for a in t.get("aliases") or []:
            alias = str(a).strip() if a is not None else ""
            if alias:
                names.append(alias)
        name_boost = _icon_name_boost(query, names)
        if _is_generic_icon_name(names[0]) and not name_boost:
            continue
        if emb is None:
            img = _load_bgr_from_static(t.get("image_url") or "")
            if img is not None:
                x, y, w, h = int(t.get("x") or 0), int(t.get("y") or 0), int(t.get("w") or 48), int(t.get("h") or 48)
                emb = get_clip_service().encode_image(_crop_or_image(img, x, y, w, h))
        if emb is None:
            continue
        score = cosine_similarity(text_vec, emb)
        if name_boost:
            score += 0.04
        if score >= _gallery_threshold() or name_boost:
            scored.append((t, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored


def _icon_name_boost(query: str, names: List[str]) -> bool:
    q = (query or "").strip().lower()
    for n in names:
        n = (n or "").strip().lower()
        if not n:
            continue
        if _is_generic_icon_name(n):
            continue
        if q == n or q in n or n in q:
            return True
    return False


def _clip_hit_matches_query(query: str, hit: Dict[str, Any], *, region: str, screen_h: int) -> bool:
    """语义与区域校验，避免 icon_6 等泛匹配误点。"""
    from server.services.copilot_service import (  # noqa: lazy import
        _classify_login_method_intent,
        _is_one_click_login_label,
        _is_probable_bottom_tab_query,
        _match_bottom_tab_label,
        _match_target_label,
        parse_bottom_tab_label,
    )

    label = (hit.get("label") or "").strip()
    method = (hit.get("method") or "").strip()
    cx, cy = hit.get("center") or [0, 0]

    login_query = _is_one_click_login_label(query) or _classify_login_method_intent(query) == "one_click"
    if login_query:
        consent_labels = ("同意", "不同意", "拒绝", "始终允许", "仅在使用中允许")
        if label in consent_labels:
            SLog.i(
                TAG,
                f"reject clip hit: consent/permission for login query={query!r} label={label!r}",
            )
            return False
        # 一键登录主按钮走全屏 CLIP；底栏图标行不应作为 one_click 命中
        if region == "login_row":
            SLog.i(
                TAG,
                f"reject clip hit: one_click must use full-screen match query={query!r}",
            )
            return False
        if label and _match_target_label(query, label):
            return True
        if not label or _is_generic_icon_name(label):
            return float(hit.get("score") or 0) >= _score_threshold()
    if _is_generic_icon_name(label) and method.startswith("clip_gallery"):
        SLog.i(TAG, f"reject clip hit: generic gallery label={label!r} query={query!r}")
        return False

    tab_q = parse_bottom_tab_label(query) or (
        query if _is_probable_bottom_tab_query(query) else ""
    )
    if tab_q:
        if label and _match_bottom_tab_label(query, label):
            return True
        if label and re.search(r"[\u4e00-\u9fff]", label):
            SLog.i(TAG, f"reject clip tab hit: want={tab_q!r} got={label!r}")
            return False
        return True

    if _is_generic_icon_name(label):
        if region == "login_row" and method in ("clip_patch", "clip_grid", "clip_gallery_patch"):
            if float(hit.get("score") or 0) >= _score_threshold():
                return True
        SLog.i(TAG, f"reject clip hit: generic label={label!r} query={query!r}")
        return False

    if label and re.search(r"[\u4e00-\u9fff]", label):
        if _match_target_label(query, label):
            return True
        try:
            from server.services.local.locate.toggle_locate_service import parse_toggle_intent

            if parse_toggle_intent(query) and method in (
                "clip_toggle",
                "clip_patch",
                "clip_grid",
            ):
                if float(hit.get("score") or 0) >= _score_threshold():
                    return True
        except Exception:
            pass
        SLog.i(TAG, f"reject clip hit: label mismatch query={query!r} label={label!r}")
        return False

    return True


def compute_icon_embedding(
    *,
    image_url: str = "",
    image_bgr: Optional[np.ndarray] = None,
    x: int = 0,
    y: int = 0,
    w: int = 0,
    h: int = 0,
) -> Optional[List[float]]:
    if not clip_enabled():
        return None
    svc = get_clip_service()
    if not svc.available():
        return None
    img = image_bgr
    if img is None and image_url:
        img = _load_bgr_from_static(image_url)
    if img is None:
        return None
    if w > 0 and h > 0:
        crop = _crop_bgr(img, x, y, w, h)
        if crop is not None:
            img = crop
    vec = svc.encode_image(img)
    if vec is None:
        return None
    return vec.astype(np.float32).tolist()


def locate_on_screenshot(
    screenshot_bgr,
    query: str,
    *,
    screen_w: int,
    screen_h: int,
    region: str = "full",
    aliases: Optional[Sequence[str]] = None,
    icon_targets: Optional[Sequence[Dict[str, Any]]] = None,
    engine=None,
    extra_candidates: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    返回 {x,y,w,h,center,score,method,detail,label,matched_prompts?}
    """
    if not clip_enabled():
        return None
    svc = get_clip_service()
    if not svc.available():
        SLog.w(TAG, f"CLIP unavailable: {svc.last_error()}")
        return None

    frame = _screenshot_to_bgr(screenshot_bgr)
    if frame is None:
        SLog.w(TAG, f"screenshot unsupported type={type(screenshot_bgr).__name__}")
        return None

    text_vec = svc.encode_text_mixed(query, aliases)
    if text_vec is None:
        return None

    threshold = _score_threshold()
    gallery_threshold = _gallery_threshold()
    best: Optional[Dict[str, Any]] = None

    candidates: List[Dict[str, Any]] = list(extra_candidates or [])
    if engine is not None:
        for c in _collect_candidates_from_hierarchy(engine, screen_w, screen_h):
            if c not in candidates:
                candidates.append(c)

    scored_patches: List[Tuple[float, Dict[str, Any]]] = []
    for score, c in _score_patch_candidates(frame, candidates, text_vec, svc, query=query):
        scored_patches.append((score, c))
        if best is None or score > best["score"]:
            method = "clip_patch" if c.get("source") != "grid" else "clip_grid"
            best = _hit_from_candidate(c, score, query=query, method=method)

    if not scored_patches or (best and best["score"] < threshold):
        grid = _grid_scan_full(screen_w, screen_h)
        grid_scored = _score_patch_candidates(frame, grid, text_vec, svc, query=query, pad=4)
        for score, c in grid_scored:
            scored_patches.append((score, c))
            if best is None or score > best["score"]:
                best = _hit_from_candidate(c, score, query=query, method="clip_grid")
        if grid_scored:
            SLog.i(
                TAG,
                f"full grid fallback n={len(grid)} top={grid_scored[0][0]:.3f}",
            )

    gallery = _gallery_entries(icon_targets, text_vec, query=query)
    if gallery:
        top_icon, g_score = gallery[0]
        if g_score >= gallery_threshold or _icon_name_boost(
            query,
            [(top_icon.get("name") or "").strip()]
            + [str(a).strip() for a in (top_icon.get("aliases") or []) if a],
        ):
            icon_emb = _parse_embedding(top_icon.get("clip_embedding"))
            if icon_emb is None:
                gx, gy = int(top_icon.get("x") or 0), int(top_icon.get("y") or 0)
                gw, gh = int(top_icon.get("w") or 48), int(top_icon.get("h") or 48)
                img = _load_bgr_from_static(top_icon.get("image_url") or "")
                if img is not None:
                    icon_emb = svc.encode_image(_crop_or_image(img, gx, gy, gw, gh))

            search_candidates = list(candidates)
            if icon_emb is not None and search_candidates:
                for c in search_candidates:
                    crop = _crop_bgr(frame, c["x"], c["y"], c["w"], c["h"])
                    if crop is None:
                        continue
                    patch_emb = svc.encode_image(crop)
                    if patch_emb is None:
                        continue
                    t_score = cosine_similarity(text_vec, patch_emb)
                    if t_score < threshold:
                        continue
                    i_score = cosine_similarity(icon_emb, patch_emb)
                    score = 0.75 * t_score + 0.25 * i_score
                    if best is None or score > best["score"]:
                        cx = c["x"] + c["w"] // 2
                        cy = c["y"] + c["h"] // 2
                        best = {
                            "x": c["x"],
                            "y": c["y"],
                            "w": c["w"],
                            "h": c["h"],
                            "center": [cx, cy],
                            "score": score,
                            "t_score": t_score,
                            "method": "clip_gallery_patch",
                            "detail": (
                                f"CLIP 图标库「{top_icon.get('name')}」+ 屏上候选 "
                                f"score={score:.3f} text={t_score:.3f}"
                            ),
                            "label": c.get("label") or top_icon.get("name"),
                            "icon_name": top_icon.get("name"),
                        }
            elif icon_emb is not None and not _is_generic_icon_name(top_icon.get("name") or ""):
                gx, gy = int(top_icon.get("x") or 0), int(top_icon.get("y") or 0)
                gw, gh = int(top_icon.get("w") or 0), int(top_icon.get("h") or 0)
                if gw <= 0 or gh <= 0:
                    gx, gy, gw, gh = 0, 0, 0, 0
                cx, cy = gx + gw // 2, gy + gh // 2
                if gw > 0 and gh > 0:
                    if g_score >= gallery_threshold:
                        coord_hit = {
                            "x": gx,
                            "y": gy,
                            "w": gw,
                            "h": gh,
                            "center": [cx, cy],
                            "score": g_score,
                            "t_score": g_score,
                            "method": "clip_gallery_coord",
                            "detail": f"CLIP 图标库「{top_icon.get('name')}」score={g_score:.3f}",
                            "label": top_icon.get("name"),
                            "icon_name": top_icon.get("name"),
                        }
                        if best is None or coord_hit["score"] > best["score"]:
                            best = coord_hit

    if best:
        if not _clip_hit_matches_query(query, best, region="full", screen_h=screen_h):
            SLog.i(
                TAG,
                f"locate rejected query={query!r} region={region} "
                f"label={best.get('label')!r} score={best['score']:.3f}",
            )
            return None
        best["prompts"] = build_mixed_text_prompts(query, aliases)
        best["clip_model"] = svc.model_tag
        SLog.i(
            TAG,
            f"locate ok query={query!r} region={region} score={best['score']:.3f} "
            f"method={best.get('method')} label={best.get('label')!r}",
        )
        return best
    SLog.i(TAG, f"locate no candidate query={query!r} region={region}")
    return None


def try_clip_locate(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    label: str,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    region: Optional[str] = None,
    query: Optional[str] = None,
    aliases: Optional[Sequence[str]] = None,
) -> Tuple[Optional[Tuple[int, int]], str, str, Optional[Dict[str, Any]]]:
    """供 copilot _resolve_click_target 调用的薄封装。"""
    if not label and not query:
        return None, "none", "", None
    try:
        from server.services.shared.screenshot.screen_frame_service import get_screen_frame

        frame = get_screen_frame(engine)
        if frame.get("screen_not_ready"):
            return None, "none", "屏未就绪", None
        shot = frame.get("shot")
    except Exception as e:
        SLog.w(TAG, f"screen frame failed: {e}")
        return None, "none", f"CLIP 截图失败: {e}", None
    if shot is None:
        return None, "none", "CLIP 无截图", None

    q = (query or label or "").strip()
    _ = region
    alias_list: List[str] = [str(a).strip() for a in (aliases or []) if str(a).strip()]
    SLog.i(
        TAG,
        f"try label={label!r} query={q!r} region=full "
        f"icons={len(icon_targets or [])} aliases={len(alias_list)}",
    )

    hit = locate_on_screenshot(
        shot,
        q,
        screen_w=screen_w,
        screen_h=screen_h,
        region="full",
        aliases=alias_list,
        icon_targets=icon_targets,
        engine=engine,
    )
    if not hit:
        return (
            None,
            "none",
            f"CLIP 未命中「{q}」（region=full，threshold={_score_threshold():.2f}）",
            None,
        )
    cx, cy = hit["center"]
    rect = {
        "left": hit["x"],
        "top": hit["y"],
        "width": hit["w"],
        "height": hit["h"],
        "center": [cx, cy],
        "label": hit.get("label") or q,
    }
    return (cx, cy), hit.get("method") or "clip", hit.get("detail") or "", rect
