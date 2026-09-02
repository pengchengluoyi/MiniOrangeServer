# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""plugins/resources/*.yaml —— 测试资源目录。不进业务看图菜单。"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from script.log import SLog

TAG = "ResourceCatalog"


def _resources_dir() -> Path:
    from server.services.plugins.loader import find_plugin_root

    return find_plugin_root() / "resources"


def list_resource_skills() -> list[dict[str, Any]]:
    root = _resources_dir()
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.iterdir()):
        name = path.name.lower()
        if not (name.endswith(".yaml") or name.endswith(".yml")):
            continue
        if name.endswith((".disabled", ".draft", ".bak")):
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            SLog.w(TAG, f"skip {path.name}: {exc}")
            continue
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        rows.append(
            {
                "id": str(raw.get("id") or ""),
                "name": str(raw.get("display_name") or raw.get("id") or ""),
                "description": str(raw.get("description") or "").strip(),
                "invoke": f"plugins/resources/{path.name} · caller={raw.get('caller') or 'system'}",
                "caller": str(raw.get("caller") or ""),
                "kind": "resource",
            }
        )
    return rows
