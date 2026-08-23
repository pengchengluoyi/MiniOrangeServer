# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""IM 机器人对话：两套 prompt、会话、飞书收发。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from server.core.database import APP_DATA_DIR
from server.services.im_prompts import DEFAULT_IM_DEFECT_PROMPT, DEFAULT_IM_DIALOGUE_PROMPT
from script.log import SLog

TAG = "im_bot"
IM_PLUGIN_IDS = ("feishu", "wecom", "dingtalk", "slack", "wechat")
IM_PLUGIN_IDS = IM_PLUGIN_IDS
_PLUGIN_LABEL = {
    "feishu": "飞书",
    "wecom": "企业微信",
    "dingtalk": "钉钉",
    "slack": "Slack",
    "wechat": "微信",
}
_STORE = os.path.join(APP_DATA_DIR, "im_bot_sessions.json")
_INBOUND = os.path.join(APP_DATA_DIR, "im_bot_inbound.json")
_MAX_HISTORY = 12
_DEFECT_HINTS = (
    "提缺陷",
    "提交缺陷",
    "提单",
    "报bug",
    "报 bug",
    "报缺陷",
    "建缺陷",
    "创建bug",
    "创建 bug",
    "提个bug",
    "提个 bug",
    "提bug",
    "提 bug",
)


def default_im_chat() -> Dict[str, Any]:
    return {"enabled": False}


def normalize_im_chat(raw: Any = None) -> Dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    return {"enabled": bool(src.get("enabled"))}


def _role_prompt(role_id: str, fallback: str) -> str:
    from server.services.ai.roles_catalog import get_role

    role = get_role(role_id) or {}
    return str(role.get("system_prompt") or role.get("system_prompt") or "").strip() or fallback


def get_im_chat_config(plugin_id: str = "feishu") -> Dict[str, Any]:
    from server.services.ai.role_plugin_graph import im_roles_for_plugin, resolve_im
    from server.services.system_settings_service import _merged_plugin_config

    pid = plugin_id if plugin_id in IM_PLUGIN_IDS else "feishu"
    cfg = _merged_plugin_config(pid)
    roles = im_roles_for_plugin(pid)
    defect = resolve_im(plugin_id=pid, intent="defect")
    return {
        **normalize_im_chat(cfg.get("chat")),
        "dialogue_role": roles["dialogue"],
        "defect_role": roles["defect"],
        "dialogue_prompt": _role_prompt(roles["dialogue"], DEFAULT_IM_DIALOGUE_PROMPT),
        "defect_prompt": _role_prompt(roles["defect"], DEFAULT_IM_DEFECT_PROMPT),
        "submit_plugin_id": defect.get("submit_plugin_id") or "",
    }


def record_im_inbound(info: Dict[str, Any]) -> None:
    current = get_im_inbound()
    current.update(info if isinstance(info, dict) else {})
    current["at"] = datetime.now(timezone.utc).isoformat()
    os.makedirs(os.path.dirname(_INBOUND), exist_ok=True)
    tmp = f"{_INBOUND}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _INBOUND)


