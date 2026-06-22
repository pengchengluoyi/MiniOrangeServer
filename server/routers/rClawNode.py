# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""ClawNode 设备日志上传接口。"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse

from script.log import SLog

router = APIRouter(prefix="/api/clawnode", tags=["ClawNode"])
TAG = "ClawNodeLogs"

_LOG_DIR = Path(__file__).resolve().parents[2] / "logs" / "clawnode"


def _safe_sn(sn: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", (sn or "unknown").strip())
    return cleaned[:64] or "unknown"


@router.post("/logs")
async def upload_clawnode_log(
    sn: str = Form(""),
    version: str = Form(""),
    log: UploadFile = File(...),
):
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        safe_sn = _safe_sn(sn)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"{safe_sn}_{ts}.txt"
        dest = _LOG_DIR / filename
        content = await log.read()
        dest.write_bytes(content)
        SLog.i(TAG, f"saved clawnode log sn={safe_sn} version={version} path={dest} bytes={len(content)}")
        return {"code": 200, "msg": "ok", "path": str(dest)}
    except Exception as e:
        SLog.e(TAG, f"upload failed: {e}")
        return {"code": 500, "msg": str(e)}


@router.get("/logs")
def list_clawnode_logs():
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        files = sorted(_LOG_DIR.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        items = []
        for f in files[:200]:
            parts = f.stem.split("_", 1)
            items.append({
                "filename": f.name,
                "sn": parts[0] if parts else "unknown",
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
            })
        return {"code": 200, "data": items}
    except Exception as e:
        SLog.e(TAG, f"list failed: {e}")
        return {"code": 500, "msg": str(e), "data": []}


@router.get("/logs/{filename}")
def download_clawnode_log(filename: str):
    safe = _safe_sn(filename.replace(".txt", ""))
    if not filename.endswith(".txt") or ".." in filename:
        return {"code": 400, "msg": "invalid filename"}
    path = _LOG_DIR / filename
    if not path.is_file() or not str(path.resolve()).startswith(str(_LOG_DIR.resolve())):
        return {"code": 404, "msg": "not found"}
    return FileResponse(path, media_type="text/plain", filename=filename)
