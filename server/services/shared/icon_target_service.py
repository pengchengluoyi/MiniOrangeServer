# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""无字图标目标：独立表 CRUD，可与应用图谱组件关联。"""
from __future__ import annotations

import uuid
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from server.models.app_icon_target import AppIconTarget
from server.models.AppGraph.app_structure import AppGraph
from server.models.AppGraph.app_component import AppComponent


TAG = "IconTarget"

_ICON_AUTO_RE = re.compile(r"^icon[_\-]?\w+$", re.I)


def should_auto_learn_icon(*, method: str, target_label: str, target_rect: Optional[Dict[str, Any]]) -> bool:
    """层级/OCR 发现的无字图标名（如 icon_6）执行成功后自动入库。"""
    m = (method or "").strip()
    if m == "icon_target" or m.startswith("clip"):
        return False
    rect = target_rect or {}
    if int(rect.get("width") or 0) < 8 or int(rect.get("height") or 0) < 8:
        return False
    label = (target_label or "").strip()
    if not label:
        return False
    if _ICON_AUTO_RE.match(label):
        return method in ("hierarchy", "ocr")
    if method in ("hierarchy", "ocr") and not re.search(r"[\u4e00-\u9fff]", label):
        return bool(re.match(r"^[a-zA-Z][\w\-]*$", label))
    return False


