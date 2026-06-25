# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
ClawNode 相关 HTTP 接口：
- 日志上传/下载（/logs）
- APK 上传/下载（/apks）：供前端通过 server 下发 INSTALL_APK 时托管本地/SMB APK
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from server.core.database import APP_DATA_DIR
from server.core.gateway_beacon import build_gateway_identity
from server.core.security import SecurityManager
from script.log import SLog

router = APIRouter(prefix="/api/clawnode", tags=["ClawNode"])
TAG = "ClawNode"


class ExecScriptRequest(BaseModel):
    sn: str = Field(..., description="ClawNode 设备 SN（claw- 开头）")
    script: str = Field("", description="内联 DSL JSON 或 JS 源码")
    script_id: str = Field("", description="预置脚本 ID（见 clawnode_script.list_script_ids）")
    language: str = Field("dsl", description="dsl | js")
    timeout_ms: int = Field(60_000, ge=1_000, le=300_000)
    script_vars: dict | None = Field(None, description="script_id 模板变量，如 package")


@router.get("/scripts")
def list_clawnode_scripts():
    from server.services.shared.clawnode_script import list_script_ids
    return {"code": 200, "data": list_script_ids()}


@router.post("/exec-script")
async def exec_script_on_device(body: ExecScriptRequest):
    """直接向在线 ClawNode 下发 EXEC_SCRIPT 并等待设备回传。"""
    sn = (body.sn or "").strip()
    if not sn:
        return {"code": 400, "msg": "missing sn"}
    if not (body.script or "").strip() and not (body.script_id or "").strip():
        return {"code": 400, "msg": "requires script or script_id"}

    from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
    from driver.tentacle.engine.mobile.mRemote import RemoteEngine

    try:
        engine, _ = bootstrap_mobile_engine(sn, "android", reuse=True)
    except Exception as e:
        return {"code": 500, "msg": f"bootstrap failed: {e}"}
    if not isinstance(engine, RemoteEngine):
        return {"code": 400, "msg": f"device {sn} is not ClawNode remote"}

    ok, stdout, stderr = engine.exec_script(
        body.script,
        script_id=body.script_id,
        language=body.language,
        timeout_ms=body.timeout_ms,
        script_vars=body.script_vars,
    )
    return {
        "code": 200 if ok else 500,
        "msg": "ok" if ok else (stderr or stdout or "exec failed"),
        "ok": ok,
        "stdout": stdout,
        "stderr": stderr,
    }


def _default_clawnode_logs_dir() -> Path:
    """默认放到用户 Downloads 目录下，避免污染项目源码目录。"""
    try:
        base = Path.home() / "Downloads"
        if not base.exists():
            base = Path.home()
        return base / "ClawNodeLogs"
    except Exception:
        return Path(APP_DATA_DIR) / "clawnode_logs"


def get_clawnode_log_dir() -> Path:
    """获取当前配置的 ClawNode 日志目录（会自动创建）。优先使用用户在系统设置里配置的路径。"""
    configured = SecurityManager.get_clawnode_logs_dir()
    if configured:
        d = Path(configured).expanduser().resolve()
    else:
        d = _default_clawnode_logs_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d

# 与 wsFile / main.py 共享的 uploads 目录，APK 也放在这里，便于 /static 也可用
_APK_DIR = Path(APP_DATA_DIR) / "uploads"
_APK_DIR.mkdir(parents=True, exist_ok=True)


def _download_prefix() -> str:
    prefix = SecurityManager.get_clawnode_log_prefix()
    return prefix.strip("/") or "download"


def _download_path(filename: str) -> str:
    return f"/api/clawnode/{_download_prefix()}/{filename}"


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
        log_dir = get_clawnode_log_dir()
        safe_sn = _safe_sn(sn)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"{safe_sn}_{ts}.txt"
        dest = log_dir / filename
        content = await log.read()
        dest.write_bytes(content)
        SLog.i(TAG, f"saved clawnode log sn={safe_sn} version={version} path={dest} bytes={len(content)}")
        return {
            "code": 200,
            "msg": "ok",
            "path": str(dest),
            "download_url": _download_path(filename),
            "storage_dir": str(log_dir),
        }
    except Exception as e:
        SLog.e(TAG, f"upload failed: {e}")
        return {"code": 500, "msg": str(e)}


