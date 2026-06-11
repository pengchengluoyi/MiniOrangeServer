# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""用例预期与页面识别结果的语义一致性判断（规则 + 可选大模型）。"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

import requests

from script.log import SLog

TAG = "ExpectSemantic"

_NAV_PREFIX_RE = re.compile(r"^(进入|打开|跳转至?|导航至?|到达|切换至?|切到)\s*", re.I)
_SUFFIX_RE = re.compile(r"(列表|页面|界面|成功|页)$")

_HOME_ALIASES = frozenset(
    {
        "首页",
        "主页",
        "app首页",
        "app 首页",
        "app home",
        "home",
        "feed",
        "首页feed",
        "app首页feed",
        "进入app首页",
        "进入首页",
        "进入 app 首页",
    }
)

_LOGIN_PAGE_ALIASES = frozenset(
    {
        "登录注册页",
        "登录页",
        "登录弹窗",
        "弹出登录弹窗",
        "登录界面",
        "登录窗口",
        "login",
    }
)


def normalize_page_intent(text: str) -> str:
    """把「进入app首页」类表述归一为「首页」等短标签。"""
    s = (text or "").strip()
    if not s:
        return ""
    s = _NAV_PREFIX_RE.sub("", s)
    s = re.sub(r"^app\s*", "", s, flags=re.I)
    s = _SUFFIX_RE.sub("", s).strip()
    low = s.lower().replace(" ", "")
    if low in ("home", "feed", "app首页", "apphome", "首页feed"):
        return "首页"
    if "首页" in s or s == "首":
        return "首页"
    if s in ("我的", "个人中心", "我"):
        return "我的"
    if s in ("消息", "通知"):
        return "消息"
    if "手机号" in s and re.search(r"登录|登陆", s, re.I):
        return "手机号登录页"
    if re.search(r"登录|登陆|sign\s*in", s, re.I) or "登录弹窗" in s or "登录页" in s:
        return "登录注册页"
    return s.strip()


def _token_variants(text: str) -> List[str]:
    base = (text or "").strip()
    if not base:
        return []
    out: List[str] = []
    seen = set()

    def _add(v: str) -> None:
        v = (v or "").strip()
        if len(v) < 1 or v in seen:
            return
        seen.add(v)
        out.append(v)

    _add(base)
    norm = normalize_page_intent(base)
    _add(norm)
    _add(re.sub(r"^app", "", base, flags=re.I).strip())
    if "首页" in base:
        _add("首页")
    return out


def _is_home_intent(norm: str, raw: str) -> bool:
    if norm == "首页" or norm in _HOME_ALIASES:
        return True
    low = (raw or "").lower().replace(" ", "")
    return "首页" in (raw or "") or low in ("home", "feed", "apphome", "app首页")


def _is_login_intent(norm: str, raw: str) -> bool:
    if norm == "登录注册页":
        return True
    if any(k in (raw or "") for k in _LOGIN_PAGE_ALIASES):
        return True
    low = (raw or "").lower().replace(" ", "")
    return "登录" in (raw or "") or "login" in low


def pages_semantically_match(expected: str, current_label: str) -> bool:
    """判断预期页面描述与当前页标签是否同一语义。"""
    exp = (expected or "").strip()
    cur = (current_label or "").strip()
    if not exp or not cur:
        return False

    exp_norm = normalize_page_intent(exp)
    cur_norm = normalize_page_intent(cur)
    if exp_norm and cur_norm and exp_norm == cur_norm:
        return True

    exp_home = _is_home_intent(exp_norm, exp)
    cur_home = _is_home_intent(cur_norm, cur)
    exp_login = _is_login_intent(exp_norm, exp)
    cur_login = _is_login_intent(cur_norm, cur)
    if exp_home and cur_login:
        return False
    if exp_login and cur_home:
        return False
    if exp_home and cur_home:
        return True

    exp_phone = "手机号" in exp and "登录" in exp
    cur_phone = "手机号" in cur or "手机号登录" in cur
    if exp_phone and (cur_phone or cur_norm == "手机号登录页"):
        return True

    if "造物秀" in exp and "造物秀" in cur:
        return True
    if "造物秀" in exp and "造物秀" in (current_label or ""):
        return True

    exp_low = exp.lower().replace(" ", "")
    cur_low = cur.lower().replace(" ", "")
    if exp_low == cur_low:
        return True
    if cur in exp or exp in cur:
        return True
    if cur_norm and cur_norm in exp_norm:
        return True
    if exp_norm and exp_norm in cur_norm:
        return True

    exp_vars = set(_token_variants(exp))
    cur_vars = set(_token_variants(cur))
    if exp_vars & cur_vars:
        return True

    if "consent" in cur.lower() and ("登录" in exp or exp_norm == "登录注册页"):
        return False

    if exp_login and cur_login:
        return True

    return False


