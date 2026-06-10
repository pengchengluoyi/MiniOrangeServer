# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""从 Figma 登录/注册页提取底部登录方式图标，写入无字图标库。"""
from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from script.log import SLog
from server.core.database import APP_DATA_DIR
from server.services import app_automation_service as aas
from server.services import figma_service as fs
from server.services.execution_clarification_service import template_for_intent

TAG = "FigmaIcon"

LOGIN_PAGE_RE = re.compile(
    r"登录|注册|登陆|sign[\s_-]*in|sign[\s_-]*up|log[\s_-]*in|register",
    re.I,
)

DEFAULT_REF_SCREEN = (1200, 2608)

_ICON_VISUAL_TYPES = frozenset(
    {
        "VECTOR",
        "INSTANCE",
        "COMPONENT",
        "ELLIPSE",
        "RECTANGLE",
        "BOOLEAN_OPERATION",
        "STAR",
        "REGULAR_POLYGON",
        "FRAME",
        "GROUP",
    }
)

_SKIP_NAME_FRAGMENTS = (
    "一键",
    "本机",
    "同意",
    "协议",
    "checkbox",
    "check",
    "登录按钮",
    "button",
    "logo",
    "背景",
    "divider",
    "分割",
    "状态栏",
    "navbar",
    "home indicator",
)

_FIGMA_NAME_INTENT_HINTS: Dict[str, Tuple[str, ...]] = {
    "wechat": ("微信", "wechat", "weixin", "wx"),
    "phone_sms": ("手机", "phone", "mobile", "sms", "验证码", "短信", "verify"),
    "email_password": ("邮箱", "密码", "email", "password", "account", "账号", "帐号"),
    "apple": ("apple", "苹果", "appleid", "apple id"),
}

_DEFAULT_ICON_ORDER = ("wechat", "phone_sms", "email_password", "apple")