def auto_learn_from_click(
    app_id: str,
    click_result: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """执行点击成功后自动写入/更新无字图标库。"""
    app_id = (app_id or "").strip()
    if not app_id or not click_result.get("ok"):
        return None
    method = click_result.get("method") or ""
    label = (click_result.get("target_label") or "").strip()
    rect = click_result.get("target_rect")
    if not should_auto_learn_icon(method=method, target_label=label, target_rect=rect):
        return None

    from server.core.database import SessionLocal

    session = SessionLocal()
    try:
        # 将 icon 名称标准化：项目ID-应用ID-原始label
        # 这样同一个 icon_* 在不同项目/应用下也能保持区分，并且便于后续统一维护别名。
        from server.models.project import App

        app = session.query(App).filter(App.id == app_id).first()
        project_code = (getattr(app, "project_id", "") or "")[:6]
        app_code = app_id[:8]
        std_name = f"{project_code}-{app_code}-{label}" if project_code else f"{app_code}-{label}"

        aliases = list({label})  # 至少保留原始 label，保证匹配仍能工作
        row = import_from_locate(
            session,
            app_id,
            {
                "name": std_name,
                "target_label": label,
                "target_rect": rect,
                "aliases": aliases,
                "screenshot": click_result.get("screenshot_after")
                or click_result.get("screenshot_before")
                or "",
                "note": f"执行时自动入库（{method}）",
            },
        )
        return row
    except Exception:
        session.rollback()
        return None
    finally:
        session.close()


def learned_icon_for_copilot(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "x": row.get("x"),
        "y": row.get("y"),
        "w": row.get("w"),
        "h": row.get("h"),
        "image_url": row.get("image_url") or "",
        "aliases": row.get("aliases") or [],
    }


def list_icon_targets(
    db: Session,
    app_id: str,
    *,
    page: int = 1,
    page_size: int = 50,
    keyword: str = "",
) -> Dict[str, Any]:
    q = db.query(AppIconTarget).filter(AppIconTarget.app_id == app_id)
    if keyword:
        kw = f"%{keyword.strip()}%"
        q = q.filter(AppIconTarget.name.like(kw))
    total = q.count()
    rows = (
        q.order_by(AppIconTarget.updated_at.desc())
        .offset(max(0, page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [_row_to_dict(r) for r in rows],
    }


def _row_to_dict(row: AppIconTarget) -> Dict[str, Any]:
    return {
        "id": row.id,
        "app_id": row.app_id,
        "name": row.name,
        "aliases": row.aliases if isinstance(row.aliases, list) else [],
        "x": row.x,
        "y": row.y,
        "w": row.w,
        "h": row.h,
        "image_url": row.image_url or "",
        "clip_embedding": row.clip_embedding if isinstance(row.clip_embedding, list) else None,
        "clip_model": row.clip_model or "",
        "region_hint": row.region_hint or "",
        "graph_id": row.graph_id,
        "component_uid": row.component_uid,
        "page_node_id": row.page_node_id,
        "note": row.note or "",
    }


def _maybe_attach_clip_embedding(payload: Dict[str, Any]) -> Dict[str, Any]:
    """有截图模板时计算 CLIP embedding（中英文 query 匹配用）。"""
    image_url = (payload.get("image_url") or "").strip()
    if not image_url:
        return payload
    try:
        from server.services.local.locate.clip_locate_service import compute_icon_embedding
        from server.core.vision.clip_service import get_clip_service

        emb = compute_icon_embedding(
            image_url=image_url,
            x=int(payload.get("x") or 0),
            y=int(payload.get("y") or 0),
            w=int(payload.get("w") or 0),
            h=int(payload.get("h") or 0),
        )
        if emb is not None and len(emb) > 0:
            payload["clip_embedding"] = emb
            svc = get_clip_service()
            if svc.available():
                payload["clip_model"] = svc.model_tag
    except Exception as e:
        SLog.w(TAG, f"clip embedding skipped: {e}")
    return payload


def upsert_icon_target(
    db: Session,
    app_id: str,
    data: Dict[str, Any],
    *,
    commit: bool = True,
) -> Dict[str, Any]:
    tid = (data.get("id") or "").strip()
    row = None
    if tid:
        row = db.query(AppIconTarget).filter(AppIconTarget.id == tid, AppIconTarget.app_id == app_id).first()
    if not row:
        row = AppIconTarget(id=uuid.uuid4().hex[:16], app_id=app_id)
        db.add(row)
    row.name = (data.get("name") or "").strip() or row.name or "未命名"
    row.aliases = data.get("aliases") if isinstance(data.get("aliases"), list) else []
    row.x = int(data.get("x") or 0)
    row.y = int(data.get("y") or 0)
    row.w = int(data.get("w") or 0)
    row.h = int(data.get("h") or 0)
    if data.get("image_url") is not None:
        row.image_url = data.get("image_url") or ""
    work = {
        "image_url": row.image_url,
        "x": row.x,
        "y": row.y,
        "w": row.w,
        "h": row.h,
        **{k: data[k] for k in ("clip_embedding", "clip_model", "region_hint") if k in data},
    }
    if data.get("image_url") and not data.get("clip_embedding"):
        work = _maybe_attach_clip_embedding(work)
        if work.get("clip_embedding") is not None:
            row.clip_embedding = work["clip_embedding"]
        if work.get("clip_model"):
            row.clip_model = work["clip_model"]
    row.graph_id = data.get("graph_id")
    row.component_uid = data.get("component_uid")
    row.page_node_id = data.get("page_node_id")
    row.note = (data.get("note") or "").strip()
    if data.get("region_hint") is not None:
        row.region_hint = (data.get("region_hint") or "").strip()
    if data.get("clip_embedding") is not None:
        row.clip_embedding = data.get("clip_embedding")
    if data.get("clip_model") is not None:
        row.clip_model = (data.get("clip_model") or "").strip()
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
    return _row_to_dict(row)


def delete_icon_target(db: Session, app_id: str, target_id: str) -> bool:
    row = db.query(AppIconTarget).filter(AppIconTarget.id == target_id, AppIconTarget.app_id == app_id).first()
    if not row:
        return False
    db.delete(row)
    db.commit()
    return True


def list_for_copilot(db: Session, app_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    rows = (
        db.query(AppIconTarget)
        .filter(AppIconTarget.app_id == app_id)
        .order_by(AppIconTarget.name)
        .limit(limit)
        .all()
    )
    return [_row_to_dict(r) for r in rows]


def import_from_locate(
    db: Session,
    app_id: str,
    data: Dict[str, Any],
) -> Dict[str, Any]:
    """从执行回放定位结果写入无字图标库（便于后续 icon_target 匹配）。"""
    name = (data.get("name") or data.get("target_label") or "").strip()
    rect = data.get("target_rect") if isinstance(data.get("target_rect"), dict) else {}
    x = int(data.get("x") or rect.get("left") or 0)
    y = int(data.get("y") or rect.get("top") or 0)
    w = int(data.get("w") or rect.get("width") or 0)
    h = int(data.get("h") or rect.get("height") or 0)
    if not name:
        raise ValueError("图标名称不能为空")
    if w <= 0 or h <= 0:
        raise ValueError("缺少有效区域宽高")

    aliases = data.get("aliases") if isinstance(data.get("aliases"), list) else []
    screenshot = (data.get("screenshot") or data.get("image_url") or "").strip()
    note = (data.get("note") or "从执行回放定位导入").strip()

    existing = (
        db.query(AppIconTarget)
        .filter(AppIconTarget.app_id == app_id, AppIconTarget.name == name)
        .first()
    )
    payload: Dict[str, Any] = {
        "name": name,
        "aliases": aliases,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "note": note,
    }
    if screenshot:
        payload["image_url"] = screenshot
    if existing:
        payload["id"] = existing.id
    payload = _maybe_attach_clip_embedding(payload)
    return upsert_icon_target(db, app_id, payload)


def list_graph_import_candidates(db: Session, app_id: str) -> List[Dict[str, Any]]:
    graph = db.query(AppGraph).filter(AppGraph.app_id == app_id).first()
    if not graph:
        return []
    out = []
    for comp in db.query(AppComponent).filter(AppComponent.graph_id == graph.id).all():
        out.append(
            {
                "component_uid": comp.uid,
                "label": comp.label or comp.name,
                "x": int(comp.x or 0),
                "y": int(comp.y or 0),
                "w": int(comp.width or 0),
                "h": int(comp.height or 0),
                "graph_id": graph.id,
                "skeleton_config": comp.skeleton_config or {},
            }
        )
    return out


def import_from_graph_component(
    db: Session,
    app_id: str,
    component_uid: str,
) -> Optional[Dict[str, Any]]:
    graph = db.query(AppGraph).filter(AppGraph.app_id == app_id).first()
    if not graph:
        return None
    comp = (
        db.query(AppComponent)
        .filter(AppComponent.graph_id == graph.id, AppComponent.uid == component_uid)
        .first()
    )
    if not comp:
        return None
    name = (comp.label or comp.name or comp.uid or "").strip()
    sk = comp.skeleton_config if isinstance(comp.skeleton_config, dict) else {}
    image_url = ""
    if isinstance(sk.get("images"), list) and sk["images"]:
        image_url = sk["images"][0] if isinstance(sk["images"][0], str) else ""
    elif sk.get("mask_url"):
        image_url = sk.get("mask_url") or ""
    return upsert_icon_target(
        db,
        app_id,
        {
            "name": name,
            "x": int(comp.x or 0),
            "y": int(comp.y or 0),
            "w": int(comp.width or 0),
            "h": int(comp.height or 0),
            "image_url": image_url,
            "graph_id": graph.id,
            "component_uid": comp.uid,
            "note": "从应用图谱组件导入",
        },
    )