def _llm_configured() -> bool:
    key = (os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    base = (os.environ.get("LLM_API_BASE") or os.environ.get("OPENAI_BASE_URL") or "").strip()
    return bool(key and base)


def _llm_judge_page_expectation(
    expected: str,
    current_label: str,
    *,
    screen_text: str = "",
    page_score: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """调用 OpenAI 兼容接口判断预期与当前页是否一致。"""
    if not _llm_configured():
        return None

    api_key = (os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    base = (os.environ.get("LLM_API_BASE") or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    model = (os.environ.get("LLM_MODEL") or "qwen-plus").strip()
    preview = (screen_text or "").strip().replace("\n", " ")[:400]

    system = (
        "你是移动 App 自动化测试的断言助手。"
        "判断「用户预期结果」与「当前识别页面」在业务上是否一致。"
        "例如「进入app首页」与当前页「首页」应判为一致。"
        "只输出 JSON：{\"match\": true/false, \"reason\": \"一句话\"}"
    )
    user = json.dumps(
        {
            "expected": expected,
            "current_page": current_label,
            "page_confidence": page_score,
            "screen_text_preview": preview,
        },
        ensure_ascii=False,
    )

    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0,
                "max_tokens": 200,
            },
            timeout=25,
        )
        if not resp.ok:
            SLog.w(TAG, f"LLM judge HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        start = content.find("{")
        end = content.rfind("}") + 1
        if start < 0 or end <= start:
            return None
        data = json.loads(content[start:end])
        if "match" not in data:
            return None
        return {
            "ok": bool(data.get("match")),
            "reason": (data.get("reason") or "").strip()
            or ("语义一致" if data.get("match") else "语义不一致"),
            "method": "llm",
        }
    except Exception as e:
        SLog.w(TAG, f"LLM judge failed: {e}")
        return None


def evaluate_dynamic_expectation(
    expected: str,
    screen_text: str,
    *,
    step_results: Optional[List[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    """
    非固定页面的预期判定：文案变化、数量、增减等。
    返回 {ok, reason, method} 或 None（无法判定，交下游）。
    """
    exp = (expected or "").strip()
    blob = screen_text or ""
    if not exp or not blob:
        return None

    # 变成 / 变为 / 显示为 / 文案为 / 提示为
    m = re.search(
        r"(?:变成|变为|显示为|文案为|提示为|展示为)[「\"'']?(.+?)[」\"'']?\s*$",
        exp,
    )
    if m:
        target = m.group(1).strip()
        if target and target in blob:
            return {"ok": True, "reason": f"界面文案已包含「{target}」", "method": "text_change"}
        return {
            "ok": False,
            "reason": f"未在界面中找到预期文案「{target}」",
            "method": "text_change",
        }

    # 不包含 / 不再显示
    m = re.search(r"(?:不包含|不再显示|没有)[「\"'']?(.+?)[」\"'']?", exp)
    if m:
        forbidden = m.group(1).strip()
        if forbidden and forbidden not in blob:
            return {"ok": True, "reason": f"界面未出现「{forbidden}」", "method": "text_absent"}
        if forbidden:
            return {"ok": False, "reason": f"界面仍包含「{forbidden}」", "method": "text_absent"}

    # 数量为 N / 是 N 个 / 共 N
    m = re.search(r"(?:数量|个数|)(?:为|是|共)\s*(\d+)\s*(?:个|条|项)?", exp)
    if m:
        want = int(m.group(1))
        nums = [int(x) for x in re.findall(r"\b(\d+)\b", blob)]
        if want in nums:
            return {"ok": True, "reason": f"界面可见数量 {want}", "method": "numeric"}
        return {
            "ok": False,
            "reason": f"界面未找到数量 {want}（OCR 数字: {nums[:8]}）",
            "method": "numeric",
        }

    # +1 / 增加1 / 加1（需操作成功且界面有数字变化迹象）
    if re.search(r"\+1|增加\s*1|加\s*1|多\s*1", exp):
        steps_ok = all(r.get("ok") for r in (step_results or [])) if step_results else True
        if steps_ok and re.search(r"\b\d+\b", blob):
            return {
                "ok": True,
                "reason": "操作已成功，界面含计数类数字（+1 类预期需结合业务字段扩展）",
                "method": "delta_hint",
            }
        if not steps_ok:
            return {"ok": False, "reason": "前置操作未成功，无法校验增减", "method": "delta_hint"}

    # 原来是 X 现在是 Y
    m = re.search(
        r"原来(?:是|为)?[「\"'']?(.+?)[」\"'']?\s*(?:现在|变为|变成)(?:是|为)?[「\"'']?(.+?)[」\"'']?\s*$",
        exp,
    )
    if m:
        now_val = m.group(2).strip()
        if now_val and now_val in blob:
            return {"ok": True, "reason": f"界面已变为「{now_val}」", "method": "state_change"}
        return {"ok": False, "reason": f"界面未出现「{now_val}」", "method": "state_change"}

    # 切换/进入某页面（如「切换到手机号登录页面」）
    m = re.search(
        r"(?:切换到|切换至?|切到|进入|打开|跳转至?|导航至?)(.+?)(?:页面|页)?\s*$",
        exp,
    )
    if m:
        target = re.sub(r"^[「\"'\s]+|[」\"'\s]+$", "", m.group(1).strip())
        candidates = [target]
        if target and not target.endswith("页"):
            candidates.extend([f"{target}页", f"{target}页面"])
        if any(c and c in blob for c in candidates):
            hit = next(c for c in candidates if c and c in blob)
            return {"ok": True, "reason": f"界面已进入「{hit}」", "method": "page_nav"}

    return None


def judge_navigation_expectation(
    expected: str,
    page_ctx: Dict[str, Any],
    *,
    screen_text: str = "",
    use_llm: bool = True,
) -> Optional[Dict[str, Any]]:
    """
    导航类预期 vs 页面识别结果。
    先规则语义匹配，可选再走大模型。
    """
    if not page_ctx.get("matched"):
        return None
    label = (page_ctx.get("label") or page_ctx.get("figma_best") or "").strip()
    if not label:
        return None

    score = page_ctx.get("score") or page_ctx.get("figma_score")
    src = "Figma 设计稿" if page_ctx.get("method") == "figma_text" else "应用图谱"

    try:
        pct = f"{float(score):.0%}" if score is not None else ""
    except (TypeError, ValueError):
        pct = ""

    if pages_semantically_match(expected, label):
        reason = f"语义匹配：预期「{expected}」≈ 当前「{label}」"
        if pct:
            reason += f"（{src} {pct}）"
        return {"ok": True, "reason": reason, "method": "semantic"}

    if use_llm:
        llm = _llm_judge_page_expectation(
            expected,
            label,
            screen_text=screen_text,
            page_score=float(score) if score is not None else None,
        )
        if llm is not None:
            if llm.get("ok"):
                llm["reason"] = f"大模型：{llm.get('reason') or '语义一致'}"
            else:
                llm["reason"] = f"大模型：{llm.get('reason') or '语义不一致'}"
            return llm

    return None
