# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""能力菜单 → OpenAI function tools。agent-decide 用，其它 job 仍走 JSON。"""
from __future__ import annotations

import json
from typing import Any, Optional

COORD = {
    "type": "integer",
    "minimum": 0,
    "maximum": 1000,
    "description": "0-1000 归一化坐标（千分比，不是像素）",
}

PARAM_DEFAULTS: dict[str, dict[str, Any]] = {
    "tap_element": {
        "type": "object",
        "properties": {
            "x": COORD,
            "y": COORD,
        },
        "required": ["x", "y"],
    },
    "long_press_element": {
        "type": "object",
        "properties": {
            "x": COORD,
            "y": COORD,
            "duration_ms": {"type": "integer", "description": "按住毫秒，默认 800"},
        },
        "required": ["x", "y"],
    },
    "multi_tap": {
        "type": "object",
        "properties": {
            "x": COORD,
            "y": COORD,
            "count": {
                "type": "integer",
                "minimum": 2,
                "maximum": 12,
                "description": "连点次数，默认 6。调试面板/版本号彩蛋用这条，不要拆成多次 tap_element",
            },
            "interval_ms": {
                "type": "integer",
                "minimum": 40,
                "maximum": 400,
                "description": "两次点击间隔毫秒，默认 80",
            },
        },
        "required": ["x", "y"],
    },
    "input_text": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要输入的文本；口令可写占位，值由系统填"},
            "x": COORD,
            "y": COORD,
            "field": {
                "type": "string",
                "enum": ["phone", "sms_code", "password", "text"],
                "description": "登录页手机号/口令时必填，值由资源网关填",
            },
        },
        "required": ["text", "x", "y"],
    },
    "swipe_element_to_element": {
        "type": "object",
        "properties": {
            "from_x": COORD,
            "from_y": COORD,
            "to_x": COORD,
            "to_y": COORD,
            "duration_ms": {"type": "integer"},
        },
        "required": ["from_x", "from_y", "to_x", "to_y"],
    },
    "swipe_direction": {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "duration_ms": {"type": "integer"},
        },
        "required": ["direction"],
    },
    "press_key": {
        "type": "object",
        "properties": {
            "key": {
                "type": "string",
                "enum": ["back", "home", "menu", "enter", "power"],
            },
        },
        "required": ["key"],
    },
    "wait_ms": {
        "type": "object",
        "properties": {
            "ms": {"type": "integer", "description": "等待毫秒"},
            "duration_ms": {"type": "integer", "description": "与 ms 同义"},
        },
    },
    "wait_screen_ready": {"type": "object", "properties": {}},
    "launch_app": {
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "必须是本趟目标应用包名"},
        },
        "required": ["package"],
    },
    "close_app": {
        "type": "object",
        "properties": {"package": {"type": "string"}},
        "required": ["package"],
    },
    "kill_app": {
        "type": "object",
        "properties": {"package": {"type": "string"}},
        "required": ["package"],
    },
    "get_app_version": {
        "type": "object",
        "properties": {"package": {"type": "string"}},
    },
    "get_foreground_app": {"type": "object", "properties": {}},
    "human_input_text": {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "field": {"type": "string", "enum": ["sms_code", "phone", "text"]},
        },
        "required": ["question"],
    },
}

SIGNAL_DONE = "signal_done"
SIGNAL_GIVE_UP = "signal_give_up"
SIGNAL_ASK_HUMAN = "signal_ask_human"
CONTROL_TOOL_NAMES = frozenset({SIGNAL_DONE, SIGNAL_GIVE_UP, SIGNAL_ASK_HUMAN})

CONTROL_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": SIGNAL_DONE,
            "description": "本步操作已经做完，不要再点。不是整案完成，也不是校验通过。",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought": {"type": "string"},
                    "expected_after": {"type": "string"},
                    "remember": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "knowledge_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SIGNAL_GIVE_UP,
            "description": "客观做不到本步（缺账号、缺入口、设备不对）。不要用这个表示校验失败。",
            "parameters": {
                "type": "object",
                "properties": {"thought": {"type": "string"}},
                "required": ["thought"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": SIGNAL_ASK_HUMAN,
            "description": "需要人提供能填进界面的信息。禁止让人去设备上点。已租账号时不要用这个要手机号或验证码。",
            "parameters": {
                "type": "object",
                "properties": {
                    "thought": {"type": "string"},
                    "question": {"type": "string"},
                    "field": {"type": "string", "enum": ["sms_code", "phone", "text"]},
                },
                "required": ["question"],
            },
        },
    },
]


