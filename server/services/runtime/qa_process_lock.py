# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""排期占用：跨应用扫描 qa_process.schedule，当前时刻落在窗口内则锁设备。

日历占用和「正在跑的任务」是两件事。busy_task_for_sn 只管 running；
这里管「这台机这会儿已经许给某条排期」。本窗口的主人（slot_id，或同
requirement/release + 对应 run_type）可以下发，别人 409。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple

from sqlalchemy.orm import Session

from server.models.project import App
from server.services.app_automation_service import get_automation_config

_RUN_TO_SLOT = {
    "req_test": "req_test",
    "req_admit": "req_admit",
    "release_regression": "rel_test",
    "release_smoke": "rel_online",
}


def _parse_iso(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iter_slots(db: Session) -> Iterator[Tuple[App, Dict[str, Any]]]:
    for app in db.query(App).all():
        cfg = get_automation_config(app)
        proc = cfg.get("qa_process") or {}
        for slot in proc.get("schedule") or []:
            if isinstance(slot, dict):
                yield app, slot


def _owner(
    hit: Dict[str, Any],
    *,
    slot_id: str = "",
    requirement_id: str = "",
    release_id: str = "",
    run_type: str = "",
) -> bool:
    if slot_id and str(hit.get("slot_id") or "") == str(slot_id):
        return True
    want_kind = _RUN_TO_SLOT.get(str(run_type or "").strip().lower(), "")
    if not want_kind or want_kind != str(hit.get("kind") or ""):
        return False
    if requirement_id and str(hit.get("requirement_id") or "") == str(requirement_id):
        return True
    if release_id and str(hit.get("release_id") or "") == str(release_id):
        return True
    return False


def reservations_by_sn(db: Session, now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """当前时刻每台设备被哪条排期占用。同一 SN 只保留最先扫到的一条。"""
    now = now or datetime.now(timezone.utc)
    out: Dict[str, Dict[str, Any]] = {}
    for app, slot in _iter_slots(db):
        start = _parse_iso(slot.get("start_at"))
        end = _parse_iso(slot.get("end_at")) or start
        sns = [str(x or "").strip() for x in (slot.get("sns") or []) if str(x or "").strip()]
        if not start or not end or not sns:
            continue
        if not (start <= now < end):
            continue
        info = {
            "slot_id": str(slot.get("id") or ""),
            "kind": str(slot.get("kind") or ""),
            "title": str(slot.get("title") or ""),
            "requirement_id": str(slot.get("requirement_id") or ""),
            "release_id": str(slot.get("release_id") or ""),
            "reserved_until": str(slot.get("end_at") or ""),
            "app_id": str(app.id or ""),
            "app_name": str(app.name or ""),
        }
        for sn in sns:
            out.setdefault(sn, info)
    return out


def blocking_reservation(
    db: Session,
    sns: Iterable[str],
    *,
    slot_id: str = "",
    requirement_id: str = "",
    release_id: str = "",
    run_type: str = "",
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    booked = reservations_by_sn(db, now)
    for raw in sns:
        sn = str(raw or "").strip()
        hit = booked.get(sn)
        if not hit:
            continue
        if _owner(
            hit,
            slot_id=slot_id,
            requirement_id=requirement_id,
            release_id=release_id,
            run_type=run_type,
        ):
            continue
        return {**hit, "sn": sn}
    return None
