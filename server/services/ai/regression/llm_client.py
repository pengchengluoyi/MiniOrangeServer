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
import random
import re
import time
from typing import Any, Optional

from script.log import SLog

TAG = "RegressionLLM"

# ---------- 重试与超时策略 ----------
# 只对「可能自愈」的失败重试：限流、网关抖动、读超时、JSON 解析失败。
# 业务错误（401/403/404/422）不重试，重试只会浪费额度。
# max_tokens 截断不在这里重试（同预算再打一次几乎必然再截断；由调用方拆批 / 加预算）。
_RETRY_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_PARSE_RETRIES = 1  # 解析失败后再完整打一轮（填点乱码 JSON 这类偶发花活）
_CONNECT_TIMEOUT_SEC = 10.0
# 读超时下限：按 max_tokens 估生成耗时（经验值 ~25 token/s），再取和调用方给的 timeout_sec 的较大值。
# 这样老调用方不用改参数也能自动获得足够的读超时，避免「4096 token 生成撞 90s 超时」。
_READ_TIMEOUT_PER_TOKEN = 1.0 / 25.0
_READ_TIMEOUT_MAX_SEC = 600.0


def _safe_record_llm(*, messages, parsed=None, raw_text: str = "", meta=None) -> None:
    try:
        from server.services.ai.dispatch_log import record_llm

        record_llm(messages=messages, parsed=parsed, raw_text=raw_text, meta=meta)
    except Exception:
        pass


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


def _salvage_truncated_json(text: str) -> Optional[dict[str, Any]]:
    """抢救被 max_tokens 截断的 JSON：丢掉最后一个不完整的元素，补齐括号。

    为什么需要：`_extract_first_json_object` 靠括号配平，截断的输出永远配不平，
    整批结果会被丢掉（一批 8 个测试点的用例全变模板桩）。截断时前面的元素通常是完整的，
    能救回来多少就算多少，剩下的交给调用方按「没覆盖到」重试。

    返回 None 表示连一个完整元素都没有。
    """
    s = str(text or "").strip()
    start = s.find("{")
    if start < 0:
        return None

    stack: list[str] = []
    in_str = False
    esc = False
    cut = -1                      # 可安全截断的位置（不含）
    cut_stack: Optional[list[str]] = None

    def mark(pos: int) -> tuple[int, list[str]]:
        return pos, list(stack)

    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch in "{[":
            stack.append(ch)
            continue
        if ch in "}]":
            if not stack:
                break
            stack.pop()
            if stack:
                # 刚闭合一个嵌套值，且外层还开着 —— 切在这里一定是完整元素边界
                cut, cut_stack = mark(i + 1)
            else:
                # 整个对象闭合了，说明并没有截断
                try:
                    return json.loads(s[start : i + 1])
                except Exception:
                    pass
            continue
        if ch == "," and stack:
            # 只在「完整元素边界」切，否则会留下 {"case_id":"c"} 这种残缺元素
            # —— 那正是要消灭的静默垃圾。
            #   · stack[-1] == "["  → 数组元素之间，前一个元素一定完整
            #   · len(stack) == 1   → 根对象的顶层字段之间，前一个 key:value 一定完整
            # 嵌套对象内部的逗号（len>1 且 stack[-1]=="{"）一律不切。
            if stack[-1] == "[" or len(stack) == 1:
                cut, cut_stack = mark(i)

    if cut < 0 or not cut_stack:
        return None
    repaired = s[start:cut] + "".join("}" if c == "{" else "]" for c in reversed(cut_stack))
    try:
        return json.loads(repaired)
    except Exception:
        return None


