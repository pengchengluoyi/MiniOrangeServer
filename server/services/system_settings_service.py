# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""服务端全局配置（存于 config.json）。"""
from __future__ import annotations

import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

import yaml

from server.core.security import SecurityManager

from server.core.database import APP_DATA_DIR
from script.log import SLog

TAG = "system_settings_service"


def _feishu_root() -> Dict[str, Any]:
    SecurityManager.load()
    fb = SecurityManager._config.get("feishu")
    if not isinstance(fb, dict):
        return {}
    return fb


def _migrate_legacy_feishu(feishu: Dict[str, Any]) -> List[Dict[str, Any]]:
    """单机器人旧配置 → bots 列表。"""
    bots = feishu.get("bots")
    if isinstance(bots, list) and bots:
        return bots
    app_id = (feishu.get("app_id") or os.environ.get("FEISHU_APP_ID", "")).strip()
    secret = (feishu.get("app_secret") or os.environ.get("FEISHU_APP_SECRET", "")).strip()
    if app_id or secret:
        return [
            {
                "id": "default",
                "name": "默认机器人",
                "app_id": app_id,
                "app_secret": secret,
            }
        ]
    return []


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return secret[:4] + "****" + secret[-4:]


AI_PROVIDER_PRESETS: List[Dict[str, Any]] = [
    {
        "id": "openai",
        "name": "OpenAI",
        "api_type": "openai",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "model_options": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini"],
    },
    {
        "id": "anthropic",
        "name": "Anthropic",
        "api_type": "anthropic",
        "base_url": "https://api.anthropic.com",
        "default_model": "claude-3-5-sonnet-latest",
        "model_options": ["claude-3-5-sonnet-latest", "claude-3-5-haiku-latest", "claude-sonnet-4-20250514"],
    },
    {
        "id": "umodelverse",
        "name": "UModelverse",
        "api_type": "anthropic",
        "base_url": "https://api.modelverse.cn",
        "default_model": "claude-sonnet-4-5-20250929",
        "model_options": [
            "claude-sonnet-4-5-20250929",
            "claude-opus-4-1-20250805",
            "claude-3-7-sonnet-20250219",
            "claude-3-5-sonnet-20241022",
        ],
    },
    {
        "id": "google",
        "name": "Google Gemini",
        "api_type": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-2.5-flash",
        "model_options": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash", "gemini-1.5-flash"],
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "api_type": "openai",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "model_options": ["deepseek-chat", "deepseek-reasoner"],
    },
    {
        "id": "qwen",
        "name": "通义千问",
        "api_type": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
        "model_options": ["qwen-plus", "qwen-max", "qwen-turbo", "qwen-long"],
    },
    {
        "id": "volcengine",
        "name": "火山引擎",
        "api_type": "openai",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "doubao-seed-2-0-lite-260215",
        "model_options": [
            # Seed 2.0 多模态 / Vision / GUI（推荐）
            "doubao-seed-2-0-pro-260215",
            "doubao-seed-2-0-lite-260215",
            "doubao-seed-2-0-mini-260215",
            # Seed 1.8 多模态
            "doubao-seed-1-8-251228",
            # Seed 1.6 多模态 / Vision
            "doubao-seed-1-6-vision-250815",
            "doubao-seed-1-6-250615",
            "doubao-seed-1-6-thinking-250615",
            "doubao-seed-1-6-flash-250615",
            "doubao-seed-1-6-flash-250828",
            "doubao-seed-1-6-lite-251015",
            "doubao-seed-1-6-251015",
            # Seed 1.5 Vision
            "doubao-1-5-thinking-vision-pro",
            "doubao-1-5-vision-pro-32k-250115",
            # 文本 / 推理（无截图坐标 Plan 能力，按需选用）
            "doubao-1-5-pro-32k-250115",
            "deepseek-v3-250324",
            "deepseek-r1-250120",
        ],
    },
]


def _ai_root() -> Dict[str, Any]:
    root = _testing_root()
    ai = root.setdefault("ai_providers", {})
    if not isinstance(ai, dict):
        ai = {}
        root["ai_providers"] = ai
    return ai


def normalize_plan_compress_ratio(value: Any) -> float:
    """Plan 截图压缩比例：保留一位小数，1.0 表示不压缩。"""
    try:
        ratio = round(float(value), 1)
    except (TypeError, ValueError):
        ratio = 3.0
    return max(1.0, min(10.0, ratio))


