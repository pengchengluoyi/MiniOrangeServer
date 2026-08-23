from __future__ import annotations

import json
import os
from typing import Any

from server.core.database import APP_DATA_DIR
from server.services.auth_service import require_session

_STORE = os.path.join(APP_DATA_DIR, "agent_sessions.json")
_MAX = 80


def _load() -> dict[str, Any]:
    if not os.path.exists(_STORE):
        return {}
    try:
        with open(_STORE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save(data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    tmp = f"{_STORE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _STORE)


def list_sessions(token: str) -> list[dict[str, Any]]:
    user = require_session(token)
    uid = str(user.get("user_id") or "")
    rows = _load().get(uid) or []
    return rows if isinstance(rows, list) else []


def save_sessions(token: str, sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    user = require_session(token)
    uid = str(user.get("user_id") or "")
    cleaned = [row for row in (sessions or []) if isinstance(row, dict) and row.get("id")]
    next_rows = cleaned[:_MAX]
    store = _load()
    store[uid] = next_rows
    _save(store)
    return next_rows