def _first_root_array_span(text: str) -> Optional[tuple[int, int]]:
    """根对象里第一个顶层数组的 [start, end) 下标（含括号）。找不到返回 None。"""
    s = str(text or "")
    brace = s.find("{")
    if brace < 0:
        return None
    in_str = False
    esc = False
    depth_obj = 0
    depth_arr = 0
    arr_start = -1
    for i in range(brace, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth_obj += 1
            continue
        if ch == "}":
            depth_obj = max(0, depth_obj - 1)
            continue
        if ch == "[":
            if depth_arr == 0 and depth_obj == 1 and arr_start < 0:
                arr_start = i
            depth_arr += 1
            continue
        if ch == "]":
            if depth_arr <= 0:
                continue
            depth_arr -= 1
            if depth_arr == 0 and arr_start >= 0:
                return arr_start, i + 1
    if arr_start >= 0:
        # 数组未闭合（截断）：仍返回起点，交给后续截断抢救
        return arr_start, len(s)
    return None


def _guess_array_key(items: list) -> str:
    """根据数组元素形状猜根字段名。"""
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("case_id") or it.get("steps") is not None:
            return "cases"
        if it.get("text") is not None or it.get("kind") is not None:
            return "points"
    return "points"


def _salvage_mangled_root_array(text: str) -> Optional[dict[str, Any]]:
    """抢救根键名被写烂、但后面仍是对象数组的输出。

    真实事故形态（fill_points）：
      { "\\n\\n  : [ {"text":"...","kind":"正向",...}, ... ] }
    `"points"` 键碎掉后整段无法 json.loads，截断抢救也配不平；
    把第一个顶层数组抠出来再包回 {"points":[...]} / {"cases":[...]} 即可。
    """
    span = _first_root_array_span(text)
    if not span:
        return None
    a, b = span
    chunk = str(text)[a:b]
    items: Optional[list] = None
    try:
        parsed = json.loads(chunk)
        if isinstance(parsed, list):
            items = parsed
    except Exception:
        # 数组本身被截断：补成 {"points": [...完整元素] }
        wrapped = '{"points":' + chunk
        salvaged = _salvage_truncated_json(wrapped)
        if isinstance(salvaged, dict) and isinstance(salvaged.get("points"), list):
            items = salvaged["points"]
    if not isinstance(items, list) or not items:
        return None
    # 只要有一个像样的对象元素才认，避免把随便一个 `[1,2` 当成结果
    if not any(isinstance(x, dict) and (x.get("text") is not None or x.get("case_id") is not None) for x in items):
        return None
    key = _guess_array_key(items)
    return {key: items}


def extract_chat_content(resp_json: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """从 OpenAI-compatible response 取出 assistant 文本。"""
    if not isinstance(resp_json, dict):
        return "", {"reason": "non-dict response"}
    choices = resp_json.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return "", {"reason": "no choices", "raw_keys": list(resp_json.keys())[:8]}
    choice = choices[0]
    message = choice.get("message") or {}
    content = ""
    if isinstance(message, dict):
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
    if not content:
        content = choice.get("text") or ""
    content = str(content or "")
    meta = {
        "finish_reason": choice.get("finish_reason"),
        "content_len": len(content),
        "content_preview": content[:240],
        "usage": resp_json.get("usage"),
        **parse_token_usage(resp_json.get("usage")),
    }
    return content, meta


def parse_token_usage(usage: Any) -> dict[str, int]:
    """兼容 OpenAI / 方舟 usage 字段。"""
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    total = int(usage.get("total_tokens") or 0) or (prompt + completion)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _parse_openai_chat_completion(resp_json: dict[str, Any]) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """从 OpenAI-compatible response 取 content 并解析成 JSON dict。"""
    content, meta = extract_chat_content(resp_json)
    if not content and meta.get("reason"):
        return None, meta
    parsed = _extract_first_json_object(content)
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


def _response_format(json_mode: bool, response_schema: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """优先 json_schema（能约束枚举和必填），provider 不支持时降级 json_object。"""
    if response_schema:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": str(response_schema.get("title") or "output"),
                "strict": False,
                "schema": response_schema,
            },
        }
    if json_mode:
        return {"type": "json_object"}
    return None


def _read_timeout(max_tokens: int, timeout_sec: float) -> float:
    est = max_tokens * _READ_TIMEOUT_PER_TOKEN + 30.0
    return float(min(_READ_TIMEOUT_MAX_SEC, max(float(timeout_sec or 0), est)))


def _post_chat_completions(
    *,
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    temperature: float = 0.1,
    max_tokens: int = 4096,
    timeout_sec: int = 90,
    extra_payload: Optional[dict[str, Any]] = None,
    json_mode: bool = False,
    response_schema: Optional[dict[str, Any]] = None,
    max_attempts: int = _MAX_ATTEMPTS,
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """OpenAI-compatible /chat/completions HTTP 调用。成功返回 (resp_json, meta)，失败 (None, meta)。

    对限流 / 网关抖动 / 读超时做指数退避重试（`_RETRY_STATUS`）；业务错误不重试。
    provider 不认 `response_format` 时（400/422）自动摘掉该字段重试一次，避免因为
    加了 JSON 模式反而把原本能用的 provider 打挂。
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
        "attempts": 0,
        "retry_reasons": [],
        "json_mode_downgraded": False,
    }
    if not base or not api_key or not model:
        meta["error"] = f"provider not configured (base={bool(base)}, key={bool(api_key)}, model={bool(model)})"
        return None, meta

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
    fmt = _response_format(json_mode, response_schema)
    if fmt:
        payload["response_format"] = fmt
    if extra_payload:
        payload.update(extra_payload)

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    url = f"{base}/chat/completions"
    timeout = (_CONNECT_TIMEOUT_SEC, _read_timeout(max_tokens, timeout_sec))

    started = time.time()
    attempt = 0
    while attempt < max(1, max_attempts):
        attempt += 1
        meta["attempts"] = attempt
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            meta["http_status"] = resp.status_code
            if resp.status_code in (400, 422) and "response_format" in payload:
                # provider 不支持 response_format：摘掉重试，不算入退避次数
                payload.pop("response_format", None)
                meta["json_mode_downgraded"] = True
                meta["retry_reasons"].append(f"{resp.status_code}:response_format_unsupported")
                SLog.w(TAG, f"provider={pid} model={model} 不支持 response_format，已降级重试")
                attempt -= 1
                continue
            if resp.status_code in _RETRY_STATUS and attempt < max_attempts:
                meta["retry_reasons"].append(str(resp.status_code))
                time.sleep(_backoff_delay(attempt))
                continue
            resp.raise_for_status()
            meta["elapsed_ms"] = int((time.time() - started) * 1000)
            return resp.json(), meta
        except Exception as e:
            transient = isinstance(e, (requests.Timeout, requests.ConnectionError))
            if transient and attempt < max_attempts:
                meta["retry_reasons"].append(type(e).__name__)
                time.sleep(_backoff_delay(attempt))
                continue
            meta["elapsed_ms"] = int((time.time() - started) * 1000)
            meta["error"] = f"http: {e!s}"[:240]
            SLog.w(
                TAG,
                f"chat call failed provider={pid} model={model} attempts={attempt}: {meta['error']}",
            )
            return None, meta

    meta["elapsed_ms"] = int((time.time() - started) * 1000)
    meta["error"] = meta["error"] or f"exhausted {max_attempts} attempts: {meta['retry_reasons']}"
    return None, meta


def _backoff_delay(attempt: int) -> float:
    """1s、3s 起步，带抖动 —— 并发分片同时撞限流时不要一起重试。"""
    return 1.0 * (3 ** (attempt - 1)) + random.uniform(0.0, 0.5)


def call_chat_plain(
    *,
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    temperature: float = 0.3,
    max_tokens: int = 2048,
    timeout_sec: int = 90,
    extra_payload: Optional[dict[str, Any]] = None,
) -> tuple[str, dict[str, Any]]:
    """纯文本对话。返回 (assistant_text, meta)，失败时文本为空且 meta.error 有值。"""
    resp_json, meta = _post_chat_completions(
        provider=provider,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        extra_payload=extra_payload,
    )
    if resp_json is None:
        _safe_record_llm(messages=messages, meta=meta)
        return "", meta
    content, parse_meta = extract_chat_content(resp_json)
    meta.update(parse_meta)
    if not content:
        meta["error"] = meta.get("error") or meta.get("reason") or "empty assistant content"
    _safe_record_llm(messages=messages, raw_text=content, meta=meta)
    return content, meta


def _parse_chat_json(resp_json: dict[str, Any], meta: dict[str, Any]) -> tuple[Optional[dict[str, Any]], str]:
    """从一次成功的 chat 响应里解析 JSON。返回 (parsed|None, content)。

    会就地写 meta：finish_reason / truncated / salvaged / content_* / usage / fail_kind / error。
    """
    content, parse_meta = extract_chat_content(resp_json)
    meta.update(parse_meta)
    meta["truncated"] = str(meta.get("finish_reason") or "") == "length"
    meta["salvaged"] = False
    meta["fail_kind"] = ""
    meta["error"] = meta.get("error") or ""

    parsed = _extract_first_json_object(content)
    if parsed is None and content:
        parsed = _salvage_truncated_json(content)
        if parsed is None:
            parsed = _salvage_mangled_root_array(content)
        if parsed is not None:
            meta["salvaged"] = True
            SLog.w(
                TAG,
                f"chat JSON 已抢救 provider={meta.get('provider_id')} model={meta.get('model')} "
                f"finish={meta.get('finish_reason')!r} len={meta.get('content_len')} "
                f"keys={list(parsed)[:6]}",
            )

    if parsed is None:
        meta["fail_kind"] = "truncated" if meta["truncated"] else "parse"
        SLog.w(
            TAG,
            f"chat JSON parse failed provider={meta.get('provider_id')} model={meta.get('model')} "
            f"finish={meta.get('finish_reason')!r} preview={meta.get('content_preview')!r}",
        )
        meta["error"] = meta.get("error") or (
            "输出被 max_tokens 截断且无法抢救" if meta["truncated"] else "模型没有返回可解析的 JSON"
        )
    return parsed, content


def _merge_round_meta(acc: dict[str, Any], round_meta: dict[str, Any]) -> dict[str, Any]:
    """把多轮 HTTP/解析的 meta 叠到一起：attempts 累加、elapsed 累加、retry_reasons 拼接。"""
    if not acc:
        out = dict(round_meta)
        out["retry_reasons"] = list(round_meta.get("retry_reasons") or [])
        return out
    out = dict(round_meta)
    out["attempts"] = int(acc.get("attempts") or 0) + int(round_meta.get("attempts") or 0)
    out["elapsed_ms"] = int(acc.get("elapsed_ms") or 0) + int(round_meta.get("elapsed_ms") or 0)
    out["retry_reasons"] = list(acc.get("retry_reasons") or []) + list(round_meta.get("retry_reasons") or [])
    out["json_mode_downgraded"] = bool(acc.get("json_mode_downgraded")) or bool(
        round_meta.get("json_mode_downgraded")
    )
    return out


def call_chat_text(
    *,
    provider: dict[str, Any],
    messages: list[dict[str, str]],
    temperature: float = 0.1,
    max_tokens: int = 4096,
    timeout_sec: int = 90,
    extra_payload: Optional[dict[str, Any]] = None,
    json_mode: bool = True,
    response_schema: Optional[dict[str, Any]] = None,
    max_attempts: int = _MAX_ATTEMPTS,
    parse_retries: int = _PARSE_RETRIES,
) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """OpenAI-compatible /chat/completions 调用，返回解析好的 JSON dict。

    入参 provider 至少要有 base_url / api_key / model。
    返回 (parsed_json_dict | None, meta)。meta 里包含：
      - elapsed_ms, http_status, finish_reason, content_len, content_preview, usage, error
      - attempts / retry_reasons：重试了几次、为什么（含 parse）
      - truncated：是否被 max_tokens 截断（finish_reason=length）
      - salvaged：截断后是否靠抢救解析拿回了部分结果
      - json_mode_downgraded：provider 不支持 response_format 已降级

    调用方**必须**看 truncated / salvaged：截断意味着结果不完整，
    不能当成功处理（这是「一批用例静默变模板桩」的根因）。

    解析失败（非截断）会再完整打一轮模型：偶发乱码 JSON（键名碎掉之类）常能自愈；
    max_tokens 截断不在此重试。
    """
    meta: dict[str, Any] = {}
    parsed: Optional[dict[str, Any]] = None
    rounds = 1 + max(0, int(parse_retries))

    for round_i in range(rounds):
        resp_json, round_meta = _post_chat_completions(
            provider=provider,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_sec=timeout_sec,
            extra_payload=extra_payload,
            json_mode=json_mode,
            response_schema=response_schema,
            max_attempts=max_attempts,
        )
        meta = _merge_round_meta(meta, round_meta)
        if resp_json is None:
            meta["fail_kind"] = "http"
            _safe_record_llm(messages=messages, meta=meta)
            return None, meta

        parsed, content = _parse_chat_json(resp_json, meta)
        if parsed is not None:
            _safe_record_llm(messages=messages, parsed=parsed, meta=meta)
            return parsed, meta

        # 截断：同预算再打没用；交给调用方拆批 / 加 tokens
        if meta.get("truncated") or round_i + 1 >= rounds:
            break

        meta["retry_reasons"] = list(meta.get("retry_reasons") or []) + ["parse"]
        SLog.w(
            TAG,
            f"chat JSON parse retry provider={meta.get('provider_id')} model={meta.get('model')} "
            f"round={round_i + 1}/{rounds} preview={meta.get('content_preview')!r}",
        )
        # 留下失败轮次的原文，便于对照 dispatch 日志里「第一次乱码、第二次修好」
        _safe_record_llm(messages=messages, raw_text=content, meta={**meta, "status_hint": "parse_retry"})
        time.sleep(_backoff_delay(round_i + 1))

    _safe_record_llm(messages=messages, parsed=parsed, meta=meta)
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