def get_im_inbound() -> Dict[str, Any]:
    if not os.path.exists(_INBOUND):
        return {}
    try:
        with open(_INBOUND, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def detect_im_intent(text: str) -> str:
    raw = str(text or "").strip().lower()
    if any(hint in raw for hint in _DEFECT_HINTS):
        return "defect"
    return "dialogue"


def _load_sessions() -> Dict[str, Any]:
    if not os.path.exists(_STORE):
        return {}
    try:
        with open(_STORE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_sessions(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    tmp = f"{_STORE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, _STORE)


def _session_key(platform: str, chat_id: str, user_id: str) -> str:
    return f"{platform}:{chat_id}:{user_id}"


def load_im_history(platform: str, chat_id: str, user_id: str) -> List[Dict[str, str]]:
    rows = _load_sessions().get(_session_key(platform, chat_id, user_id)) or []
    return [row for row in rows if isinstance(row, dict) and row.get("role") and row.get("content")]


def append_im_history(platform: str, chat_id: str, user_id: str, role: str, content: str) -> None:
    key = _session_key(platform, chat_id, user_id)
    store = _load_sessions()
    rows = [row for row in (store.get(key) or []) if isinstance(row, dict)]
    rows.append({"role": role, "content": str(content or "")[:4000]})
    store[key] = rows[-_MAX_HISTORY:]
    _save_sessions(store)


def _call_llm(system: str, history: List[Dict[str, str]], user_text: str, *, conversational: bool) -> str:
    from server.services.ai.regression.llm_client import call_chat_plain, resolve_regression_provider

    provider, gate = resolve_regression_provider()
    if not provider:
        raise RuntimeError(gate.get("reason") or "未配置可用的大模型")
    messages = [{"role": "system", "content": system}]
    for row in history[-_MAX_HISTORY:]:
        role = row.get("role")
        content = str(row.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content[:4000]})
    messages.append({"role": "user", "content": user_text})
    reply, meta = call_chat_plain(
        provider=provider,
        messages=messages,
        temperature=0.4 if conversational else 0.15,
        max_tokens=2048,
        timeout_sec=90,
    )
    if not reply:
        raise RuntimeError(meta.get("error") or "模型没有返回内容")
    return reply.strip()


def _parse_json_object(text: str) -> Dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _match_zentao_project(name: str) -> Dict[str, str]:
    from server.services.system_settings_service import _merged_plugin_config

    want = str(name or "").strip().lower()
    cfg = _merged_plugin_config("zentao")
    for row in cfg.get("bindings") or []:
        if not isinstance(row, dict):
            continue
        labels = [
            str(row.get("project_name") or ""),
            str(row.get("product_name") or ""),
            str(row.get("project_id") or ""),
        ]
        if want and any(want == item.strip().lower() or want in item.strip().lower() for item in labels if item.strip()):
            return {
                "project_id": str(row.get("project_id") or ""),
                "product_id": str(row.get("product_id") or ""),
            }
    rows = [row for row in (cfg.get("bindings") or []) if isinstance(row, dict) and row.get("product_id")]
    if len(rows) == 1:
        return {
            "project_id": str(rows[0].get("project_id") or ""),
            "product_id": str(rows[0].get("product_id") or ""),
        }
    return {}


def _submit_defect(draft: Dict[str, Any], plugin_id: str) -> Dict[str, Any]:
    pid = str(plugin_id or "").strip()
    if pid != "zentao":
        return {"ok": False, "error": "这条角色还没有绑定可提单的插件。"}
    return _submit_zentao_from_draft(draft)


def _submit_zentao_from_draft(draft: Dict[str, Any]) -> Dict[str, Any]:
    from server.services.system_settings_service import create_zentao_bug, get_zentao_credentials

    creds = get_zentao_credentials()
    if not creds.get("url") or not creds.get("token"):
        return {"ok": False, "error": "禅道还没连上。先到设置 → 插件 → 禅道换 Token。"}
    matched = _match_zentao_project(str(draft.get("project") or ""))
    if not matched.get("product_id"):
        return {"ok": False, "error": "对不上禅道产品。先在禅道插件「产品绑定」里绑项目，或在对话里写出项目名。"}
    title = str(draft.get("title") or "").strip() or "未命名缺陷"
    try:
        info = create_zentao_bug(
            project_id=matched.get("project_id") or "",
            product_id=matched.get("product_id") or "",
            context={
                "title": title,
                "project": str(draft.get("project") or "") or "IM",
                "app": "",
                "version": "",
                "module": "",
                "case": "",
                "env": "IM 机器人",
                "steps": str(draft.get("steps") or "").strip(),
                "expected": str(draft.get("expected") or "").strip(),
                "actual": str(draft.get("actual") or "").strip(),
                "run": "IM 提缺陷",
            },
        )
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, **info}


def reply_im_message(
    *,
    text: str,
    history: Optional[List[Dict[str, str]]] = None,
    mode: str = "",
    plugin_id: str = "feishu",
    require_enabled: bool = False,
    source: str = "",
) -> Dict[str, Any]:
    user_text = str(text or "").strip()
    if not user_text:
        raise ValueError("请先说一句话")
    cfg = get_im_chat_config(plugin_id)
    if require_enabled and not cfg.get("enabled"):
        raise ValueError("还没开启 IM 对话。到插件「对话」页打开开关。")
    intent = mode if mode in ("dialogue", "defect") else detect_im_intent(user_text)
    rows = [row for row in (history or []) if isinstance(row, dict)]
    from server.services.ai.role_plugin_graph import resolve_im
    from server.services.ai import dispatch_log as dispatch

    binding = resolve_im(plugin_id=plugin_id if plugin_id in IM_PLUGIN_IDS else "feishu", intent=intent)
    source_id = source or ("plugin_trial" if not require_enabled else f"{plugin_id}_im")
    tok = dispatch.bind(
        trigger="im_chat",
        source=source_id,
        role=binding["role_id"],
        job=binding["job"],
        skill=binding.get("skill_id") or "",
        routed_by="conductor",
        app_name=_PLUGIN_LABEL.get(plugin_id, plugin_id),
    )
    try:
        return _reply_im_bound(cfg, rows, user_text, binding)
    finally:
        dispatch.reset(tok)


def _reply_im_bound(
    cfg: Dict[str, Any],
    rows: List[Dict[str, str]],
    user_text: str,
    binding: Dict[str, Any],
) -> Dict[str, Any]:
    intent = str(binding.get("intent") or "dialogue")
    if intent == "defect":
        raw = _call_llm(cfg["defect_prompt"], rows, user_text, conversational=False)
        draft = _parse_json_object(raw)
        action = str(draft.get("action") or "clarify").strip().lower()
        reply = str(draft.get("reply") or "").strip()
        if action == "submit":
            submitted = _submit_defect(draft, str(binding.get("submit_plugin_id") or cfg.get("submit_plugin_id") or ""))
            if submitted.get("ok"):
                bug_id = submitted.get("bug_id")
                url = submitted.get("url") or ""
                reply = reply or f"已在禅道建单 #{bug_id}。"
                if url:
                    reply = f"{reply}\n{url}"
                return {
                    "ok": True,
                    "mode": "defect",
                    "action": "submit",
                    "reply": reply,
                    "bug_id": bug_id,
                    "url": url,
                }
            return {
                "ok": True,
                "mode": "defect",
                "action": "clarify",
                "reply": submitted.get("error") or "禅道没有收下这张单。",
            }
        if action == "reject":
            return {"ok": True, "mode": "defect", "action": "reject", "reply": reply or "这不像缺陷。直接问我就行，要提单请说「提缺陷」。"}
        return {"ok": True, "mode": "defect", "action": "clarify", "reply": reply or "再补一下标题、重现步骤和实际结果。"}
    from server.services.im_command import run_commander_turn

    reply = run_commander_turn(
        base_prompt=cfg["dialogue_prompt"],
        history=rows,
        user_text=user_text,
        call_llm=_call_llm,
    )
    return {"ok": True, "mode": "dialogue", "action": "chat", "reply": reply}


def send_feishu_text(*, chat_id: str, text: str, bot_id: str = "") -> None:
    from server.services.feishu_service import get_tenant_access_token

    receive_id = str(chat_id or "").strip()
    body = str(text or "").strip()
    if not receive_id or not body:
        return
    token = get_tenant_access_token(bot_id or None)
    import requests

    resp = requests.post(
        "https://open.feishu.cn/open-apis/im/v1/messages",
        params={"receive_id_type": "chat_id"},
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"receive_id": receive_id, "msg_type": "text", "content": json.dumps({"text": body}, ensure_ascii=False)},
        timeout=15,
    )
    data = {}
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code >= 400 or int(data.get("code") or 0) != 0:
        raise RuntimeError(data.get("msg") or f"飞书发消息失败 HTTP {resp.status_code}")