def _list_clawnode_logs():
    try:
        log_dir = get_clawnode_log_dir()
        files = sorted(log_dir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        items = []
        for f in files[:200]:
            parts = f.stem.split("_", 1)
            items.append({
                "filename": f.name,
                "sn": parts[0] if parts else "unknown",
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
                "download_url": _download_path(f.name),
            })
        return {
            "code": 200,
            "data": items,
            "download_prefix": _download_prefix(),
            "storage_dir": str(log_dir),
        }
    except Exception as e:
        SLog.e(TAG, f"list failed: {e}")
        return {"code": 500, "msg": str(e), "data": []}


@router.get("/logs")
@router.get("/download")
def list_clawnode_logs():
    return _list_clawnode_logs()


@router.get("/logs/{filename}")
@router.get("/download/{filename}")
def download_clawnode_log(filename: str):
    safe = _safe_sn(filename.replace(".txt", ""))
    if not filename.endswith(".txt") or ".." in filename:
        return {"code": 400, "msg": "invalid filename"}
    log_dir = get_clawnode_log_dir()
    path = log_dir / filename
    if not path.is_file() or not str(path.resolve()).startswith(str(log_dir.resolve())):
        return {"code": 404, "msg": "not found"}
    return FileResponse(path, media_type="text/plain", filename=filename)


# ------------------------------
# APK 托管（供 ClawNode INSTALL_APK 使用）
# ------------------------------

def _safe_apk_name(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]", "_", (name or "install.apk").strip())
    if not cleaned.lower().endswith(".apk"):
        cleaned += ".apk"
    return cleaned[:128] or "install.apk"


@router.post("/apks")
async def upload_clawnode_apk(file: UploadFile = File(...)):
    """
    前端上传本地/SMB APK，返回 ClawNode 可下载的 HTTP URL。
    保存到共享 uploads 目录，同时支持 /static/xxx 和 /api/clawnode/apks/xxx 访问。
    """
    try:
        raw_name = file.filename or "install.apk"
        safe_name = _safe_apk_name(raw_name)
        dest = _APK_DIR / safe_name

        content = await file.read()
        dest.write_bytes(content)

        SLog.i(TAG, f"APK uploaded: {dest} size={len(content)}")

        # 关键：返回给设备下载时必须用 server 的局域网可达地址，而不是前端看到的 127.0.0.1
        identity = build_gateway_identity()
        lan_host = identity.get("local_ip") or "127.0.0.1"
        port = 10104
        device_base = f"http://{lan_host}:{port}"

        base_download = f"/api/clawnode/apks/{safe_name}"
        device_url = f"{device_base}{base_download}"

        return {
            "code": 200,
            "msg": "ok",
            "filename": safe_name,
            "url": base_download,                 # 相对路径，前端可用
            "download_url": base_download,
            "device_url": device_url,             # 推荐：设备可直接用的完整地址（LAN IP）
            "static_url": f"/static/{safe_name}",
            "lan_host": lan_host,
        }
    except Exception as e:
        SLog.e(TAG, f"APK upload failed: {e}")
        return {"code": 500, "msg": str(e)}


@router.get("/apks/{filename}")
def download_clawnode_apk(filename: str):
    """设备（ClawNode）通过此路径下载之前上传的 APK。"""
    if ".." in filename or "/" in filename:
        raise HTTPException(status_code=400, detail="invalid filename")
    # 优先从 uploads 找（ws 上传和本接口都落在这里）
    path = _APK_DIR / filename
    if not path.is_file():
        # 也兼容直接落在 uploads 根下的情况
        alt = Path(APP_DATA_DIR) / "uploads" / filename
        if alt.is_file():
            path = alt
    if not path.is_file():
        raise HTTPException(status_code=404, detail="apk not found")

    return FileResponse(
        path,
        media_type="application/vnd.android.package-archive",
        filename=filename,
    )
