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


def _llm_parse_enabled() -> bool:
    case_flag = (os.environ.get("CASE_TEXT_PARSE_LLM") or "").strip().lower()
    if case_flag:
        return case_flag not in ("0", "false", "no", "off")
    flag = (os.environ.get("EXPECTATION_PARSE_LLM") or "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


_CLAIM_KINDS = frozenset(
    {
        "page_nav",
        "text_present",
        "text_absent",
        "numeric",
        "state_change",
        "login_outcome",
        "generic",
    }
)

_ASSERTION_HINT_RE = re.compile(
    r"进入|打开|切换|跳转|显示|展示|包含|不包含|不再|数量|成功|失败|登录|页|校验|确认|变为|变成",
    re.I,
)

_PARSE_CACHE: Dict[str, List[Dict[str, Any]]] = {}
_PARSE_CACHE_MAX = 128

_NUMBERED_STEP_RE = re.compile(r"(?:^|\s)\d+[.、．)\）]\s*")


def _strip_expected_prefix(text: str) -> str:
    return re.sub(r"^\d+[.、．)\）]\s*", "", (text or "").strip())


def _protect_quoted_segments(text: str) -> tuple[str, Dict[str, str]]:
    """保护引号内文案，避免误切分。"""
    protected: Dict[str, str] = {}

    def _shield(match: re.Match) -> str:
        key = f"__Q{len(protected)}__"
        protected[key] = match.group(0)
        return key

    shielded = re.sub(r"[「『\"']([^」』\"']+)[」』\"']", _shield, text)
    return shielded, protected


def _restore_protected(text: str, protected: Dict[str, str]) -> str:
    out = text
    for key, val in protected.items():
        out = out.replace(key, val)
    return out


def _looks_like_independent_claims(parts: List[str]) -> bool:
    if len(parts) <= 1:
        return False
    scored = 0
    for p in parts:
        if len(p.strip()) < 2:
            return False
        if _ASSERTION_HINT_RE.search(p):
            scored += 1
    return scored >= len(parts)


def _parse_expectation_claims_rules(expected_text: str) -> List[Dict[str, Any]]:
    """
    规则拆解预期（无 LLM 时的回退）。
    默认不在逗号处切分，避免「进入首页，推荐流正常」被误拆。
    """
    exp = _strip_expected_prefix(expected_text)
    if not exp:
        return []

    if _NUMBERED_STEP_RE.search(exp):
        chunks = _NUMBERED_STEP_RE.split(exp)
        parts = [_strip_expected_prefix(c) for c in chunks if c and c.strip()]
        parts = [p for p in parts if p]
        if _looks_like_independent_claims(parts):
            return [
                {"text": p, "kind": "generic", "parse_method": "rules"}
                for p in parts
            ]

    cross_app_shields = (
        "切换到微信app, 并打开登录页面",
        "切换到微信app，并打开登录页面",
    )
    shielded = exp
    protected: Dict[str, str] = {}
    for phrase in cross_app_shields:
        if phrase in shielded:
            key = f"__XAPP_{len(protected)}__"
            protected[key] = phrase
            shielded = shielded.replace(phrase, key)
    shielded, quoted_protected = _protect_quoted_segments(shielded)
    protected.update(quoted_protected)
    delims = r"[；;、]"
    if (os.environ.get("EXPECTATION_SPLIT_COMMA") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        delims = r"[；;、,，]"

    parts = [p.strip() for p in re.split(delims, shielded) if p.strip()]
    parts = [_restore_protected(p, protected) for p in parts]

    if len(parts) <= 1:
        return [{"text": exp, "kind": "generic", "parse_method": "rules"}]
    if not _looks_like_independent_claims(parts):
        return [{"text": exp, "kind": "generic", "parse_method": "rules"}]
    return [{"text": p, "kind": "generic", "parse_method": "rules"} for p in parts]


def _llm_chat_json(
    *,
    system: str,
    user_payload: Dict[str, Any],
    max_tokens: int = 400,
    timeout: int = 25,
) -> Optional[Dict[str, Any]]:
    if not _llm_configured():
        return None
    api_key = (os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    base = (os.environ.get("LLM_API_BASE") or os.environ.get("OPENAI_BASE_URL") or "").rstrip("/")
    model = (os.environ.get("LLM_MODEL") or "qwen-plus").strip()
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                ],
                "temperature": 0,
                "max_tokens": max_tokens,
            },
            timeout=timeout,
        )
        if not resp.ok:
            SLog.w(TAG, f"LLM HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content") or ""
        start = content.find("{")
        end = content.rfind("}") + 1
        if start < 0 or end <= start:
            return None
        return json.loads(content[start:end])
    except Exception as e:
        SLog.w(TAG, f"LLM chat failed: {e}")
        return None