def _decrypt_feishu(encrypt: str, encrypt_key: str) -> Dict[str, Any]:
    import base64
    from Crypto.Cipher import AES

    key = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    blob = base64.b64decode(encrypt)
    iv, data = blob[:16], blob[16:]
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    pad = plain[-1]
    text = plain[:-pad].decode("utf-8")
    return json.loads(text)


def _feishu_message_text(content: str) -> str:
    raw = str(content or "").strip()
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except Exception:
        data = raw
    if isinstance(data, dict):
        text = data.get("text") or data.get("content") or ""
        if isinstance(text, dict):
            text = text.get("text") or ""
        raw = str(text or "")
    elif not isinstance(data, str):
        raw = str(data or "")
    raw = re.sub(r"@_user_\d+", "", raw)
    raw = re.sub(r"@_all", "", raw)
    return raw.strip()


def parse_feishu_event(payload: Any) -> Dict[str, Any]:
    body = payload if isinstance(payload, dict) else {}
    from server.services.system_settings_service import get_lark_event_secrets

    secrets = get_lark_event_secrets()
    encrypt_key = secrets.get("encrypt_key") or ""
    verification = secrets.get("verification_token") or ""
    bot_id = secrets.get("bot_id") or ""

    if body.get("encrypt"):
        if not encrypt_key:
            raise ValueError("飞书事件已加密。先到插件「连接」填写 Encrypt Key。")
        try:
            body = _decrypt_feishu(str(body.get("encrypt")), encrypt_key)
        except Exception as e:
            SLog.w(TAG, f"feishu decrypt failed: {e}")
            raise ValueError("飞书事件解密失败") from e

    if body.get("type") == "url_verification" or body.get("challenge"):
        token = str(body.get("token") or "")
        if verification and token and token != verification:
            raise ValueError("飞书 verification token 不匹配")
        return {"kind": "challenge", "challenge": body.get("challenge")}

    header = body.get("header") if isinstance(body.get("header"), dict) else {}
    event_token = str(header.get("token") or body.get("token") or "")
    if verification and event_token and event_token != verification:
        raise ValueError("飞书 verification token 不匹配")

    cfg = get_im_chat_config("feishu")
    if not cfg.get("enabled"):
        return {"kind": "ignore", "reason": "chat_off"}

    event = body.get("event") if isinstance(body.get("event"), dict) else {}
    event_type = str(header.get("event_type") or body.get("type") or "")
    if event_type not in ("im.message.receive_v1", "im.message.receive"):
        return {"kind": "ignore", "reason": event_type or "unknown"}

    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    if str(sender.get("sender_type") or "") == "app":
        return {"kind": "ignore", "reason": "bot"}
    chat_type = str(message.get("chat_type") or "")
    mentions = message.get("mentions") if isinstance(message.get("mentions"), list) else []
    if chat_type == "group" and not mentions:
        return {"kind": "ignore", "reason": "not_mentioned"}

    text = _feishu_message_text(str(message.get("content") or ""))
    if not text:
        return {"kind": "ignore", "reason": "empty"}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    return {
        "kind": "message",
        "bot_id": bot_id,
        "chat_id": str(message.get("chat_id") or ""),
        "user_id": str(sender_id.get("open_id") or sender_id.get("user_id") or "unknown"),
        "text": text,
    }