def _abs_bbox(node: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    box = node.get("absoluteBoundingBox") or node.get("absoluteRenderBounds")
    if not isinstance(box, dict):
        return None
    x = float(box.get("x") or 0)
    y = float(box.get("y") or 0)
    w = float(box.get("width") or 0)
    h = float(box.get("height") or 0)
    if w < 4 or h < 4:
        return None
    return x, y, w, h


def _rel_bbox(
    bbox: Tuple[float, float, float, float],
    frame_bbox: Tuple[float, float, float, float],
) -> Tuple[float, float, float, float]:
    fx, fy, fw, fh = frame_bbox
    ix, iy, iw, ih = bbox
    if fw <= 0 or fh <= 0:
        return 0.0, 0.0, 0.0, 0.0
    return (
        (ix - fx) / fw,
        (iy - fy) / fh,
        iw / fw,
        ih / fh,
    )


def _name_is_skip(name: str) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return False
    return any(k.lower() in n for k in _SKIP_NAME_FRAGMENTS)


def _intent_from_figma_name(name: str) -> Optional[str]:
    raw = (name or "").strip()
    if not raw:
        return None
    low = raw.lower()
    for intent, hints in _FIGMA_NAME_INTENT_HINTS.items():
        for h in hints:
            if h.lower() in low or h in raw:
                return intent
    return None


def _in_icon_band(rel: Tuple[float, float, float, float]) -> bool:
    _, ry, _, rh = rel
    cy = ry + rh / 2.0
    return 0.64 <= cy <= 0.90


def _icon_size_ok(rel: Tuple[float, float, float, float]) -> bool:
    _, _, rw, rh = rel
    return 0.015 <= rw <= 0.24 and 0.015 <= rh <= 0.12


def _overlap_area(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1 = max(ax, bx)
    y1 = max(ay, by)
    x2 = min(ax + aw, bx + bw)
    y2 = min(ay + ah, by + bh)
    if x2 <= x1 or y2 <= y1:
        return 0.0
    return (x2 - x1) * (y2 - y1)


def _dedupe_icon_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not candidates:
        return []
    kept: List[Dict[str, Any]] = []
    for c in sorted(candidates, key=lambda x: x["bbox"][2] * x["bbox"][3]):
        bbox = c["bbox"]
        dominated = False
        for k in kept:
            inter = _overlap_area(bbox, k["bbox"])
            smaller = min(bbox[2] * bbox[3], k["bbox"][2] * k["bbox"][3])
            if smaller > 0 and inter / smaller > 0.55:
                dominated = True
                break
        if not dominated:
            kept.append(c)
    return kept


def _cluster_icon_row(
    candidates: List[Dict[str, Any]],
    frame_bbox: Tuple[float, float, float, float],
) -> List[Dict[str, Any]]:
    if not candidates:
        return []
    _, fy, _, fh = frame_bbox
    band_items = [c for c in candidates if _in_icon_band(c["norm"]) and _icon_size_ok(c["norm"])]
    if not band_items:
        band_items = [c for c in candidates if _icon_size_ok(c["norm"])]
    if not band_items:
        return []

    centers_y = [c["bbox"][1] + c["bbox"][3] / 2.0 for c in band_items]
    centers_y.sort()
    row_y = centers_y[len(centers_y) // 2]
    band = max(24.0, fh * 0.04)
    row = [c for c in band_items if abs((c["bbox"][1] + c["bbox"][3] / 2.0) - row_y) <= band]
    row.sort(key=lambda c: c["bbox"][0])
    return _dedupe_icon_candidates(row)


def _collect_login_icons_in_frame(frame_node: Dict[str, Any]) -> List[Dict[str, Any]]:
    frame_bbox = _abs_bbox(frame_node)
    if not frame_bbox:
        return []

    candidates: List[Dict[str, Any]] = []

    def walk(node: Dict[str, Any], depth: int = 0) -> None:
        if depth > 14 or not isinstance(node, dict):
            return
        ntype = (node.get("type") or "").strip()
        name = (node.get("name") or "").strip()
        if ntype == "TEXT":
            return
        if _name_is_skip(name) and ntype not in ("INSTANCE", "COMPONENT"):
            return

        bbox = _abs_bbox(node)
        children = node.get("children") or []
        is_container = ntype in ("FRAME", "GROUP") and children
        is_visual = ntype in _ICON_VISUAL_TYPES and not is_container

        if bbox and is_visual and not _name_is_skip(name):
            rel = _rel_bbox(bbox, frame_bbox)
            if _in_icon_band(rel) and _icon_size_ok(rel):
                candidates.append(
                    {
                        "figma_id": node.get("id") or "",
                        "name": name,
                        "bbox": bbox,
                        "norm": rel,
                        "depth": depth,
                        "type": ntype,
                    }
                )
                if ntype in ("INSTANCE", "COMPONENT", "VECTOR"):
                    return

        for child in children:
            walk(child, depth + 1)

    walk(frame_node)
    return _cluster_icon_row(candidates, frame_bbox)


def _find_login_register_frames(doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []

    def walk(node: Dict[str, Any]) -> None:
        if not isinstance(node, dict):
            return
        ntype = (node.get("type") or "").strip()
        name = (node.get("name") or "").strip()
        if ntype in ("FRAME", "COMPONENT", "INSTANCE", "SECTION") and name:
            if LOGIN_PAGE_RE.search(name):
                found.append(node)
        for child in node.get("children") or []:
            walk(child)

    walk(doc)
    return found


def _pick_best_login_frame(frames: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not frames:
        return None
    scored: List[Tuple[int, Dict[str, Any]]] = []
    for frame in frames:
        icons = _collect_login_icons_in_frame(frame)
        score = len(icons) * 10
        name = (frame.get("name") or "").lower()
        if "登录" in name or "login" in name:
            score += 5
        if "注册" in name or "register" in name:
            score += 2
        scored.append((score, frame))
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best = scored[0]
    if best_score <= 0:
        return None
    return best


def _assign_intents_to_row(row: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    used_intents: set = set()
    for item in row:
        intent = _intent_from_figma_name(item.get("name") or "")
        if intent and intent not in used_intents:
            used_intents.add(intent)
            out.append({**item, "intent": intent})
        else:
            out.append({**item, "intent": None})

    unset = [i for i, x in enumerate(out) if not x.get("intent")]
    free_intents = [k for k in _DEFAULT_ICON_ORDER if k not in used_intents]
    for idx, intent in zip(unset, free_intents):
        out[idx]["intent"] = intent
        used_intents.add(intent)
    return out


def fetch_figma_document_deep(
    *,
    file_url: str = "",
    file_key: str = "",
    depth: int = 8,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    key = fs.parse_file_key(file_url, file_key)
    if not key:
        raise ValueError("无法解析 Figma file_key")
    resp = fs.figma_get(
        f"{fs.FIGMA_API}/files/{key}",
        token=token,
        params={"depth": max(4, min(int(depth), 10))},
        timeout=120,
    )
    if resp.status_code == 403:
        raise ValueError("Figma Token 无效或无权访问该文件")
    if resp.status_code == 429:
        raise ValueError("Figma API 请求过于频繁 (429)，请 1–2 分钟后再试")
    if not resp.ok:
        raise ValueError(f"Figma 文件读取失败 ({resp.status_code})")
    return resp.json() or {}


def _unwrap_figma_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """统一整文件 / nodes API / raw_document 结构。"""
    if not payload:
        return {}
    if payload.get("document"):
        return payload
    nodes = payload.get("nodes") or {}
    for wrap in nodes.values():
        if not isinstance(wrap, dict):
            continue
        doc = wrap.get("document")
        if isinstance(doc, dict):
            return {"document": doc}
    return payload


def _resolve_figma_payload_for_icons(
    app,
    *,
    file_url: str = "",
    file_key: str = "",
    token: Optional[str] = None,
    document: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if document:
        return _unwrap_figma_payload(document)

    cfg = aas.get_automation_config(app)
    figma_cfg = cfg.get("figma") or {}
    url = (file_url or figma_cfg.get("file_url") or "").strip()
    key = fs.parse_file_key(url, file_key or figma_cfg.get("file_key") or "")
    frame_id = (figma_cfg.get("login_frame") or {}).get("figma_id") or ""

    if key and frame_id:
        try:
            SLog.i(TAG, f"fetch login frame node only id={frame_id[-8:]}")
            return _unwrap_figma_payload(fs.fetch_figma_nodes(key, [frame_id], depth=6, token=token))
        except Exception as e:
            SLog.w(TAG, f"fetch login frame node failed, fallback full file: {e}")

    return fetch_figma_document_deep(file_url=url, file_key=key, depth=8, token=token)


def export_figma_node_images(
    file_key: str,
    node_ids: List[str],
    *,
    token: Optional[str] = None,
    scale: int = 2,
) -> Dict[str, str]:
    out: Dict[str, str] = {}
    ids = [i for i in node_ids if i]
    for i in range(0, len(ids), 40):
        batch = ids[i : i + 40]
        resp = fs.figma_get(
            f"{fs.FIGMA_API}/images/{file_key}",
            token=token,
            params={"ids": ",".join(batch), "format": "png", "scale": scale},
            timeout=120,
        )
        if not resp.ok:
            SLog.w(TAG, f"figma images batch failed: {resp.status_code}")
            continue
        images = (resp.json() or {}).get("images") or {}
        for nid, url in images.items():
            if url:
                out[nid] = url
    return out


def _save_image_from_url(url: str, app_id: str, tag: str) -> str:
    uploads = os.path.join(APP_DATA_DIR, "uploads")
    os.makedirs(uploads, exist_ok=True)
    resp = requests.get(url, timeout=90)
    resp.raise_for_status()
    fname = f"figma_icon_{app_id[:8]}_{tag}_{uuid.uuid4().hex[:8]}.png"
    path = os.path.join(uploads, fname)
    with open(path, "wb") as f:
        f.write(resp.content)
    return f"/static/{fname}"


def _norm_to_ref_pixels(
    norm: Tuple[float, float, float, float],
    ref_w: int,
    ref_h: int,
) -> Tuple[int, int, int, int]:
    rx, ry, rw, rh = norm
    return (
        int(rx * ref_w),
        int(ry * ref_h),
        max(1, int(rw * ref_w)),
        max(1, int(rh * ref_h)),
    )


def _reference_screen(app) -> Tuple[int, int]:
    cfg = aas.get_automation_config(app)
    ref = (cfg.get("figma") or {}).get("login_reference") or {}
    w = int(ref.get("w") or DEFAULT_REF_SCREEN[0])
    h = int(ref.get("h") or DEFAULT_REF_SCREEN[1])
    return max(320, w), max(480, h)


def _should_preserve_existing(row) -> bool:
    if not row:
        return False
    note = (row.note or "").strip()
    if "人工标定" in note and int(row.w or 0) > 0:
        return True
    return False


def extract_login_icons_from_document(
    document: Dict[str, Any],
) -> Dict[str, Any]:
    """从 Figma document 树解析登录页图标（不调图片 API）。"""
    payload = _unwrap_figma_payload(document)
    doc = payload.get("document") or payload
    frames = _find_login_register_frames(doc)
    if not frames and isinstance(doc, dict):
        name = (doc.get("name") or "").strip()
        ntype = (doc.get("type") or "").strip()
        if ntype in ("FRAME", "COMPONENT", "INSTANCE", "SECTION") and LOGIN_PAGE_RE.search(name):
            frames = [doc]
    frame = _pick_best_login_frame(frames)
    if not frame:
        return {
            "ok": False,
            "msg": "未在设计稿中找到登录/注册页 Frame",
            "frame_name": "",
            "icons": [],
        }

    row = _collect_login_icons_in_frame(frame)
    if not row:
        return {
            "ok": False,
            "msg": f"页面「{frame.get('name')}」未识别到底部登录图标行",
            "frame_name": frame.get("name") or "",
            "icons": [],
        }

    assigned = _assign_intents_to_row(row)
    frame_bbox = _abs_bbox(frame) or (0, 0, 1, 1)
    return {
        "ok": True,
        "msg": f"识别到 {len(assigned)} 个登录图标",
        "frame_name": frame.get("name") or "",
        "frame_figma_id": frame.get("id") or "",
        "frame_bbox": list(frame_bbox),
        "icons": assigned,
    }


def seed_login_icons_from_figma(
    db,
    app,
    *,
    file_url: str = "",
    file_key: str = "",
    token: Optional[str] = None,
    overwrite_figma: bool = True,
    document: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    从 Figma 登录/注册页导入图标到 icon_targets。
    坐标按 figma_norm 存相对比例，并写入参考分辨率像素值。
    """
    from server.models.app_icon_target import AppIconTarget
    from server.services.icon_target_service import upsert_icon_target

    cfg = aas.get_automation_config(app)
    figma_cfg = cfg.get("figma") or {}
    url = (file_url or figma_cfg.get("file_url") or "").strip()
    key = fs.parse_file_key(url, file_key or figma_cfg.get("file_key") or "")

    payload = _resolve_figma_payload_for_icons(
        app,
        file_url=url,
        file_key=key,
        token=token,
        document=document,
    )
    extracted = extract_login_icons_from_document(payload)
    if not extracted.get("ok"):
        return {
            "source": "figma",
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "icons": [],
            "msg": extracted.get("msg") or "提取失败",
        }

    icons = extracted.get("icons") or []
    ref_w, ref_h = _reference_screen(app)
    image_map = export_figma_node_images(
        key,
        [i.get("figma_id") or "" for i in icons],
        token=token,
    )

    created = updated = skipped = 0
    saved_rows: List[Dict[str, Any]] = []

    for item in icons:
        intent = item.get("intent") or ""
        tpl = template_for_intent(intent) or {}
        name = (tpl.get("name") or f"登录-{intent}" or item.get("name") or "登录图标").strip()
        if not name:
            continue

        existing = (
            db.query(AppIconTarget)
            .filter(AppIconTarget.app_id == app.id, AppIconTarget.name == name)
            .first()
        )
        if _should_preserve_existing(existing):
            skipped += 1
            continue
        if existing and not overwrite_figma and int(existing.w or 0) > 0:
            skipped += 1
            continue

        norm = item.get("norm") or (0, 0, 0, 0)
        x, y, w, h = _norm_to_ref_pixels(tuple(norm), ref_w, ref_h)
        if w <= 0 or h <= 0:
            skipped += 1
            continue

        image_url = ""
        fid = item.get("figma_id") or ""
        img_remote = image_map.get(fid) or ""
        if img_remote:
            try:
                image_url = _save_image_from_url(img_remote, app.id, intent or "icon")
            except Exception as e:
                SLog.w(TAG, f"save figma image failed {fid}: {e}")

        rx, ry, rw, rh = norm
        note = (
            f"intent:{intent}; figma_norm:{rx:.4f},{ry:.4f},{rw:.4f},{rh:.4f}; "
            f"ref:{ref_w}x{ref_h}; figma_id:{fid}; "
            f"frame:{extracted.get('frame_name')}; node:{item.get('name')}; 来自 Figma 设计稿"
        )
        payload_row: Dict[str, Any] = {
            "name": name,
            "aliases": list(tpl.get("aliases") or []),
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "image_url": image_url,
            "note": note,
        }
        if existing:
            payload_row["id"] = existing.id
            updated += 1
        else:
            created += 1

        row = upsert_icon_target(db, app.id, payload_row, commit=False)
        saved_rows.append(row)

    if created or updated:
        figma_patch = dict(figma_cfg)
        figma_patch["login_frame"] = {
            "name": extracted.get("frame_name") or "",
            "figma_id": extracted.get("frame_figma_id") or "",
            "bbox": extracted.get("frame_bbox") or [],
            "icon_count": len(saved_rows),
            "seeded_at": datetime.now(timezone.utc).isoformat(),
        }
        figma_patch["login_reference"] = {"w": ref_w, "h": ref_h}
        aas.save_automation_config(app, {"figma": figma_patch})

    db.commit()
    SLog.i(
        TAG,
        f"seed login icons app={app.id} created={created} updated={updated} skipped={skipped}",
    )
    return {
        "source": "figma",
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "frame_name": extracted.get("frame_name") or "",
        "icons": saved_rows,
        "msg": extracted.get("msg") or "",
    }


def scale_icon_target_rect(
    target: Dict[str, Any],
    screen_w: int,
    screen_h: int,
) -> Tuple[int, int, int, int]:
    """将图标库坐标按 figma_norm 或 ref 缩放到当前设备分辨率。"""
    note = (target.get("note") or "").strip()
    m = re.search(
        r"figma_norm:([\d.]+),([\d.]+),([\d.]+),([\d.]+)",
        note,
    )
    if m and screen_w > 0 and screen_h > 0:
        rx, ry, rw, rh = (float(m.group(i)) for i in range(1, 5))
        return (
            int(rx * screen_w),
            int(ry * screen_h),
            max(1, int(rw * screen_w)),
            max(1, int(rh * screen_h)),
        )

    x = int(target.get("x") or 0)
    y = int(target.get("y") or 0)
    w = int(target.get("w") or 0)
    h = int(target.get("h") or 0)
    ref_m = re.search(r"ref:(\d+)x(\d+)", note)
    if ref_m and screen_w > 0 and screen_h > 0:
        ref_w, ref_h = int(ref_m.group(1)), int(ref_m.group(2))
        if ref_w > 0 and ref_h > 0:
            sx = screen_w / ref_w
            sy = screen_h / ref_h
            return int(x * sx), int(y * sy), max(1, int(w * sx)), max(1, int(h * sy))
    return x, y, w, h