def _yaml_params_to_schema(rows: Any) -> Optional[dict[str, Any]]:
    if not isinstance(rows, list) or not rows:
        return None
    props: dict[str, Any] = {}
    required: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        spec: dict[str, Any] = {"type": str(row.get("type") or "string")}
        if row.get("description"):
            spec["description"] = str(row.get("description"))
        if row.get("enum"):
            spec["enum"] = list(row.get("enum") or [])
        if row.get("minimum") is not None:
            spec["minimum"] = row.get("minimum")
        if row.get("maximum") is not None:
            spec["maximum"] = row.get("maximum")
        props[name] = spec
        if row.get("required"):
            required.append(name)
    if not props:
        return None
    out: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def params_schema_for(cap_id: str, yaml_params: Any = None) -> dict[str, Any]:
    from_yaml = _yaml_params_to_schema(yaml_params)
    if from_yaml:
        return from_yaml
    return dict(PARAM_DEFAULTS.get(str(cap_id or ""), {"type": "object", "properties": {}}))


def openai_tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": str(name or "").strip(),
            "description": (description or name or "")[:240],
            "parameters": parameters or {"type": "object", "properties": {}},
        },
    }


def tools_for_menu(menu: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """当前 Run 可用能力 + 三个控制信号。assert / 资源网关能力不进 tools。"""
    tools: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in menu or []:
        if not isinstance(row, dict):
            continue
        cid = str(row.get("id") or "").strip()
        if not cid or cid in seen or cid in CONTROL_TOOL_NAMES:
            continue
        if cid in {
            "assert_visual", "assert_goal", "assert",
            "get_otp", "get_phone", "lease_account", "release_account",
            "provision_session", "pick_account",
        }:
            continue
        seen.add(cid)
        yaml_params = None
        try:
            from server.services.plugins import registry as plugin_registry

            cap = plugin_registry.get_capability(cid)
            yaml_params = getattr(cap, "params", None) if cap is not None else None
        except Exception:
            yaml_params = None
        tools.append(openai_tool(
            cid,
            str(row.get("summary") or cid),
            params_schema_for(cid, yaml_params),
        ))
    tools.extend(CONTROL_TOOLS)
    return tools


def tools_chat_payload(tools: list[dict[str, Any]]) -> dict[str, Any]:
    if not tools:
        return {}
    return {
        "tools": tools,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
    }


def _args_of(call: dict[str, Any]) -> dict[str, Any]:
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    raw = fn.get("arguments")
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_tool_call(tool_calls: Any) -> Optional[dict[str, Any]]:
    rows = tool_calls if isinstance(tool_calls, list) else []
    for row in rows:
        if isinstance(row, dict) and str((row.get("function") or {}).get("name") or "").strip():
            return row
    return None


def decision_from_tool_calls(
    tool_calls: Any,
    *,
    content: str = "",
) -> Optional[dict[str, Any]]:
    """把 OpenAI tool_calls 收成 decide JSON。只取第一个 call。"""
    call = _first_tool_call(tool_calls)
    if call is None:
        return None
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = str(fn.get("name") or "").strip()
    args = _args_of(call)
    thought = str(args.pop("thought", "") or content or "").strip()
    remember = args.pop("remember", None)
    knowledge_ids = args.pop("knowledge_ids", None)
    out: dict[str, Any] = {
        "thought": thought,
        "status": "continue",
        "action": None,
        "expected_after": str(args.pop("expected_after", "") or ""),
        "remember": remember if isinstance(remember, list) else [],
        "knowledge_ids": knowledge_ids if isinstance(knowledge_ids, list) else [],
        "_tool_name": name,
    }
    if name == SIGNAL_DONE:
        out["status"] = "done"
        return out
    if name == SIGNAL_GIVE_UP:
        out["status"] = "give_up"
        return out
    if name == SIGNAL_ASK_HUMAN:
        out["status"] = "ask_human"
        out["action"] = {
            "capability_id": "human_input_text",
            "params": {
                "question": str(args.get("question") or thought or "需要你提供信息"),
                "field": str(args.get("field") or "text"),
            },
        }
        return out
    out["action"] = {"capability_id": name, "params": args}
    return out


def merge_tool_call_deltas(acc: list[dict[str, Any]], deltas: Any) -> None:
    """把流式 delta.tool_calls 拼进 acc（按 index）。"""
    rows = deltas if isinstance(deltas, list) else []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        try:
            idx = int(raw.get("index") or 0)
        except (TypeError, ValueError):
            idx = 0
        if idx < 0:
            idx = 0
        while len(acc) <= idx:
            acc.append({
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""},
            })
        row = acc[idx]
        if raw.get("id"):
            row["id"] = str(raw.get("id") or "")
        if raw.get("type"):
            row["type"] = str(raw.get("type") or "function")
        fn = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        dest = row.setdefault("function", {"name": "", "arguments": ""})
        if fn.get("name"):
            dest["name"] = str(fn.get("name") or "")
        if fn.get("arguments"):
            dest["arguments"] = str(dest.get("arguments") or "") + str(fn.get("arguments") or "")