def reply_feishu_parsed(parsed: Dict[str, Any]) -> Dict[str, Any]:
    text = str(parsed.get("text") or "").strip()
    chat_id = str(parsed.get("chat_id") or "")
    user_id = str(parsed.get("user_id") or "unknown")
    bot_id = str(parsed.get("bot_id") or "")
    history = load_im_history("lark", chat_id, user_id)
    try:
        result = reply_im_message(
            text=text,
            history=history,
            plugin_id="feishu",
            require_enabled=True,
        )
    except Exception as e:
        SLog.w(TAG, f"im reply failed: {e}")
        result = {"ok": False, "reply": str(e) or "我这边暂时答不上来。"}
    reply = str(result.get("reply") or "").strip()
    append_im_history("lark", chat_id, user_id, "user", text)
    if reply:
        append_im_history("lark", chat_id, user_id, "assistant", reply)
        try:
            send_feishu_text(chat_id=chat_id, text=reply, bot_id=bot_id)
        except Exception as e:
            SLog.w(TAG, f"feishu send failed: {e}")
    return {"ok": True, "mode": result.get("mode"), "replied": bool(reply)}


def _safe_reply_feishu(parsed: Dict[str, Any]) -> None:
    try:
        reply_feishu_parsed(parsed)
    except Exception as e:
        SLog.w(TAG, f"feishu reply thread: {e}")


def accept_feishu_event(payload: Any) -> Dict[str, Any]:
    parsed = parse_feishu_event(payload)
    if parsed.get("kind") == "challenge":
        result = {"challenge": parsed.get("challenge")}
        record_im_inbound({"kind": "challenge", "result": result})
        return result
    if parsed.get("kind") == "message":
        threading.Thread(target=_safe_reply_feishu, args=(parsed,), daemon=True).start()
        result = {"ok": True, "queued": True, "text": str(parsed.get("text") or "")[:80]}
        record_im_inbound({"kind": "message", "result": result})
        SLog.i(TAG, f"feishu message queued: {result.get('text')}")
        return result
    result = {"ok": True, "ignored": parsed.get("reason") or "unknown"}
    record_im_inbound({"kind": "ignore", "result": result})
    SLog.i(TAG, f"feishu event ignored: {result.get('ignored')}")
    return result


def handle_feishu_event(payload: Any) -> Dict[str, Any]:
    parsed = parse_feishu_event(payload)
    if parsed.get("kind") == "challenge":
        return {"challenge": parsed.get("challenge")}
    if parsed.get("kind") == "message":
        return reply_feishu_parsed(parsed)
    return {"ok": True, "ignored": parsed.get("reason") or "unknown"}


# Routers / listeners historically mixed these spellings.
IM_PLUGIN_IDS = IM_PLUGIN_IDS
reply_im_message = reply_im_message
get_im_chat_config = get_im_chat_config
record_im_inbound = record_im_inbound
get_im_inbound = get_im_inbound
accept_feishu_event = accept_feishu_event
load_im_history = load_im_history
append_im_history = append_im_history
