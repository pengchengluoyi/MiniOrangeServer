# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""微信 ClawBot（iLink）：扫码登录、长轮询收消息、回文本。"""
from __future__ import annotations

import base64
import json
import os
import random
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urljoin

import requests

from server.core.database import APP_DATA_DIR
from script.log import SLog

TAG = "wechat_ilink"
LOGIN_BASE = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.4.6"
BOT_AGENT = "MiniOrange/1.0.0"
ACCOUNT_PATH = os.path.join(APP_DATA_DIR, "wechat_ilink.json")
TOKEN_EXPIRED = -14

_lock = threading.RLock()
_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_login: Dict[str, Any] = {}
_state: Dict[str, Any] = {
    "running": False,
    "connected": False,
    "wanted": False,
    "error": "",
    "detail": "",
    "started_at": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _client_version() -> str:
    parts = [int(p) if p.isdigit() else 0 for p in CHANNEL_VERSION.split(".")[:3]]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts
    return str(((major & 0xFF) << 16) | ((minor & 0xFF) << 8) | (patch & 0xFF))


def _wechat_uin() -> str:
    return base64.b64encode(str(random.randint(0, 0xFFFFFFFF)).encode("utf-8")).decode("ascii")


def _headers(token: str = "") -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": _wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": _client_version(),
        "SKRouteTag": "1001",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _base_info() -> Dict[str, str]:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def _load_account() -> Dict[str, Any]:
    if not os.path.exists(ACCOUNT_PATH):
        return {}
    try:
        with open(ACCOUNT_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_account(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(ACCOUNT_PATH) or ".", exist_ok=True)
    tmp = f"{ACCOUNT_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, ACCOUNT_PATH)


def _clear_account() -> None:
    try:
        if os.path.exists(ACCOUNT_PATH):
            os.remove(ACCOUNT_PATH)
    except Exception:
        pass


def is_logged_in() -> bool:
    return bool(str(_load_account().get("bot_token") or "").strip())


def public_account() -> Dict[str, Any]:
    acc = _load_account()
    baseurl = str(acc.get("baseurl") or "").strip()
    host = ""
    if baseurl:
        host = baseurl.replace("https://", "").replace("http://", "").split("/")[0]
    return {
        "logged_in": bool(str(acc.get("bot_token") or "").strip()),
        "ilink_user_id": str(acc.get("ilink_user_id") or ""),
        "ilink_bot_id": str(acc.get("ilink_bot_id") or ""),
        "baseurl_host": host,
        "logged_in_at": str(acc.get("logged_in_at") or ""),
    }


def listener_status() -> Dict[str, Any]:
    with _lock:
        out = dict(_state)
    out["logged_in"] = is_logged_in()
    return out


def _api_base() -> str:
    return str(_load_account().get("baseurl") or "").strip().rstrip("/") or LOGIN_BASE


def _request(
    method: str,
    url: str,
    *,
    token: str = "",
    json_body: Optional[Dict[str, Any]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    resp = requests.request(
        method,
        url,
        headers=_headers(token),
        json=json_body if json_body is not None else None,
        timeout=timeout,
    )
    text = (resp.text or "").strip()
    try:
        data = resp.json() if text else {}
    except Exception:
        data = {"raw": text[:300], "http_status": resp.status_code}
    if not isinstance(data, dict):
        data = {"raw": text[:300], "http_status": resp.status_code}
    if resp.status_code >= 400 and "ret" not in data and "errcode" not in data:
        raise RuntimeError(f"微信接口 HTTP {resp.status_code}")
    return data


def _as_image_src(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://", "data:")):
        return text
    if text.startswith("/"):
        return urljoin(LOGIN_BASE + "/", text.lstrip("/"))
    compact = "".join(text.split())
    if compact.startswith("/9j/") or compact.startswith("iVBORw0K") or len(compact) > 80:
        mime = "image/jpeg" if compact.startswith("/9j/") else "image/png"
        return f"data:{mime};base64,{compact}"
    return text


def _set_login(**kwargs: Any) -> None:
    with _lock:
        _login.update(kwargs)


def _set_state(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)


def _login_snapshot() -> Dict[str, Any]:
    with _lock:
        row = dict(_login)
    status = str(row.get("status") or "")
    return {
        **public_account(),
        "status": status or ("confirmed" if is_logged_in() else "idle"),
        "qrcode_img": _as_image_src(str(row.get("qrcode_img") or "")),
        "need_verify": status in ("need_verifycode", "need_verify"),
        "error": str(row.get("error") or ""),
        "listener": listener_status(),
    }


def _unwrap_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    inner = data.get("data")
    if isinstance(inner, dict):
        merged = dict(inner)
        for key in ("ret", "errcode", "errmsg", "error"):
            if key in data and key not in merged:
                merged[key] = data.get(key)
        return merged
    return data


def _extract_qr(data: Dict[str, Any]) -> tuple[str, str]:
    row = _unwrap_payload(data)
    qrcode = str(
        row.get("qrcode")
        or row.get("qrcode_id")
        or row.get("qrcode_str")
        or ""
    ).strip()
    img = str(
        row.get("qrcode_img_content")
        or row.get("qrcode_img")
        or row.get("qrcode_url")
        or row.get("qrcode_img_url")
        or row.get("qrcode_img_base64")
        or ""
    ).strip()
    if not img and qrcode.startswith(("http://", "https://", "data:")):
        img = qrcode
    return qrcode, img


def start_qr_login() -> Dict[str, Any]:
    _set_login(status="wait", qrcode="", qrcode_img="", error="", started_at=_now())
    url = f"{LOGIN_BASE}/ilink/bot/get_bot_qrcode?bot_type=3"
    last_error = ""
    data: Dict[str, Any] = {}
    for method, body in (("GET", None), ("POST", {"local_token_list": []})):
        try:
            data = _request(method, url, json_body=body, timeout=12)
            last_error = str(data.get("errmsg") or data.get("error") or "")
            qrcode, img = _extract_qr(data)
            if qrcode or img:
                _set_login(status="wait", qrcode=qrcode, qrcode_img=img, error="")
                SLog.i(TAG, f"wechat qr login started keys={list(_unwrap_payload(data).keys())[:12]}")
                return _login_snapshot()
        except Exception as e:
            last_error = str(e)
            SLog.w(TAG, f"get bot qrcode {method} failed: {e}")
    raise RuntimeError(last_error or "没有拿到微信登录二维码")


def _apply_confirmed(data: Dict[str, Any]) -> None:
    token = str(data.get("bot_token") or data.get("token") or "").strip()
    if not token:
        raise RuntimeError("扫码成功但没有 bot_token")
    _save_account(
        {
            "bot_token": token,
            "ilink_bot_id": str(data.get("ilink_bot_id") or ""),
            "ilink_user_id": str(data.get("ilink_user_id") or ""),
            "baseurl": str(data.get("baseurl") or LOGIN_BASE).rstrip("/"),
            "logged_in_at": _now(),
        }
    )
    _set_login(status="confirmed", qrcode="", qrcode_img="", error="")
    SLog.i(TAG, "wechat qr login confirmed")
    sync_wechat_listener()


def poll_qr_login(*, verify_code: str = "") -> Dict[str, Any]:
    with _lock:
        qrcode = str(_login.get("qrcode") or "").strip()
        status = str(_login.get("status") or "")
    if not qrcode or status == "confirmed":
        return _login_snapshot()
    query = f"qrcode={quote(qrcode, safe='')}"
    code = str(verify_code or "").strip()
    if code:
        query += f"&verify_code={quote(code, safe='')}"
    url = f"{LOGIN_BASE}/ilink/bot/get_qrcode_status?{query}"
    data = _request("GET", url, timeout=25)
    st = str(data.get("status") or "").strip() or "wait"
    if st in ("scaned_but_redirect", "binded_redirect"):
        _set_login(status="wait")
        return _login_snapshot()
    if st in ("confirmed", "confirm", "success"):
        _apply_confirmed(data)
        return _login_snapshot()
    if st in ("expired", "expire", "timeout"):
        _set_login(status="expired", error="二维码过期了，重新扫一次")
        return _login_snapshot()
    img = str(data.get("qrcode_img_content") or data.get("qrcode_img") or "").strip()
    extra: Dict[str, Any] = {"status": st, "error": ""}
    if img:
        extra["qrcode_img"] = img
    _set_login(**extra)
    return _login_snapshot()


def login_status() -> Dict[str, Any]:
    with _lock:
        qrcode = str(_login.get("qrcode") or "").strip()
        status = str(_login.get("status") or "")
    if qrcode and status in ("wait", "scaned", "scanned", "need_verifycode", "need_verify"):
        try:
            return poll_qr_login()
        except Exception as e:
            _set_login(error=str(e))
            SLog.w(TAG, f"poll qr failed: {e}")
    return _login_snapshot()


def verify_qr_login(verify_code: str) -> Dict[str, Any]:
    code = str(verify_code or "").strip()
    if not code:
        raise ValueError("请填写微信里显示的配对码")
    return poll_qr_login(verify_code=code)


def _post_cgi(path: str, body: Dict[str, Any], *, timeout: float = 20) -> Dict[str, Any]:
    acc = _load_account()
    token = str(acc.get("bot_token") or "").strip()
    if not token:
        raise RuntimeError("还没登录微信")
    payload = dict(body)
    payload.setdefault("base_info", _base_info())
    url = f"{_api_base()}/{path.lstrip('/')}"
    data = _request("POST", url, token=token, json_body=payload, timeout=timeout)
    err = data.get("errcode", data.get("ret"))
    if err == TOKEN_EXPIRED:
        _clear_account()
        _set_state(connected=False, error="微信登录过期，请重新扫码")
        raise RuntimeError("微信登录过期，请重新扫码")
    return data


def send_wechat_text(*, to_user_id: str, context_token: str, text: str) -> None:
    body = str(text or "").strip()
    user_id = str(to_user_id or "").strip()
    token = str(context_token or "").strip()
    if not body or not user_id or not token:
        return
    ticket = ""
    try:
        cfg = _post_cgi(
            "ilink/bot/getconfig",
            {"ilink_user_id": user_id, "context_token": token},
            timeout=10,
        )
        ticket = str(cfg.get("typing_ticket") or "").strip()
        if ticket:
            _post_cgi(
                "ilink/bot/sendtyping",
                {"ilink_user_id": user_id, "typing_ticket": ticket, "status": 1},
                timeout=8,
            )
    except Exception as e:
        SLog.w(TAG, f"wechat typing failed: {e}")
    _post_cgi(
        "ilink/bot/sendmessage",
        {
            "msg": {
                "to_user_id": user_id,
                "from_user_id": "",
                "client_id": uuid.uuid4().hex,
                "message_type": 2,
                "message_state": 2,
                "context_token": token,
                "item_list": [{"type": 1, "text_item": {"text": body}}],
            }
        },
        timeout=20,
    )
    if ticket:
        try:
            _post_cgi(
                "ilink/bot/sendtyping",
                {"ilink_user_id": user_id, "typing_ticket": ticket, "status": 2},
                timeout=8,
            )
        except Exception:
            pass


def logout_wechat() -> Dict[str, Any]:
    if str(_load_account().get("bot_token") or "").strip():
        try:
            _post_cgi("ilink/bot/msg/notifystop", {}, timeout=8)
        except Exception:
            pass
    _clear_account()
    _set_login(status="idle", qrcode="", qrcode_img="", error="")
    stop_wechat_listener()
    return login_status()


def _item_text(item: Dict[str, Any]) -> str:
    if not isinstance(item, dict):
        return ""
    kind = int(item.get("type") or 0)
    if kind == 1:
        row = item.get("text_item") if isinstance(item.get("text_item"), dict) else {}
        return str(row.get("text") or "").strip()
    if kind == 3:
        row = item.get("voice_item") if isinstance(item.get("voice_item"), dict) else {}
        return str(row.get("text") or "").strip()
    text_item = item.get("text_item") if isinstance(item.get("text_item"), dict) else {}
    return str(text_item.get("text") or "").strip()


def _message_text(msg: Dict[str, Any]) -> str:
    items = msg.get("item_list") if isinstance(msg.get("item_list"), list) else []
    parts = [_item_text(item) for item in items if isinstance(item, dict)]
    return "\n".join(part for part in parts if part).strip()


def _handle_message(msg: Dict[str, Any]) -> None:
    from server.services.im_bot_service import append_im_history, load_im_history, record_im_inbound, reply_im_message

    if int(msg.get("message_type") or 0) == 2:
        return
    text = _message_text(msg)
    user_id = str(msg.get("from_user_id") or "").strip() or "unknown"
    context_token = str(msg.get("context_token") or "").strip()
    if not text or not context_token:
        record_im_inbound({"kind": "ignore", "result": {"ignored": "empty" if not text else "no_context"}})
        return
    history = load_im_history("wechat", user_id, user_id)
    try:
        result = reply_im_message(
            text=text,
            history=history,
            plugin_id="wechat",
            require_enabled=True,
            source="wechat_im",
        )
    except Exception as e:
        SLog.w(TAG, f"im reply failed: {e}")
        result = {"ok": False, "reply": str(e) or "我这边暂时答不上来。"}
    reply = str(result.get("reply") or "").strip()
    append_im_history("wechat", user_id, user_id, "user", text)
    if reply:
        append_im_history("wechat", user_id, user_id, "assistant", reply)
        try:
            send_wechat_text(to_user_id=user_id, context_token=context_token, text=reply)
        except Exception as e:
            SLog.w(TAG, f"wechat send failed: {e}")
    record_im_inbound(
        {
            "kind": "message",
            "result": {
                "queued": False,
                "text": text[:80],
                "mode": result.get("mode"),
                "replied": bool(reply),
            },
        }
    )


def _poll_loop() -> None:
    cursor = str(_load_account().get("get_updates_buf") or "")
    _set_state(running=True, connected=False, error="", detail="正在连接微信…")
    try:
        _post_cgi("ilink/bot/msg/notifystart", {}, timeout=10)
    except Exception as e:
        SLog.w(TAG, f"wechat notifystart: {e}")
    while not _stop.is_set():
        try:
            data = _post_cgi("ilink/bot/getupdates", {"get_updates_buf": cursor}, timeout=40)
            cursor = str(data.get("get_updates_buf") or cursor)
            acc = _load_account()
            if acc:
                acc["get_updates_buf"] = cursor
                _save_account(acc)
            _set_state(running=True, connected=True, error="", detail="已连接")
            msgs: List[Dict[str, Any]] = [m for m in (data.get("msgs") or []) if isinstance(m, dict)]
            for msg in msgs:
                if _stop.is_set():
                    break
                try:
                    _handle_message(msg)
                except Exception as e:
                    SLog.w(TAG, f"handle wechat msg: {e}")
        except RuntimeError as e:
            _set_state(connected=False, error=str(e))
            SLog.w(TAG, f"wechat poll: {e}")
            if "过期" in str(e):
                break
            _stop.wait(3)
        except requests.Timeout:
            continue
        except Exception as e:
            _set_state(connected=False, error=str(e)[:200])
            SLog.w(TAG, f"wechat poll failed: {e}")
            _stop.wait(3)
    _set_state(running=False, connected=False)


def stop_wechat_listener() -> None:
    global _thread
    _stop.set()
    _set_state(wanted=False, running=False, connected=False)
    thread = _thread
    _thread = None
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=1.5)


def sync_wechat_listener() -> Dict[str, Any]:
    from server.services.im_bot_service import get_im_chat_config

    cfg = get_im_chat_config("wechat")
    if not cfg.get("enabled"):
        stop_wechat_listener()
        _set_state(error="")
        return listener_status()
    if not is_logged_in():
        stop_wechat_listener()
        _set_state(error="先在「连接」里用微信扫码")
        return listener_status()

    global _thread
    with _lock:
        alive = _thread is not None and _thread.is_alive()
        if alive:
            _state["wanted"] = True
            return listener_status()

    stop_wechat_listener()
    _stop.clear()
    thread = threading.Thread(target=_poll_loop, name="wechat-ilink", daemon=True)
    with _lock:
        _thread = thread
        _state.update(
            {
                "wanted": True,
                "running": True,
                "connected": False,
                "error": "",
                "started_at": _now(),
                "detail": "正在连接微信…",
            }
        )
    thread.start()
    return listener_status()
