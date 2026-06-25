# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
ClawNode EXEC_SCRIPT 脚本库与工具函数。

Server 维护可复用 DSL/JS 脚本，通过 RemoteEngine.exec_script() 下发到设备执行，
无需为每个新能力发 ClawNode APK（需设备 >= v1.8.0）。
"""
from __future__ import annotations

import json
from typing import Any

# 预置脚本：key -> {language, script}；script 可为 str 或接受 vars 的 callable
_BUILTIN: dict[str, Any] = {}


def _register_builtin(script_id: str, *, language: str = "dsl"):
    def deco(fn):
        _BUILTIN[script_id] = {"language": language, "builder": fn}
        return fn
    return deco


@_register_builtin("open_settings")
def _open_settings(_vars: dict | None = None) -> str:
    return json.dumps({
        "steps": [
            {"op": "wake"},
            {"op": "open_app", "package": "com.android.settings"},
            {"op": "sleep", "ms": 1500},
            {"op": "foreground"},
        ],
    }, ensure_ascii=False)


@_register_builtin("open_app_settings")
def _open_app_settings(vars: dict | None = None) -> str:
    pkg = str((vars or {}).get("package") or (vars or {}).get("pkg") or "").strip()
    if not pkg:
        raise ValueError("open_app_settings requires package")
    return json.dumps({
        "steps": [
            {"op": "wake"},
            {"op": "shell", "command": f"am start -a android.settings.APPLICATION_DETAILS_SETTINGS -d package:{pkg}"},
            {"op": "sleep", "ms": 2000},
            {"op": "foreground"},
        ],
    }, ensure_ascii=False)


@_register_builtin("home")
def _home(_vars: dict | None = None) -> str:
    return json.dumps({"steps": [{"op": "key", "key": "home"}]}, ensure_ascii=False)


@_register_builtin("launch_package")
def _launch_package(vars: dict | None = None) -> str:
    pkg = str((vars or {}).get("package") or (vars or {}).get("pkg") or "").strip()
    if not pkg:
        raise ValueError("launch_package requires package")
    activity = str((vars or {}).get("activity") or "").strip()
    step: dict[str, Any] = {"op": "open_app", "package": pkg}
    if activity:
        step["activity"] = activity
    return json.dumps({
        "steps": [
            {"op": "wake"},
            step,
            {"op": "sleep", "ms": int((vars or {}).get("wait_ms") or 2000)},
            {"op": "foreground"},
        ],
    }, ensure_ascii=False)


@_register_builtin("shell_raw", language="js")
def _shell_raw_js(vars: dict | None = None) -> str:
    cmd = str((vars or {}).get("command") or (vars or {}).get("cmd") or "").strip()
    if not cmd:
        raise ValueError("shell_raw requires command")
    # JSON 转义 command 中的引号与反斜杠
    escaped = json.dumps(cmd)
    return f"claw.shell({escaped});"


def list_script_ids() -> list[str]:
    return sorted(_BUILTIN.keys())


def resolve_script(
    *,
    script: str = "",
    script_id: str = "",
    language: str = "",
    script_vars: dict | None = None,
) -> tuple[str, str]:
    """
    解析最终脚本正文与语言。
    优先使用内联 script；否则按 script_id 查库。
    返回 (script_body, language)。
    """
    inline = (script or "").strip()
    if inline:
        lang = (language or "dsl").strip().lower() or "dsl"
        return inline, lang

    sid = (script_id or "").strip()
    if not sid:
        raise ValueError("EXEC_SCRIPT requires script or script_id")

    entry = _BUILTIN.get(sid)
    if not entry:
        raise ValueError(f"unknown script_id={sid!r} (available: {', '.join(list_script_ids())})")

    lang = (language or entry.get("language") or "dsl").strip().lower() or "dsl"
    builder = entry.get("builder")
    if callable(builder):
        body = builder(script_vars or {})
    else:
        body = str(entry.get("script") or "")
    if not str(body).strip():
        raise ValueError(f"script_id={sid} resolved to empty body")
    return str(body).strip(), lang


def build_exec_script_command_params(
    *,
    script: str = "",
    script_id: str = "",
    language: str = "",
    timeout_ms: int = 60_000,
    script_vars: dict | None = None,
) -> dict[str, Any]:
    body, lang = resolve_script(
        script=script,
        script_id=script_id,
        language=language,
        script_vars=script_vars,
    )
    return {
        "script": body,
        "language": lang,
        "timeout_ms": int(timeout_ms),
    }


def parse_exec_script_response(data: dict | None) -> tuple[bool, str, str]:
    """解析 ClawNode EXEC_SCRIPT 回传（shellResult 格式）。"""
    if not data:
        return False, "", "no response"
    status = str(data.get("status") or "").lower()
    ok = status == "success"
    stdout = (data.get("stdout") or data.get("message") or "").strip()
    stderr = (data.get("stderr") or "").strip()
    return ok, stdout, stderr
