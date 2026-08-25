# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""图谱别名表存储层。只读写，不掺对齐业务规则。"""
from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Iterator, Optional

from sqlalchemy.orm import Session

from server.core.database import SessionLocal
from server.models.atlas_alias import MAtlasAlias
from server.services.ai.atlas_align import norm_name


@contextlib.contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _clip_alias(text: str) -> str:
    """和 apply_mindmap 截断一致：模块/功能 ≤20 字。别名 key 必须按截断后文本算。"""
    s = str(text or "").strip()
    return s[:20] if len(s) > 20 else s


def approved_map(db: Session, app_id: str) -> dict[str, str]:
    """归一化别名 → target_id。只含 approved。"""
    rows = (
        db.query(MAtlasAlias)
        .filter(
            MAtlasAlias.app_id == str(app_id or ""),
            MAtlasAlias.review_status == "approved",
        )
        .all()
    )
    out: dict[str, str] = {}
    for row in rows:
        key = str(row.alias_norm or "").strip()
        tid = str(row.target_id or "").strip()
        if key and tid:
            out[key] = tid
    return out


def rejected_pairs(db: Session, app_id: str) -> set[tuple[str, str]]:
    """(alias_norm, target_id)。驳回过的配对，模糊匹配不许再提。"""
    rows = (
        db.query(MAtlasAlias)
        .filter(
            MAtlasAlias.app_id == str(app_id or ""),
            MAtlasAlias.review_status == "rejected",
        )
        .all()
    )
    out: set[tuple[str, str]] = set()
    for row in rows:
        key = str(row.alias_norm or "").strip()
        tid = str(row.target_id or "").strip()
        if key and tid:
            out.add((key, tid))
    return out


def load_for_aligner(app_id: str) -> tuple[dict[str, str], set[tuple[str, str]]]:
    with session_scope() as db:
        return approved_map(db, app_id), rejected_pairs(db, app_id)


def upsert(
    db: Session,
    *,
    app_id: str,
    alias: str,
    target_id: str,
    target_kind: str = "module",
    target_path: list | None = None,
    source: str = "import",
    review_status: str = "pending",
    score: int = 0,
    note: str = "",
    bump_hit: bool = False,
) -> Optional[MAtlasAlias]:
    text = _clip_alias(alias)
    key = norm_name(text)
    tid = str(target_id or "").strip()
    if not key or not tid:
        return None
    row = (
        db.query(MAtlasAlias)
        .filter(MAtlasAlias.app_id == str(app_id or ""), MAtlasAlias.alias_norm == key)
        .first()
    )
    now = datetime.now()
    if row:
        # 已 approved 的别名被再次确认：只加 hits，不降级状态。
        # 已 rejected 的被再次提及时：按这次人的决定覆盖。
        row.alias = text
        row.target_id = tid
        row.target_kind = str(target_kind or "module")
        row.target_path = list(target_path or row.target_path or [])
        row.source = str(source or row.source or "import")
        row.score = int(score or row.score or 0)
        if note:
            row.note = str(note)[:512]
        if review_status:
            row.review_status = review_status
        if bump_hit and row.review_status == "approved":
            row.hits = int(row.hits or 0) + 1
        row.updated_at = now
        return row
    row = MAtlasAlias(
        app_id=str(app_id or ""),
        alias=text,
        alias_norm=key,
        target_id=tid,
        target_kind=str(target_kind or "module"),
        target_path=list(target_path or []),
        source=str(source or "import"),
        review_status=str(review_status or "pending"),
        hits=1 if bump_hit and review_status == "approved" else 0,
        score=int(score or 0),
        note=str(note or "")[:512],
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    return row


def apply_decisions(
    app_id: str,
    aliases: list[dict],
    *,
    review_status: str,
    source: str = "import",
) -> int:
    """批量写入人审结果。返回写入条数。"""
    if not aliases:
        return 0
    n = 0
    with session_scope() as db:
        for item in aliases:
            if not isinstance(item, dict):
                continue
            row = upsert(
                db,
                app_id=app_id,
                alias=str(item.get("alias") or item.get("text") or ""),
                target_id=str(item.get("target_id") or ""),
                target_kind=str(item.get("target_kind") or item.get("kind") or "module"),
                target_path=list(item.get("path") or item.get("target_path") or []),
                source=str(item.get("source") or source),
                review_status=review_status,
                score=int(item.get("score") or 0),
                note=str(item.get("note") or ""),
                bump_hit=(review_status == "approved"),
            )
            if row:
                n += 1
    return n


def record_hit(app_id: str, alias_norm: str) -> None:
    key = str(alias_norm or "").strip()
    if not key:
        return
    with session_scope() as db:
        row = (
            db.query(MAtlasAlias)
            .filter(
                MAtlasAlias.app_id == str(app_id or ""),
                MAtlasAlias.alias_norm == key,
                MAtlasAlias.review_status == "approved",
            )
            .first()
        )
        if row:
            row.hits = int(row.hits or 0) + 1
            row.updated_at = datetime.now()


def delete_alias(db: Session, app_id: str, alias_id: int) -> bool:
    row = (
        db.query(MAtlasAlias)
        .filter(MAtlasAlias.app_id == str(app_id or ""), MAtlasAlias.id == int(alias_id))
        .first()
    )
    if not row:
        return False
    db.delete(row)
    return True


def set_status(db: Session, app_id: str, alias_id: int, review_status: str) -> Optional[MAtlasAlias]:
    if review_status not in ("pending", "approved", "rejected"):
        return None
    row = (
        db.query(MAtlasAlias)
        .filter(MAtlasAlias.app_id == str(app_id or ""), MAtlasAlias.id == int(alias_id))
        .first()
    )
    if not row:
        return None
    row.review_status = review_status
    row.updated_at = datetime.now()
    return row


def to_public(row: MAtlasAlias) -> dict:
    return {
        "id": row.id,
        "app_id": row.app_id,
        "alias": row.alias,
        "alias_norm": row.alias_norm,
        "target_id": row.target_id,
        "target_kind": row.target_kind,
        "target_path": list(row.target_path or []),
        "source": row.source,
        "review_status": row.review_status,
        "hits": int(row.hits or 0),
        "score": int(row.score or 0),
        "note": row.note or "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


def list_aliases(db: Session, app_id: str, *, status: str = "") -> list[MAtlasAlias]:
    q = db.query(MAtlasAlias).filter(MAtlasAlias.app_id == str(app_id or ""))
    if status:
        q = q.filter(MAtlasAlias.review_status == status)
    return q.order_by(MAtlasAlias.hits.desc(), MAtlasAlias.updated_at.desc()).all()


__all__ = [
    "apply_decisions",
    "approved_map",
    "delete_alias",
    "list_aliases",
    "load_for_aligner",
    "record_hit",
    "rejected_pairs",
    "session_scope",
    "set_status",
    "to_public",
    "upsert",
]
