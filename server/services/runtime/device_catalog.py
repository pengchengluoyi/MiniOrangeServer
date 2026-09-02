# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""当前环境设备目录：在线 / 占用 / 排期，给选设备技能和 GET /devices 共用。"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from script.log import SLog

TAG = "DeviceCatalog"


def list_device_catalog(db: Optional[Session], *, only_online: bool = True) -> list[dict[str, Any]]:
    """MDevice 列表 + 本机 Web 槽。busy_task_id / reserved_slot_id 非空表示此刻不能新占。"""
    from server.services.regression import task_store
    from server.services.runtime.qa_process_lock import reservations_by_sn

    busy = task_store.busy_map()
    reserved = reservations_by_sn(db) if db is not None else {}
    items: list[dict[str, Any]] = []
    try:
        if db is not None:
            from server.models.mDevice import MDevice
            from server.services.runtime.channels import channels_to_brief

            q = db.query(MDevice)
            if only_online:
                q = q.filter(MDevice.status == "online")
            for d in q.order_by(MDevice.sn).all():
                hit = reserved.get(d.sn) or {}
                items.append({
                    "sn": d.sn,
                    "model": d.model or "",
                    "device_type": d.device_type or "",
                    "type": d.device_type or "",
                    "os_version": d.os_version or "",
                    "resolution": d.resolution or "",
                    "role": d.role or "",
                    "status": d.status or "offline",
                    "channels": channels_to_brief(d.channels or {}),
                    "busy_task_id": busy.get(d.sn, ""),
                    "reserved_slot_id": hit.get("slot_id") or "",
                    "reserved_title": hit.get("title") or "",
                    "reserved_until": hit.get("reserved_until") or "",
                    "reserved_kind": hit.get("kind") or "",
                    "reserved_app_id": hit.get("app_id") or "",
                })
    except Exception as exc:
        SLog.w(TAG, f"list devices failed: {exc}")
    from server.services.runtime.playwright_hub import WEB_SLOT_SN, is_web_slot, probe_playwright
    from server.services.runtime.run_context import device_platform_kind

    if not any(is_web_slot(str(it.get("sn") or "")) for it in items):
        state, meta = probe_playwright()
        available = state in ("connected", "available")
        if available or not only_online:
            hit = reserved.get(WEB_SLOT_SN) or {}
            items.insert(0, {
                "sn": WEB_SLOT_SN,
                "model": "本机浏览器",
                "device_type": "web",
                "type": "web",
                "os_version": "",
                "resolution": "1280x800",
                "role": "",
                "status": "online" if available else "offline",
                "channels": {
                    "playwright_state": state,
                    "playwright_reason": (meta or {}).get("reason") or "",
                    "remote_state": "not_applicable",
                    "adb_state": "not_applicable",
                    "ios_state": "not_applicable",
                },
                "busy_task_id": busy.get(WEB_SLOT_SN, ""),
                "reserved_slot_id": hit.get("slot_id") or "",
                "reserved_title": hit.get("title") or "",
                "reserved_until": hit.get("reserved_until") or "",
                "reserved_kind": hit.get("kind") or "",
                "reserved_app_id": hit.get("app_id") or "",
            })
    for it in items:
        it["platform"] = device_platform_kind(
            str(it.get("device_type") or it.get("type") or ""),
            it.get("channels"),
            sn=str(it.get("sn") or ""),
        )
    return items