def _provider_public(provider_id: str, raw: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    preset = next((p for p in AI_PROVIDER_PRESETS if p["id"] == provider_id), None)
    raw = raw if isinstance(raw, dict) else {}
    api_key = (raw.get("api_key") or "").strip()
    model_options = list((preset or {}).get("model_options") or [])
    raw_model = (raw.get("model") or "").strip()
    default_model = ((preset or {}).get("default_model") or "").strip()
    # model_options 仅为 UI 候选；也支持手填 ep- 接入点或方舟新模型 ID
    model = raw_model or default_model
    configured = bool(api_key)
    enabled = raw.get("enabled", True) is not False
    case_execution_use = raw.get("case_execution_use") is True
    if not configured:
        enabled = False
        case_execution_use = False
    elif not enabled:
        case_execution_use = False
    return {
        "id": provider_id,
        "name": raw.get("name") or (preset or {}).get("name") or provider_id,
        "api_type": (raw.get("api_type") or (preset or {}).get("api_type") or "openai").strip(),
        "base_url": (raw.get("base_url") or (preset or {}).get("base_url") or "").strip(),
        "model": model,
        "model_options": model_options,
        "api_key_masked": _mask_secret(api_key),
        "configured": configured,
        "enabled": enabled,
        "case_execution_use": case_execution_use,
        "plan_compress_ratio": normalize_plan_compress_ratio(
            raw.get("plan_compress_ratio", _default_plan_compress_ratio(provider_id))
        ),
    }


def list_ai_provider_settings() -> Dict[str, Any]:
    ai = _ai_root()
    providers = [_provider_public(p["id"], ai.get(p["id"])) for p in AI_PROVIDER_PRESETS]
    custom_ids = [
        x
        for x in ai.keys()
        if x not in {p["id"] for p in AI_PROVIDER_PRESETS} and not str(x).startswith("_")
    ]
    for provider_id in sorted(custom_ids):
        providers.append(_provider_public(provider_id, ai.get(provider_id)))
    return {
        "providers": providers,
        "presets": AI_PROVIDER_PRESETS,
        "default_provider": (ai.get("_default_provider") or "openai"),
        "usage": get_ai_usage_settings(),
    }


def get_ai_usage_settings() -> Dict[str, Any]:
    ai = _ai_root()
    usage = ai.get("_usage") if isinstance(ai.get("_usage"), dict) else {}
    return {
        "copilot_enabled": bool(usage.get("copilot_enabled", False)),
        "case_execution_enabled": bool(usage.get("case_execution_enabled", False)),
        "case_execution_provider_id": (usage.get("case_execution_provider_id") or "").strip().lower(),
        "mode": usage.get("mode") or "local_first",
        "plan_compress_image": bool(usage.get("plan_compress_image", True)),
    }


def save_ai_usage_settings(
    *,
    copilot_enabled: bool = False,
    case_execution_enabled: bool = False,
    case_execution_provider_id: str = "",
    mode: str = "local_first",
    plan_compress_image: bool = True,
) -> Dict[str, Any]:
    ai = _ai_root()
    mode = mode if mode in {"local_first", "ai_first"} else "local_first"
    ai["_usage"] = {
        "copilot_enabled": bool(copilot_enabled),
        "case_execution_enabled": bool(case_execution_enabled),
        "case_execution_provider_id": (case_execution_provider_id or "").strip().lower(),
        "mode": mode,
        "plan_compress_image": bool(plan_compress_image),
    }
    SecurityManager.save()
    return get_ai_usage_settings()


def ai_plan_compress_image_enabled() -> bool:
    """兼容旧配置：全局开关仍可读，优先使用各模型 plan_compress_ratio。"""
    return bool(get_ai_usage_settings().get("plan_compress_image", True))


def _default_plan_compress_ratio(provider_id: str) -> float:
    return 3.0


def get_ai_plan_compress_ratio(provider_id: Optional[str] = None) -> float:
    """返回模型 Plan 截图压缩比例；1.0=不压缩，默认 3.0（宽高各除以 3）。"""
    ai = _ai_root()
    target_id = (provider_id or ai.get("_default_provider") or "openai").strip().lower()
    raw = ai.get(target_id)
    if isinstance(raw, dict) and raw.get("plan_compress_ratio") is not None:
        return normalize_plan_compress_ratio(raw.get("plan_compress_ratio"))
    if not ai_plan_compress_image_enabled():
        return 1.0
    return _default_plan_compress_ratio(target_id)


def get_ai_provider_credentials(provider_id: Optional[str] = None) -> Dict[str, Any]:
    """供 LLM 调用层读取明文凭据；不要直接暴露给前端 API。"""
    ai = _ai_root()
    target_id = (provider_id or ai.get("_default_provider") or "openai").strip().lower()
    raw = ai.get(target_id)
    if not isinstance(raw, dict):
        raw = {}
    preset = next((p for p in AI_PROVIDER_PRESETS if p["id"] == target_id), None)
    api_key = (raw.get("api_key") or "").strip()
    return {
        "id": target_id,
        "name": raw.get("name") or (preset or {}).get("name") or target_id,
        "api_type": (raw.get("api_type") or (preset or {}).get("api_type") or "openai").strip(),
        "api_key": api_key,
        "base_url": (raw.get("base_url") or (preset or {}).get("base_url") or "").strip(),
        "model": (raw.get("model") or (preset or {}).get("default_model") or "").strip(),
        "enabled": raw.get("enabled", True) is not False,
        "configured": bool(api_key),
        "plan_compress_ratio": normalize_plan_compress_ratio(
            (raw or {}).get("plan_compress_ratio", _default_plan_compress_ratio(target_id))
        ),
        "case_execution_use": (raw or {}).get("case_execution_use") is True,
    }


def find_case_execution_provider_id() -> str:
    """从大模型 Key 配置里找「可用=true 且 用例=true」的 provider id。

    与 KeysPage 单选「用例」逻辑一致；优先 usage.case_execution_provider_id，
    再扫描各 provider 行的 case_execution_use 标记。
    """
    usage = get_ai_usage_settings()
    usage_pid = (usage.get("case_execution_provider_id") or "").strip().lower()
    if usage_pid:
        cred = get_ai_provider_credentials(usage_pid)
        if cred.get("configured") and cred.get("enabled") and cred.get("case_execution_use"):
            return usage_pid
    ai = _ai_root()
    for pid, raw in ai.items():
        if pid.startswith("_") or not isinstance(raw, dict):
            continue
        cred = get_ai_provider_credentials(pid)
        if cred.get("configured") and cred.get("enabled") and cred.get("case_execution_use"):
            return pid
    return usage_pid


def should_use_ai_planning(channel: str, provider_id: Optional[str] = None) -> Dict[str, Any]:
    """Return whether a planning channel may call the configured LLM."""
    usage = get_ai_usage_settings()
    normalized = (channel or "copilot").strip().lower()
    explicit_provider = bool((provider_id or "").strip())
    if normalized in {"case", "case_execution", "regression", "feishu"}:
        scope_enabled = usage["case_execution_enabled"]
    elif normalized == "copilot" and explicit_provider:
        scope_enabled = True
    else:
        scope_enabled = usage["copilot_enabled"]
    selected_provider_id = provider_id
    if normalized in {"case", "case_execution", "regression", "feishu"} and not selected_provider_id:
        selected_provider_id = find_case_execution_provider_id() or None
    provider = get_ai_provider_credentials(selected_provider_id)
    ok = bool(
        scope_enabled
        and provider.get("configured")
        and provider.get("enabled")
        and provider.get("case_execution_use")
    )
    reason = ""
    if not scope_enabled:
        reason = "未开启「使用大模型能力」（请到密钥配置 → 大模型 Key）"
    elif not provider.get("configured"):
        reason = f"AI provider key missing: {provider.get('id')}"
    elif not provider.get("enabled"):
        reason = f"AI provider disabled: {provider.get('id')}"
    elif not provider.get("case_execution_use"):
        reason = "未找到「可用 + 用例」的大模型（请到密钥配置 → 大模型 Key 设置）"
    return {
        "enabled": ok,
        "reason": reason,
        "mode": usage.get("mode") or "local_first",
        "channel": normalized,
        "provider": {k: v for k, v in provider.items() if k != "api_key"},
    }


def save_ai_provider_settings(
    provider_id: str,
    *,
    name: str = "",
    api_type: str = "",
    api_key: str = "",
    base_url: str = "",
    model: str = "",
    enabled: bool = True,
    clear_key: bool = False,
    set_default: bool = False,
    plan_compress_ratio: Optional[float] = None,
    case_execution_use: Optional[bool] = None,
) -> Dict[str, Any]:
    provider_id = (provider_id or "").strip().lower()
    if not provider_id:
        raise ValueError("provider_id 不能为空")
    ai = _ai_root()
    current = ai.setdefault(provider_id, {})
    preset = next((p for p in AI_PROVIDER_PRESETS if p["id"] == provider_id), None)
    current["name"] = (name or current.get("name") or (preset or {}).get("name") or provider_id).strip()
    current["api_type"] = (api_type or current.get("api_type") or (preset or {}).get("api_type") or "openai").strip()
    current["base_url"] = (base_url or current.get("base_url") or (preset or {}).get("base_url") or "").strip()
    current["model"] = (model or current.get("model") or (preset or {}).get("default_model") or "").strip()
    if clear_key:
        current.pop("api_key", None)
    elif api_key and str(api_key).strip():
        current["api_key"] = str(api_key).strip()
    has_key = bool((current.get("api_key") or "").strip())
    current["enabled"] = enabled is not False and has_key
    if case_execution_use is not None:
        use = bool(case_execution_use) and has_key and (enabled is not False)
        current["case_execution_use"] = use
        if use:
            for pid, row in list(ai.items()):
                if pid.startswith("_") or pid == provider_id:
                    continue
                if isinstance(row, dict) and row.get("case_execution_use"):
                    row["case_execution_use"] = False
    elif not has_key:
        current["case_execution_use"] = False
    if not current.get("enabled"):
        current["case_execution_use"] = False
    if plan_compress_ratio is not None:
        current["plan_compress_ratio"] = normalize_plan_compress_ratio(plan_compress_ratio)
    elif "plan_compress_ratio" not in current:
        current["plan_compress_ratio"] = 3.0
    if set_default:
        ai["_default_provider"] = provider_id
    SecurityManager.save()
    return _provider_public(provider_id, current)


def delete_ai_provider_settings(provider_id: str) -> None:
    provider_id = (provider_id or "").strip().lower()
    ai = _ai_root()
    ai.pop(provider_id, None)
    if ai.get("_default_provider") == provider_id:
        ai["_default_provider"] = "openai"
    SecurityManager.save()


def _bot_public(bot: Dict[str, Any]) -> Dict[str, Any]:
    secret = (bot.get("app_secret") or "").strip()
    app_id = (bot.get("app_id") or "").strip()
    return {
        "id": bot.get("id") or "",
        "name": bot.get("name") or "未命名",
        "platform": "lark",
        "platform_label": "飞书",
        "app_id": app_id,
        "app_secret_masked": _mask_secret(secret),
        "configured": bool(app_id and secret),
    }


ROBOT_PLATFORM_PRESETS: Dict[str, Dict[str, Any]] = {
    "lark": {
        "label": "飞书",
        "required": ["app_id", "app_secret"],
        "secret_fields": ["app_secret"],
    },
    "wecom": {
        "label": "企业微信",
        "required": ["webhook_url"],
        "secret_fields": ["webhook_url", "secret"],
    },
    "dingtalk": {
        "label": "钉钉",
        "required": ["webhook_url"],
        "secret_fields": ["webhook_url", "secret"],
    },
    "slack": {
        "label": "Slack",
        "required": ["webhook_url"],
        "secret_fields": ["webhook_url", "bot_token"],
    },
}


def _robots_root() -> Dict[str, Any]:
    root = _testing_root()
    robots = root.setdefault("robots", {})
    if not isinstance(robots, dict):
        robots = {}
        root["robots"] = robots
    return robots


def _legacy_feishu_as_robot(bot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": bot.get("id") or uuid.uuid4().hex[:12],
        "platform": "lark",
        "name": bot.get("name") or "飞书机器人",
        "credentials": {
            "app_id": bot.get("app_id") or "",
            "app_secret": bot.get("app_secret") or "",
        },
    }


def _robot_public(row: Dict[str, Any]) -> Dict[str, Any]:
    platform = (row.get("platform") or "lark").strip()
    preset = ROBOT_PLATFORM_PRESETS.get(platform, ROBOT_PLATFORM_PRESETS["lark"])
    credentials = row.get("credentials") if isinstance(row.get("credentials"), dict) else {}
    masked: Dict[str, str] = {}
    public_credentials: Dict[str, str] = {}
    for key, value in credentials.items():
        val = str(value or "").strip()
        if key in preset.get("secret_fields", []):
            masked[f"{key}_masked"] = _mask_secret(val)
        else:
            public_credentials[key] = val
    configured = all(str(credentials.get(k) or "").strip() for k in preset.get("required", []))
    # Compatibility fields used by existing regression selectors.
    app_id = str(credentials.get("app_id") or "").strip()
    app_secret = str(credentials.get("app_secret") or "").strip()
    return {
        "id": row.get("id") or "",
        "platform": platform,
        "platform_label": preset.get("label") or platform,
        "name": row.get("name") or preset.get("label") or "机器人",
        "credentials": public_credentials,
        "masked": masked,
        "configured": configured,
        "app_id": app_id,
        "app_secret_masked": _mask_secret(app_secret),
    }


def _list_robot_rows() -> List[Dict[str, Any]]:
    robots = _robots_root()
    items = robots.get("items")
    if isinstance(items, list):
        rows = [dict(x) for x in items if isinstance(x, dict)]
    else:
        rows = []
    if not rows:
        legacy = _migrate_legacy_feishu(_feishu_root())
        rows = [_legacy_feishu_as_robot(b) for b in legacy]
        if rows:
            robots["items"] = rows
            SecurityManager.save()
    return rows


def _save_robot_rows(rows: List[Dict[str, Any]]) -> None:
    robots = _robots_root()
    robots["items"] = rows
    # Keep lark credentials mirrored for older feishu services.
    lark_bots = []
    for row in rows:
        if (row.get("platform") or "lark") != "lark":
            continue
        credentials = row.get("credentials") if isinstance(row.get("credentials"), dict) else {}
        lark_bots.append(
            {
                "id": row.get("id") or uuid.uuid4().hex[:12],
                "name": row.get("name") or "飞书机器人",
                "app_id": credentials.get("app_id") or "",
                "app_secret": credentials.get("app_secret") or "",
            }
        )
    feishu = SecurityManager._config.setdefault("feishu", {})
    feishu["bots"] = lark_bots
    feishu.pop("app_id", None)
    feishu.pop("app_secret", None)
    SecurityManager.save()


def list_robot_integrations() -> List[Dict[str, Any]]:
    return [_robot_public(x) for x in _list_robot_rows()]


def create_robot_integration(*, platform: str, name: str, credentials: Dict[str, Any]) -> Dict[str, Any]:
    platform = (platform or "lark").strip()
    if platform not in ROBOT_PLATFORM_PRESETS:
        raise ValueError(f"不支持的平台: {platform}")
    clean_credentials = {str(k): str(v).strip() for k, v in (credentials or {}).items()}
    preset = ROBOT_PLATFORM_PRESETS[platform]
    for key in preset.get("required", []):
        if not clean_credentials.get(key):
            raise ValueError(f"请填写 {key}")
    rows = _list_robot_rows()
    row = {
        "id": uuid.uuid4().hex[:12],
        "platform": platform,
        "name": (name or preset.get("label") or "机器人").strip(),
        "credentials": clean_credentials,
    }
    rows.append(row)
    _save_robot_rows(rows)
    return _robot_public(row)


def update_robot_integration(
    robot_id: str,
    *,
    platform: Optional[str] = None,
    name: Optional[str] = None,
    credentials: Optional[Dict[str, Any]] = None,
    clear_secret: bool = False,
) -> Dict[str, Any]:
    rows = _list_robot_rows()
    found = next((x for x in rows if str(x.get("id")) == str(robot_id)), None)
    if not found:
        raise ValueError(f"机器人不存在: {robot_id}")
    if platform:
        if platform not in ROBOT_PLATFORM_PRESETS:
            raise ValueError(f"不支持的平台: {platform}")
        found["platform"] = platform
    if name is not None:
        found["name"] = str(name).strip() or found.get("name")
    current = found.setdefault("credentials", {})
    if not isinstance(current, dict):
        current = {}
        found["credentials"] = current
    if clear_secret:
        preset = ROBOT_PLATFORM_PRESETS.get(found.get("platform") or "lark", ROBOT_PLATFORM_PRESETS["lark"])
        for key in preset.get("secret_fields", []):
            current.pop(key, None)
    for key, value in (credentials or {}).items():
        val = str(value or "").strip()
        if val:
            current[str(key)] = val
    _save_robot_rows(rows)
    return _robot_public(found)


def delete_robot_integration(robot_id: str) -> None:
    rows = _list_robot_rows()
    new_rows = [x for x in rows if str(x.get("id")) != str(robot_id)]
    if len(new_rows) == len(rows):
        raise ValueError(f"机器人不存在: {robot_id}")
    _save_robot_rows(new_rows)


def _save_bots(bots: List[Dict[str, Any]]) -> None:
    SecurityManager.load()
    feishu = SecurityManager._config.setdefault("feishu", {})
    feishu["bots"] = bots
    feishu.pop("app_id", None)
    feishu.pop("app_secret", None)
    SecurityManager.save()


def list_feishu_bots() -> List[Dict[str, Any]]:
    bots = []
    for row in _list_robot_rows():
        if (row.get("platform") or "lark") != "lark":
            continue
        credentials = row.get("credentials") if isinstance(row.get("credentials"), dict) else {}
        bots.append(
            {
                "id": row.get("id") or "",
                "name": row.get("name") or "飞书机器人",
                "app_id": credentials.get("app_id") or "",
                "app_secret": credentials.get("app_secret") or "",
            }
        )
    return [_bot_public(b) for b in bots]


def get_feishu_bot(bot_id: str) -> Optional[Dict[str, Any]]:
    for bot in list_feishu_bots():
        if str(bot.get("id")) == str(bot_id):
            raw = next(
                (
                    r
                    for r in _list_robot_rows()
                    if str(r.get("id")) == str(bot_id) and (r.get("platform") or "lark") == "lark"
                ),
                None,
            )
            if raw:
                credentials = raw.get("credentials") if isinstance(raw.get("credentials"), dict) else {}
                return {
                    "id": raw.get("id") or "",
                    "name": raw.get("name") or "飞书机器人",
                    "app_id": credentials.get("app_id") or "",
                    "app_secret": credentials.get("app_secret") or "",
                }
            return bot
    return None


def get_feishu_credentials(bot_id: Optional[str] = None) -> Tuple[str, str]:
    """飞书 API 鉴权；未指定 bot_id 时使用第一个已配置机器人。"""
    bots = []
    for row in _list_robot_rows():
        if (row.get("platform") or "lark") != "lark":
            continue
        credentials = row.get("credentials") if isinstance(row.get("credentials"), dict) else {}
        bots.append(
            {
                "id": row.get("id") or "",
                "app_id": credentials.get("app_id") or "",
                "app_secret": credentials.get("app_secret") or "",
            }
        )
    target = None
    if bot_id:
        target = get_feishu_bot(bot_id)
        if not target:
            raise RuntimeError(f"未找到飞书机器人 id={bot_id}")
    else:
        for bot in bots:
            if (bot.get("app_id") or "").strip() and (bot.get("app_secret") or "").strip():
                target = bot
                break
    if target:
        return (target.get("app_id") or "").strip(), (target.get("app_secret") or "").strip()
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    return app_id, app_secret


def get_feishu_bot_settings() -> Dict[str, Any]:
    """兼容旧接口：返回首个机器人摘要。"""
    bots = list_feishu_bots()
    if not bots:
        return {
            "app_id": "",
            "app_secret_masked": "",
            "configured": False,
            "source": "none",
            "bots": [],
        }
    first = bots[0]
    return {
        "app_id": first.get("app_id", ""),
        "app_secret_masked": first.get("app_secret_masked", ""),
        "configured": first.get("configured", False),
        "source": "server",
        "bots": bots,
    }


def create_feishu_bot(*, name: str, app_id: str, app_secret: str) -> Dict[str, Any]:
    if not (app_id or "").strip():
        raise ValueError("App ID 不能为空")
    if not (app_secret or "").strip():
        raise ValueError("App Secret 不能为空")
    row = create_robot_integration(
        platform="lark",
        name=(name or "飞书机器人").strip() or "飞书机器人",
        credentials={"app_id": app_id, "app_secret": app_secret},
    )
    return {
        "id": row.get("id") or "",
        "name": row.get("name") or "飞书机器人",
        "platform": "lark",
        "platform_label": "飞书",
        "app_id": row.get("app_id") or "",
        "app_secret_masked": row.get("app_secret_masked") or "",
        "configured": row.get("configured", False),
    }


def update_feishu_bot(
    bot_id: str,
    *,
    name: Optional[str] = None,
    app_id: Optional[str] = None,
    app_secret: str = "",
    clear_secret: bool = False,
) -> Dict[str, Any]:
    credentials: Dict[str, Any] = {}
    if app_id is not None:
        credentials["app_id"] = app_id
    if app_secret and str(app_secret).strip():
        credentials["app_secret"] = app_secret
    row = update_robot_integration(
        bot_id,
        platform="lark",
        name=name,
        credentials=credentials,
        clear_secret=clear_secret,
    )
    return {
        "id": row.get("id") or "",
        "name": row.get("name") or "飞书机器人",
        "platform": "lark",
        "platform_label": "飞书",
        "app_id": row.get("app_id") or "",
        "app_secret_masked": row.get("app_secret_masked") or "",
        "configured": row.get("configured", False),
    }


def delete_feishu_bot(bot_id: str) -> None:
    delete_robot_integration(bot_id)


def save_feishu_bot_settings(
    *,
    app_id: str,
    app_secret: str = "",
    clear_secret: bool = False,
) -> Dict[str, Any]:
    """兼容旧单机器人 PUT：更新或创建 default。"""
    bots = list_feishu_bots()
    if bots:
        return update_feishu_bot(
            bots[0]["id"],
            app_id=app_id,
            app_secret=app_secret,
            clear_secret=clear_secret,
        )
    if clear_secret:
        return get_feishu_bot_settings()
    return create_feishu_bot(name="默认机器人", app_id=app_id, app_secret=app_secret)


def _testing_root() -> Dict[str, Any]:
    SecurityManager.load()
    root = SecurityManager._config.setdefault("testing", {})
    if not isinstance(root, dict):
        root = {}
        SecurityManager._config["testing"] = root
    return root


def _knowledge_entries_dir() -> Path:
    """knowledge 写入到 packs/learned（S0 后续：真正迁移读写链路）。"""
    # 保持与前端/规划一致：packs/learned/<pack>/entries/*.yaml
    return Path(APP_DATA_DIR) / "packs" / "learned" / "knowledge" / "entries"


def _load_knowledge_from_yaml() -> List[Dict[str, Any]]:
    d = _knowledge_entries_dir()
    if not d.is_dir():
        return []
    out: List[Dict[str, Any]] = []
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix.lower() not in (".yaml", ".yml"):
            continue
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        title = (data.get("title") or "").strip()
        content = (data.get("content") or "").strip()
        if not title or not content:
            continue
        out.append(dict(data))
    return out


_KNOWLEDGE_RUNTIME_KEYS = ("score", "match_pct", "used", "skip_reason")
_KNOWLEDGE_REVIEW_STATUSES = ("pending", "approved", "rejected")
_KNOWLEDGE_SOURCES = ("manual", "case_run", "task_run")


def _knowledge_review_status(raw: Dict[str, Any]) -> str:
    st = str(raw.get("review_status") or "").strip().lower()
    if st in _KNOWLEDGE_REVIEW_STATUSES:
        return st
    # 旧条目没有该字段：视为已审核，保持现网可匹配
    return "approved"


def _knowledge_source(raw: Dict[str, Any]) -> str:
    src = str(raw.get("source") or "").strip().lower()
    if src in _KNOWLEDGE_SOURCES:
        return src
    return "manual"


def _normalize_knowledge_row(raw: Dict[str, Any], *, require_body: bool = True) -> Dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = (raw.get("title") or "").strip()
    content = (raw.get("content") or "").strip()
    if require_body and (not title or not content):
        return None
    extra = {
        k: v for k, v in raw.items()
        if k not in (
            "score", "match_pct", "used", "skip_reason",
            "id", "title", "content", "category", "tags", "app_ids", "enabled",
            "source", "review_status",
        ) and v not in (None, "", [], {})
    }
    return {
        **extra,
        "id": raw.get("id") or uuid.uuid4().hex[:12],
        "title": title,
        "content": content,
        "category": (raw.get("category") or "").strip() or "其他",
        "tags": [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()],
        "app_ids": [str(a).strip() for a in (raw.get("app_ids") or []) if str(a).strip()],
        "enabled": raw.get("enabled", True) is not False,
        "source": _knowledge_source(raw),
        "review_status": _knowledge_review_status(raw),
    }


def list_testing_knowledge() -> List[Dict[str, Any]]:
    # 优先从 packs/learned 读取：实现 /settings/knowledge 写入迁移
    from_yaml = _load_knowledge_from_yaml()
    if from_yaml:
        cleaned: List[Dict[str, Any]] = []
        for raw in from_yaml:
            row = _normalize_knowledge_row(raw)
            if row:
                cleaned.append(row)
        return cleaned

    # 兼容旧数据：config.json/testing/knowledge
    root = _testing_root()
    items = root.get("knowledge") or []
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    for x in items:
        row = _normalize_knowledge_row(x) if isinstance(x, dict) else None
        if row:
            out.append(row)
    return out


def save_testing_knowledge(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    root = _testing_root()
    cleaned: List[Dict[str, Any]] = []
    for raw in items or []:
        row = _normalize_knowledge_row(raw)
        if row:
            cleaned.append(row)

    # 迁移写入：同时落到 packs/learned（learned 默认用于“系统写入”）
    try:
        d = _knowledge_entries_dir()
        d.mkdir(parents=True, exist_ok=True)
        for row in cleaned:
            kid = str(row.get("id") or "").strip()
            if not kid:
                continue
            p = d / f"{kid}.yaml"
            p.write_text(
                yaml.safe_dump(
                    {k: v for k, v in row.items()
                     if k not in _KNOWLEDGE_RUNTIME_KEYS and v not in (None, "")},
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
    except Exception as exc:
        # 不要因为 YAML 写入失败导致保存失败；config.json 仍保留（可回滚）
        SLog.w(TAG, f"knowledge yaml write failed: {type(exc).__name__}: {exc}")

    root["knowledge"] = cleaned
    SecurityManager.save()
    return cleaned


def _write_knowledge_yaml(row: Dict[str, Any]) -> None:
    """把单条知识写入 yaml 文件（有 id 才写）。"""
    kid = str(row.get("id") or "").strip()
    if not kid:
        return
    d = _knowledge_entries_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{kid}.yaml"
    p.write_text(
        yaml.safe_dump(
            {k: v for k, v in row.items()
             if k not in ("score", "match_pct", "used", "skip_reason") and v not in (None, "")},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def upsert_knowledge_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """新建或更新单条知识条目，不影响其他条目。"""
    title = (item.get("title") or "").strip()
    content = (item.get("content") or "").strip()
    if not title or not content:
        raise ValueError("标题与知识内容均不能为空")

    kid = (item.get("id") or "").strip() or uuid.uuid4().hex[:12]
    existing = next(
        (x for x in list_testing_knowledge() if str(x.get("id") or "") == kid),
        None,
    )
    merged = dict(existing or {})
    merged.update(item)
    merged["id"] = kid
    if not str(item.get("source") or "").strip():
        merged["source"] = (existing or {}).get("source") or "manual"
    if not str(item.get("review_status") or "").strip():
        if existing:
            merged["review_status"] = existing.get("review_status") or "approved"
        else:
            src = _knowledge_source(merged)
            merged["review_status"] = "approved" if src == "manual" else "pending"
    row = _normalize_knowledge_row(merged)
    if not row:
        raise ValueError("标题与知识内容均不能为空")

    # 1. 写 yaml（首选持久化路径）
    try:
        _write_knowledge_yaml(row)
    except Exception as exc:
        SLog.w(TAG, f"upsert_knowledge_item yaml write failed: {exc}")

    # 2. 同步更新 config.json（兼容旧读取路径）
    root = _testing_root()
    items: List[Dict[str, Any]] = root.get("knowledge") or []
    if not isinstance(items, list):
        items = []
    idx = next((i for i, x in enumerate(items) if isinstance(x, dict) and x.get("id") == kid), -1)
    if idx >= 0:
        items[idx] = row
    else:
        items.append(row)
    root["knowledge"] = items
    SecurityManager.save()

    return row


def delete_knowledge_item(kid: str) -> bool:
    """删除单条知识条目（yaml + config.json 同步）。返回是否找到并删除。"""
    kid = (kid or "").strip()
    if not kid:
        return False

    # 1. 删 yaml 文件
    yaml_path = _knowledge_entries_dir() / f"{kid}.yaml"
    deleted_yaml = False
    try:
        if yaml_path.exists():
            yaml_path.unlink()
            deleted_yaml = True
    except Exception as exc:
        SLog.w(TAG, f"delete_knowledge_item yaml unlink failed: {exc}")

    # 2. 从 config.json 移除
    root = _testing_root()
    items: List[Dict[str, Any]] = root.get("knowledge") or []
    if not isinstance(items, list):
        items = []
    before = len(items)
    items = [x for x in items if not (isinstance(x, dict) and x.get("id") == kid)]
    root["knowledge"] = items
    SecurityManager.save()

    return deleted_yaml or len(items) < before


def review_knowledge_item(kid: str, *, action: str, updates: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """审核通过才可被执行匹配；驳回则删除。"""
    kid = (kid or "").strip()
    act = (action or "").strip().lower()
    if act not in ("approve", "reject"):
        raise ValueError("action 必须是 approve 或 reject")
    existing = next(
        (x for x in list_testing_knowledge() if str(x.get("id") or "") == kid),
        None,
    )
    if not existing:
        raise KeyError(kid)
    if act == "reject":
        delete_knowledge_item(kid)
        return {**existing, "review_status": "rejected", "deleted": True}
    payload = dict(existing)
    if isinstance(updates, dict):
        payload.update({k: v for k, v in updates.items() if v is not None})
    payload["id"] = kid
    payload["review_status"] = "approved"
    return upsert_knowledge_item(payload)


# 原始分 25 视为 100%；低于 40% 不注入 prompt，避免低相关知识误导模型。
KNOWLEDGE_SCORE_FULL = 25
KNOWLEDGE_USE_MIN_PCT = 40
KNOWLEDGE_SKIP_REASON = "匹配度过低，未注入本步，避免误导模型"
_KNOWLEDGE_CORE_KEYS = (
    "id", "title", "content", "category", "tags", "app_ids", "enabled",
    "source", "review_status", "origin_task_id", "origin_case_id",
    *_KNOWLEDGE_RUNTIME_KEYS,
)


def _knowledge_match_pct(score: int) -> int:
    if score <= 0:
        return 0
    return max(1, min(100, round(score * 100 / KNOWLEDGE_SCORE_FULL)))


def knowledge_body_text(item: Dict[str, Any]) -> str:
    """正文 + 1.2.0 起新增的扩展字段（如 物体），供匹配与注入，避免只截 content。"""
    parts: List[str] = []
    content = str(item.get("content") or "").strip()
    if content:
        parts.append(content)
    for key, val in item.items():
        if key in _KNOWLEDGE_CORE_KEYS or val in (None, "", [], {}):
            continue
        if isinstance(val, (dict, list)):
            try:
                import json
                rendered = json.dumps(val, ensure_ascii=False)
            except Exception:
                rendered = str(val)
        else:
            rendered = str(val).strip()
        if rendered:
            parts.append(f"{key}: {rendered}")
    return "\n".join(parts)


def knowledge_prompt_snippet(item: Dict[str, Any], *, max_chars: int = 1200) -> str:
    title = str(item.get("title") or "").strip() or str(item.get("id") or "")
    body = knowledge_body_text(item).strip()
    if len(body) > max_chars:
        body = body[:max_chars].rstrip() + "…"
    return f"「{title}」: {body}" if body else f"「{title}」"


def _score_knowledge_item(item: Dict[str, Any], query: str) -> int:
    q = (query or "").strip().lower()
    if not q:
        return 0
    title = (item.get("title") or "").lower()
    body = knowledge_body_text(item).lower()
    category = (item.get("category") or "").lower()
    tags = [str(t).lower() for t in (item.get("tags") or [])]
    score = 0
    if title and title in q:
        score += 14
    elif title:
        n = 4 if len(title) >= 4 else max(2, len(title))
        hits = 0
        step = max(1, n // 2)
        for i in range(0, max(0, len(title) - n + 1), step):
            gram = title[i:i + n]
            if gram and gram in q:
                hits += 1
        score += min(12, hits * 2)
    for tag in tags:
        if len(tag) >= 2 and tag in q:
            score += 6
    if category and len(category) >= 2 and category in q:
        score += 3
    stop = {
        "点击", "输入", "勾选", "页面", "步骤", "进行", "成功", "失败",
        "登录", "打开", "关闭", "测试", "用例", "操作", "验证", "检查",
    }
    for token in re.split(r"[\s,，、/]+", q):
        if len(token) < 2 or token in stop:
            continue
        if len(token) < 3 and not re.search(r"[a-zA-Z]{3,}", token):
            continue
        local = 0
        if token in title:
            local += 8
        if token in body:
            local += 4
        if category and token in category:
            local += 5
        for tag in tags:
            if token in tag or tag in token:
                local += 6
        if local > 0:
            score += local
    return score


def match_testing_knowledge(
    text: str,
    *,
    app_id: Optional[str] = None,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """按当前上下文匹配知识条目。返回匹配度最高的前 N 条（默认 3），带 score / match_pct / used。

    used=False 的条目仍返回给前端展示，但不应写入模型 prompt。
    """
    query = (text or "").strip()
    ranked: List[tuple[int, Dict[str, Any]]] = []
    for item in list_testing_knowledge():
        if item.get("enabled") is False:
            continue
        if _knowledge_review_status(item) != "approved":
            continue
        app_ids = item.get("app_ids") or []
        if app_ids and app_id and str(app_id) not in [str(x) for x in app_ids]:
            continue
        ranked.append((_score_knowledge_item(item, query), item))
    ranked.sort(key=lambda x: x[0], reverse=True)
    out: List[Dict[str, Any]] = []
    for score, item in ranked[: max(1, int(limit or 3))]:
        pct = _knowledge_match_pct(score)
        used = pct >= KNOWLEDGE_USE_MIN_PCT
        row = dict(item)
        row["score"] = score
        row["match_pct"] = pct
        row["used"] = used
        row["skip_reason"] = "" if used else KNOWLEDGE_SKIP_REASON
        out.append(row)
    return out


def get_figma_access_token() -> str:
    root = _testing_root()
    figma = root.get("figma") or {}
    if not isinstance(figma, dict):
        return ""
    return (figma.get("access_token") or "").strip()


def get_figma_settings() -> Dict[str, Any]:
    root = _testing_root()
    figma = root.get("figma") or {}
    if not isinstance(figma, dict):
        figma = {}
    token = get_figma_access_token()
    return {
        "access_token_masked": _mask_secret(token),
        "configured": bool(token),
        "default_file_url": (figma.get("default_file_url") or "").strip(),
    }


def save_figma_settings(
    *,
    access_token: str = "",
    clear_token: bool = False,
    default_file_url: str = "",
) -> Dict[str, Any]:
    root = _testing_root()
    figma = root.setdefault("figma", {})
    if clear_token:
        figma.pop("access_token", None)
    elif access_token and str(access_token).strip():
        figma["access_token"] = str(access_token).strip()
    if default_file_url is not None:
        figma["default_file_url"] = str(default_file_url or "").strip()
    SecurityManager.save()
    return get_figma_settings()
