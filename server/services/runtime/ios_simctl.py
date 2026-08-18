# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""用 simctl 列出已启动的 iOS 模拟器（新建执行的在线目标）。"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import List

from script.log import SLog

TAG = "IosSimctl"


def list_booted_simulators() -> List[dict]:
    """返回 [{udid, name, os}]，仅 Booted 且 available。"""
    if not shutil.which("xcrun"):
        return []
    try:
        r = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "booted", "-j"],
            capture_output=True,
            text=True,
            timeout=20,
            errors="ignore",
        )
    except Exception as e:
        SLog.d(TAG, f"simctl list failed: {e}")
        return []
    if r.returncode != 0 or not (r.stdout or "").strip():
        return []
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        SLog.d(TAG, f"simctl JSON parse error: {e}")
        return []

    out: List[dict] = []
    for runtime, entries in (payload.get("devices") or {}).items():
        os_ver = str(runtime).rsplit(".", 1)[-1].replace("iOS-", "").replace("-", ".")
        for entry in entries or []:
            if entry.get("state") != "Booted":
                continue
            udid = str(entry.get("udid") or "").strip()
            if not udid:
                continue
            out.append(
                {
                    "udid": udid,
                    "name": entry.get("name") or "Simulator",
                    "os": os_ver,
                }
            )
    return out
