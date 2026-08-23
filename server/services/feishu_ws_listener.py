# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""飞书长连接：独立子进程连开放平台，不占用 FastAPI 事件循环。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from script.log import SLog

TAG = "feishu_ws"

_lock = threading.RLock()
_proc: Optional[subprocess.Popen] = None
_reader: Optional[threading.Thread] = None
_wanted_key = ""
_state: Dict[str, Any] = {
    "running": False,
    "connected": False,
    "wanted": False,
    "error": "",
    "app_id": "",
    "started_at": "",
    "detail": "",
}


def feishu_ws_status() -> Dict[str, Any]:
    with _lock:
        out = dict(_state)
        proc = _proc
    out["running"] = bool(proc is not None and proc.poll() is None)
    if out["connected"] and out["running"]:
        out["error"] = ""
    return out


def _set_state(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)


def _handle_line(line: str) -> None:
    line = (line or "").strip()
    if not line:
        return
    try:
        msg = json.loads(line)
    except Exception:
        if "connected to" in line:
            _set_state(connected=True, running=True, error="", detail="connected")
            SLog.i(TAG, "feishu ws connected")
        elif "connect failed" in line or "CERTIFICATE_VERIFY_FAILED" in line:
            _set_state(connected=False, error=line[:200])
            SLog.w(TAG, line[:200])
        return
    kind = str(msg.get("type") or "")
    if kind == "starting":
        _set_state(running=True, detail="正在向飞书申请长连接…")
        return
    if kind == "error":
        _set_state(connected=False, error=str(msg.get("error") or "长连接失败")[:240])
        SLog.w(TAG, msg.get("error"))
        return
    if kind == "event":
        from server.services.im_bot_service import accept_feishu_event, record_im_inbound

        payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else {}
        if payload.get("event") or payload.get("header"):
            body = payload
        else:
            body = {
                "schema": "2.0",
                "header": {"event_type": "im.message.receive_v1"},
                "event": payload,
            }
        _set_state(connected=True, running=True, error="")
        record_im_inbound({"source": "ws", "received": True})
        try:
            result = accept_feishu_event(body)
        except Exception as e:
            SLog.w(TAG, f"ws event failed: {e}")
            record_im_inbound({"source": "ws", "error": str(e)})
            return
        record_im_inbound({"source": "ws", "result": result})
        SLog.i(TAG, f"ws event {result}")


def _read_proc(proc: subprocess.Popen) -> None:
    try:
        assert proc.stdout is not None
        for raw in proc.stdout:
            _handle_line(raw)
    except Exception as e:
        SLog.w(TAG, f"ws reader stopped: {e}")
    code = proc.poll()
    if code not in (None, 0):
        _set_state(running=False, connected=False, error=_state.get("error") or f"长连接进程退出 {code}")
    else:
        _set_state(running=False, connected=False)


def stop_feishu_event_listener() -> None:
    global _proc, _wanted_key
    with _lock:
        proc = _proc
        _proc = None
        _wanted_key = ""
        _state["wanted"] = False
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    _set_state(running=False, connected=False)


def sync_feishu_event_listener() -> Dict[str, Any]:
    from server.services.im_bot_service import get_im_chat_config
    from server.services.system_settings_service import get_feishu_credentials

    cfg = get_im_chat_config("feishu")
    if not cfg.get("enabled"):
        stop_feishu_event_listener()
        _set_state(error="")
        return feishu_ws_status()
    try:
        app_id, app_secret = get_feishu_credentials()
    except Exception as e:
        stop_feishu_event_listener()
        _set_state(error=str(e) or "读不到飞书凭证")
        return feishu_ws_status()
    if not (app_id or "").strip() or not (app_secret or "").strip():
        stop_feishu_event_listener()
        _set_state(error="先在「连接」里填 App ID 和 App Secret")
        return feishu_ws_status()

    key = f"{app_id}:{app_secret}"
    global _proc, _reader, _wanted_key
    status = feishu_ws_status()
    with _lock:
        alive = _proc is not None and _proc.poll() is None and _wanted_key == key
        if alive and (status.get("connected") or (status.get("running") and not status.get("error"))):
            _state["wanted"] = True
            return feishu_ws_status()

    stop_feishu_event_listener()
    env = os.environ.copy()
    env["MO_FEISHU_APP_ID"] = app_id
    env["MO_FEISHU_APP_SECRET"] = app_secret
    env.pop("MO_FEISHU_WS_INSECURE", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "server.services.feishu_ws_worker"],
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    reader = threading.Thread(target=_read_proc, args=(proc,), daemon=True, name="feishu-ws-reader")
    with _lock:
        _proc = proc
        _reader = reader
        _wanted_key = key
        _state.update(
            {
                "wanted": True,
                "running": True,
                "connected": False,
                "error": "",
                "app_id": app_id,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "detail": "正在向飞书申请长连接…",
            }
        )
    reader.start()
    return feishu_ws_status()
