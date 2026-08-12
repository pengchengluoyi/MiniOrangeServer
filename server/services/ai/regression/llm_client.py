# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""纯文本 OpenAI-compatible chat 客户端（不带截图）。

为什么不复用 copilot_service._call_openai_compatible_plan？
  - 那个 caller 强绑定 copilot 的坐标 / 截图机制（_append_openai_image / preview 提示）
  - regression 的 PLAN_OVERVIEW_TEXT、SINGLE_STEP_REPLAN 是文本-only，强行复用会塞一堆图相关 prompt 噪音

支持：
  - Volcengine Doubao（关 thinking）/ OpenAI / 任何 OpenAI-compatible endpoint
  - 通过 system_settings_service.get_ai_provider_credentials 拿 provider
  - 返回解析好的 JSON dict + parse_meta；解析失败返回 (None, meta)
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from script.log import SLog

TAG = "RegressionLLM"


# ---------- JSON 抽取 ----------


_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_first_json_object(text: str) -> Optional[dict[str, Any]]:
    """从 LLM 文本里抠出第一个完整 JSON 对象（容错 Markdown / 思考链外泄）。"""
    if not text:
        return None
    text = text.strip()
    # 直接尝试解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 剥掉 ```json ... ``` 包裹
    if text.startswith("```"):
        text = re.sub(r"^```(?:json|JSON)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except Exception:
            pass
    # 找第一个 { ... } 段
    match = _JSON_OBJECT_RE.search(text)
    if not match:
        return None
    candidate = match.group(0)
    try:
        return json.loads(candidate)
    except Exception:
        # 进一步用栈匹配抠出第一个平衡 JSON
        depth = 0
        start = -1
        for i, ch in enumerate(text):
            if ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start >= 0:
                    snippet = text[start : i + 1]
                    try:
                        return json.loads(snippet)
                    except Exception:
                        start = -1
                        continue
        return None


def _parse_openai_chat_completion(resp_json: dict[str, Any]) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """从 OpenAI-compatible response 取 content 并解析成 JSON dict。"""
    if not isinstance(resp_json, dict):
        return None, {"reason": "non-dict response"}
    choices = resp_json.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None, {"reason": "no choices", "raw_keys": list(resp_json.keys())[:8]}
    choice = choices[0]
    message = choice.get("message") or {}
    content = ""
    if isinstance(message, dict):
        content = message.get("content") or ""
        if isinstance(content, list):
            # vision 风格 multi-part；只取文本块
            content = "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
    if not content:
        # 兜底：text 字段
        content = choice.get("text") or ""
    content = str(content or "")
    parsed = _extract_first_json_object(content)
    meta = {
        "finish_reason": choice.get("finish_reason"),
        "content_len": len(content),
        "content_preview": content[:240],
        "usage": resp_json.get("usage"),
    }
    return parsed, meta


# ---------- HTTP 调用 ----------


def _is_volcengine_doubao(provider_id: str = "", model: str = "") -> bool:
    pid = (provider_id or "").strip().lower()
    mid = (model or "").strip().lower()
    if pid == "volcengine":
        return True
    return "doubao" in mid


def _volcengine_extras(provider_id: str = "", model: str = "") -> dict[str, Any]:
    """方舟关 thinking，避免推理链污染 JSON。"""
    if _is_volcengine_doubao(provider_id, model):
        return {"thinking": {"type": "disabled"}}
    return {}


def call_chat_text(
    *,
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    temperature: float = 0.1,
    max_tokens: int = 4096,
    timeout_sec: int = 90,
    extra_payload: Optional[dict[str, Any]] = None,
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """OpenAI-compatible /chat/completions 调用。

    入参 provider 至少要有 base_url / api_key / model。
    返回 (parsed_json_dict | None, meta)。meta 里包含：
      - elapsed_ms, http_status, finish_reason, content_len, content_preview, usage, error
    """
    base = str(provider.get("base_url") or "").rstrip("/")
    api_key = str(provider.get("api_key") or "").strip()
    model = str(provider.get("model") or "").strip()
    pid = str(provider.get("id") or "")
    meta: dict[str, Any] = {
        "provider_id": pid,
        "model": model,
        "http_status": 0,
        "elapsed_ms": 0,
        "error": "",
    }
    if not base or not api_key or not model:
        meta["error"] = f"provider not configured (base={bool(base)}, key={bool(api_key)}, model={bool(model)})"
        return None, meta

    # 延迟 import，避免无网调用环境的 requests 找不到
    try:
        import requests
    except Exception as e:
        meta["error"] = f"requests import failed: {e}"
        return None, meta

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        **_volcengine_extras(pid, model),
    }
    if extra_payload:
        payload.update(extra_payload)

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    url = f"{base}/chat/completions"

    started = time.time()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=timeout_sec)
        meta["http_status"] = resp.status_code
        meta["elapsed_ms"] = int((time.time() - started) * 1000)
        resp.raise_for_status()
        resp_json = resp.json()
    except Exception as e:
        meta["elapsed_ms"] = int((time.time() - started) * 1000)
        meta["error"] = f"http: {e!s}"[:240]
        SLog.w(TAG, f"chat call failed provider={pid} model={model}: {meta['error']}")
        return None, meta

    parsed, parse_meta = _parse_openai_chat_completion(resp_json)
    meta.update(parse_meta)
    if parsed is None:
        SLog.w(
            TAG,
            f"chat JSON parse failed provider={pid} model={model} "
            f"finish={meta.get('finish_reason')!r} preview={meta.get('content_preview')!r}",
        )
    return parsed, meta


# ---------- Provider 解析快捷方法 ----------


def resolve_regression_provider(provider_id: Optional[str] = None) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """读 system settings 解析本次 Run 该用哪个 AI provider。

    CaseRunner 回归固定走「大模型 Key → 可用 + 用例」那条配置，忽略外部传入的 provider_id。
    返回 (provider_dict | None, gate_dict)。
    """
    try:
        from server.services import system_settings_service as ss
    except Exception as e:
        return None, {"enabled": False, "reason": f"system_settings import failed: {e}"}

    gate = ss.should_use_ai_planning("case_execution", provider_id=None)
    if not gate.get("enabled"):
        return None, {"enabled": False, "reason": gate.get("reason") or "ai planning gate disabled", **(gate or {})}
    selected = (gate.get("provider") or {}).get("id") or ss.find_case_execution_provider_id() or ""
    provider = ss.get_ai_provider_credentials(selected)
    if not provider.get("configured") or not provider.get("api_key"):
        return None, {"enabled": False, "reason": "provider missing api_key", "provider_id": selected}
    if not provider.get("enabled"):
        return None, {"enabled": False, "reason": f"AI provider disabled: {selected}", "provider_id": selected}
    if not provider.get("case_execution_use"):
        return None, {
            "enabled": False,
            "reason": "未找到「可用 + 用例」的大模型（请到密钥配置 → 大模型 Key 设置）",
            "provider_id": selected,
        }
    return provider, {
        "enabled": True,
        "provider_id": provider.get("id"),
        "model": provider.get("model"),
        "provider_name": provider.get("name"),
    }