def _llm_parse_expectation_claims(expected_text: str) -> Optional[List[Dict[str, Any]]]:
    """用大模型把一条预期拆成可独立校验的原子断言。"""
    exp = _strip_expected_prefix(expected_text)
    if not exp:
        return []

    system = (
        "你是移动 App 自动化测试用例编写助手。"
        "用户给出一条「预期效果」自然语言，请拆成若干条可独立用 OCR/页面识别校验的原子断言。"
        "规则：\n"
        "1. 若整句只需一次校验，claims 只含一条，text 保留原意完整表述。\n"
        "2. 不要在无意义的逗号处强行拆分；「进入首页，推荐正常」若是一条综合预期可保留一条。\n"
        "3. 明确并列的多条预期（分号、顿号、并且/同时/且 连接的两件独立事）才拆多条。\n"
        "4. kind 取值：page_nav|text_present|text_absent|numeric|state_change|login_outcome|generic。\n"
        "5. 只输出 JSON：{\"claims\":[{\"text\":\"...\",\"kind\":\"...\"}]}"
    )
    data = _llm_chat_json(system=system, user_payload={"expected": exp})
    if not data:
        return None
    rows = data.get("claims") or []
    if not isinstance(rows, list) or not rows:
        return None

    out: List[Dict[str, Any]] = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = _strip_expected_prefix(str(row.get("text") or "").strip())
        if not text or text in seen:
            continue
        seen.add(text)
        kind = str(row.get("kind") or "generic").strip().lower()
        if kind not in _CLAIM_KINDS:
            kind = "generic"
        out.append({"text": text, "kind": kind, "parse_method": "llm"})
    if not out:
        return None
    if len(out) == 1 and out[0]["text"] == exp:
        return out
    if len(out) == 1:
        return out
    SLog.i(TAG, f"LLM parsed expectation into {len(out)} claims: {exp[:40]!r}")
    return out


def parse_expectation_claims(
    expected_text: str,
    *,
    use_llm: bool = True,
) -> List[Dict[str, Any]]:
    """
    将一条预期拆解为原子断言列表。
    优先 LLM（EXPECTATION_PARSE_LLM=1 且已配置 LLM_API_KEY/BASE），否则规则回退。
    """
    exp = _strip_expected_prefix(expected_text)
    if not exp:
        return []

    cache_key = f"{int(use_llm and _llm_parse_enabled())}:{exp}"
    if cache_key in _PARSE_CACHE:
        return [dict(c) for c in _PARSE_CACHE[cache_key]]

    claims: Optional[List[Dict[str, Any]]] = None
    if use_llm and _llm_parse_enabled() and _llm_configured():
        claims = _llm_parse_expectation_claims(exp)
    if not claims:
        claims = _parse_expectation_claims_rules(exp)

    if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX:
        _PARSE_CACHE.clear()
    _PARSE_CACHE[cache_key] = [dict(c) for c in claims]
    return [dict(c) for c in claims]


def parse_expectation_texts(expected_text: str, *, use_llm: bool = True) -> List[str]:
    """仅返回断言文案列表（兼容旧 _split_expected_fragments 调用方）。"""
    return [c["text"] for c in parse_expectation_claims(expected_text, use_llm=use_llm)]


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


_FOREIGN_APP_SWITCH_RE = re.compile(
    r"(?:切换到|切换至?|切到|打开|跳转至?)\s*(.+?)(?:app|应用)?(?:\s*[,，].*)?$",
    re.I,
)


def evaluate_foreign_app_expectation(
    expected: str,
    foreground_package: str,
    *,
    test_package: str = "",
    platform: str = "android",
) -> Optional[Dict[str, Any]]:
    """
    跨 App 预期：仅依据前台包名判定（不用 OCR）。
    例：「切换到微信app, 并打开登录页面」→ 期望前台为 com.tencent.mm
    """
    exp = (expected or "").strip()
    pkg = (foreground_package or "").strip()
    if not exp:
        return None

    target_text = ""
    m = re.search(r"切换到\s*(.+?)(?:app|应用)", exp, re.I)
    if m:
        target_text = m.group(1).strip()
    if not target_text:
        m2 = re.search(r"打开\s*(.+?)(?:app|应用)?(?:\s*[,，]|$)", exp, re.I)
        if m2:
            target_text = m2.group(1).strip()
    if not target_text:
        m3 = _FOREIGN_APP_SWITCH_RE.match(exp)
        if m3:
            target_text = re.sub(r"(app|应用)$", "", m3.group(1).strip(), flags=re.I).strip()

    if not target_text:
        return None

    try:
        from server.services.locate.app_packages import (
            package_for_app_key,
            resolve_known_app_by_alias,
            resolve_known_app_by_package,
        )

        known = resolve_known_app_by_alias(target_text)
        if not known:
            return None
        expected_pkg = package_for_app_key(known.key, platform=platform)
        if not expected_pkg:
            return None

        if pkg and known.matches_package(pkg):
            return {
                "ok": True,
                "reason": f"前台应用为 {known.name}（{pkg}）",
                "method": "foreground_package",
                "expected_package": expected_pkg,
                "foreground_package": pkg,
            }

        if test_package and pkg == test_package:
            actual_name = known.name
            other = resolve_known_app_by_package(pkg)
            if other:
                actual_name = other.name
            return {
                "ok": False,
                "reason": (
                    f"前台仍为被测应用 {actual_name}（{pkg}），"
                    f"未完成切换到 {known.name}（{expected_pkg}）"
                ),
                "method": "foreground_package",
                "expected_package": expected_pkg,
                "foreground_package": pkg,
            }

        if pkg:
            other = resolve_known_app_by_package(pkg)
            cur = other.name if other else pkg
            return {
                "ok": False,
                "reason": f"前台为 {cur}（{pkg}），期望 {known.name}（{expected_pkg}）",
                "method": "foreground_package",
                "expected_package": expected_pkg,
                "foreground_package": pkg,
            }
        return {
            "ok": False,
            "reason": f"无法读取前台包名，期望 {known.name}（{expected_pkg}）",
            "method": "foreground_package",
            "expected_package": expected_pkg,
            "foreground_package": "",
        }
    except Exception as e:
        SLog.w(TAG, f"foreign app expectation failed: {e}")
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
