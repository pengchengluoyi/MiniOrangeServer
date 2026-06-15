# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
飞书用例文本语义解析：前置条件、测试步骤、预期效果。

优先 LLM（CASE_TEXT_PARSE_LLM / EXPECTATION_PARSE_LLM），规则回退。
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from script.log import SLog

from server.services.expectation_semantic_service import (
    _llm_chat_json,
    _llm_configured,
    parse_expectation_claims,
    parse_expectation_texts,
)

TAG = "CaseTextSemantic"

FIELD_PRECONDITION = "precondition"
FIELD_STEP = "step"
FIELD_EXPECTED = "expected"

_PRECONDITION_KINDS = frozenset(
    {
        "clear_cache",
        "check_sim",
        "check_wechat",
        "check_no_wechat",
        "check_ios_device",
        "check_android_device",
        "check_logged_in",
        "check_not_logged_in",
        "unknown",
    }
)

_NUMBERED_PATTERN = re.compile(r"(?:^|\n)\s*(\d+)[.、．)\）]\s*")
_STEP_VERB_RE = re.compile(r"点击|打开|关闭|滑|等待|返回|启动|输入|勾选|选择", re.I)
_CONDITIONAL_STEP_RE = re.compile(
    r"^(?:如果|若|当).+|(?:可跳过|跳过此步骤|此步骤可跳过)",
    re.I,
)

_FIELD_CACHE: Dict[str, List[Dict[str, Any]]] = {}
_FIELD_CACHE_MAX = 256


def case_text_llm_enabled() -> bool:
    flag = (os.environ.get("CASE_TEXT_PARSE_LLM") or "").strip().lower()
    if flag:
        return flag not in ("0", "false", "no", "off")
    flag = (os.environ.get("EXPECTATION_PARSE_LLM") or "1").strip().lower()
    return flag not in ("0", "false", "no", "off")


def _strip_number_prefix(text: str) -> str:
    t = (text or "").strip()
    while t:
        m = re.match(r"^\d+[.、．)\）]\s*", t)
        if not m:
            break
        t = t[m.end() :].strip()
    return t


def _cache_get(key: str) -> Optional[List[Dict[str, Any]]]:
    rows = _FIELD_CACHE.get(key)
    return [dict(r) for r in rows] if rows else None


def _cache_put(key: str, rows: List[Dict[str, Any]]) -> None:
    if len(_FIELD_CACHE) >= _FIELD_CACHE_MAX:
        _FIELD_CACHE.clear()
    _FIELD_CACHE[key] = [dict(r) for r in rows]


def parse_numbered_items_rules(text: str) -> List[Dict[str, Any]]:
    """规则解析带编号的单元格（步骤/预期列）。"""
    raw = (text or "").strip()
    if not raw:
        return []
    matches = list(_NUMBERED_PATTERN.finditer(raw))
    if not matches:
        return [{"num": 1, "text": raw, "parse_method": "rules"}]
    items: List[Dict[str, Any]] = []
    prefix = raw[: matches[0].start()].strip()
    if prefix:
        items.append({"num": 1, "text": prefix, "parse_method": "rules"})
    for idx, match in enumerate(matches):
        try:
            num = int(match.group(1))
        except ValueError:
            continue
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        body = _strip_number_prefix(raw[start:end].strip())
        if body:
            items.append({"num": num, "text": body, "parse_method": "rules"})
    if not items:
        return [{"num": 1, "text": _strip_number_prefix(raw), "parse_method": "rules"}]
    return items


def _llm_parse_numbered_field(text: str, field: str) -> Optional[List[Dict[str, Any]]]:
    raw = (text or "").strip()
    if not raw:
        return []

    if field == FIELD_STEP:
        system = (
            "你是移动 App 自动化测试用例助手。解析飞书「测试步骤」单元格为有序步骤列表。"
            "规则：\n"
            "1. 保留步骤编号 num（从原文 1. 2. 提取；无编号则按顺序 1,2,3）。\n"
            "2. 每条 text 是一条可独立执行的 UI 操作，使用完整自然语言（如「点击登录页右上角访客浏览」）。\n"
            "3. 不要把一句里的连续操作误拆；明确用分号/换行/编号分隔的才拆多条。\n"
            "4. 只输出 JSON：{\"items\":[{\"num\":1,\"text\":\"...\"}]}"
        )
    elif field == FIELD_EXPECTED:
        system = (
            "你是移动 App 自动化测试用例助手。解析飞书「预期效果」单元格为与步骤编号对齐的预期列表。"
            "规则：\n"
            "1. num 对应用例步骤编号（可从 2. 3. 跳号，保留原编号）。\n"
            "2. 每条 text 是该步对应的预期结果，保持完整语义，勿在逗号处无意义拆分。\n"
            "3. 仅一条预期且对应最后一步时，num 可为最大步骤号。\n"
            "4. 只输出 JSON：{\"items\":[{\"num\":1,\"text\":\"...\"}]}"
        )
    else:
        return None

    data = _llm_chat_json(system=system, user_payload={"field": field, "raw": raw}, max_tokens=600)
    if not data:
        return None
    rows = data.get("items") or []
    if not isinstance(rows, list) or not rows:
        return None

    out: List[Dict[str, Any]] = []
    seen_nums: set = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = _strip_number_prefix(str(row.get("text") or "").strip())
        if not text:
            continue
        try:
            num = int(row.get("num") or len(out) + 1)
        except (TypeError, ValueError):
            num = len(out) + 1
        if num in seen_nums:
            num = max(seen_nums, default=0) + 1
        seen_nums.add(num)
        out.append({"num": num, "text": text, "parse_method": "llm"})
    if not out:
        return None
    out.sort(key=lambda x: int(x["num"]))
    SLog.i(TAG, f"LLM parsed {field} into {len(out)} items")
    return out


def parse_numbered_field(
    text: str,
    field: str,
    *,
    use_llm: bool = True,
) -> List[Dict[str, Any]]:
    """解析步骤列或预期列（带编号条目）。"""
    raw = (text or "").strip()
    if not raw:
        return []
    cache_key = f"{field}:{int(use_llm and case_text_llm_enabled())}:{raw}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    rows: Optional[List[Dict[str, Any]]] = None
    if use_llm and case_text_llm_enabled() and _llm_configured() and field in (
        FIELD_STEP,
        FIELD_EXPECTED,
    ):
        rows = _llm_parse_numbered_field(raw, field)
    if not rows:
        rows = parse_numbered_items_rules(raw)
    _cache_put(cache_key, rows)
    return [dict(r) for r in rows]


def _classify_precondition_rules(line: str) -> Tuple[str, str]:
    """与 case_precondition_service._classify_line 对齐的规则分类。"""
    t = (line or "").strip()
    low = t.lower()

    if re.search(r"无缓存|清除缓存|清理缓存|清空缓存|清缓存|清除应用", t):
        return "clear_cache", "before_launch"
    if re.search(r"sim卡|sim\s*卡|安装\s*sim|手机卡|电话卡", t, re.I):
        return "check_sim", "before_launch"
    if re.search(r"安装.*微信|已装.*微信|有微信|装了微信|微信已安装", t):
        return "check_wechat", "before_launch"
    if re.search(r"未安装微信|没装微信|无微信", t):
        return "check_no_wechat", "before_launch"
    if re.search(r"ios|苹果机|iphone|ipad", low) and re.search(r"设备|执行|手机", t):
        return "check_ios_device", "before_launch"
    if re.search(r"安卓|android", low) and re.search(r"设备|执行|手机", t):
        return "check_android_device", "before_launch"
    if re.search(r"已登录|登录状态|保持登录", t):
        return "check_logged_in", "after_launch"
    if re.search(r"未登录|游客|未登陆", t):
        return "check_not_logged_in", "after_launch"
    return "unknown", "before_launch"


def _llm_parse_precondition_items(text: str) -> Optional[List[Dict[str, Any]]]:
    raw = (text or "").strip()
    if not raw:
        return []

    system = (
        "你是移动 App 测试环境助手。解析飞书「前置条件」为可执行检查项列表。"
        "规则：\n"
        "1. 每条含 text（原文要点）、kind、phase。\n"
        "2. kind 取值：clear_cache|check_sim|check_wechat|check_no_wechat|"
        "check_ios_device|check_android_device|check_logged_in|check_not_logged_in|unknown。\n"
        "3. phase：清缓存/SIM/微信/设备类型 → before_launch；已登录/未登录 → after_launch。\n"
        "4. 无法自动化的环境描述用 kind=unknown。\n"
        "5. 只输出 JSON：{\"items\":[{\"num\":1,\"text\":\"...\",\"kind\":\"...\",\"phase\":\"...\"}]}"
    )
    data = _llm_chat_json(system=system, user_payload={"precondition": raw}, max_tokens=500)
    if not data:
        return None
    rows = data.get("items") or []
    if not isinstance(rows, list) or not rows:
        return None

    out: List[Dict[str, Any]] = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        text_line = _strip_number_prefix(str(row.get("text") or "").strip())
        if not text_line:
            continue
        kind = str(row.get("kind") or "unknown").strip().lower()
        if kind not in _PRECONDITION_KINDS:
            kind, _ = _classify_precondition_rules(text_line)
        phase = str(row.get("phase") or "").strip().lower()
        if phase not in ("before_launch", "after_launch"):
            _, phase = _classify_precondition_rules(text_line)
        try:
            num = int(row.get("num") or i + 1)
        except (TypeError, ValueError):
            num = i + 1
        out.append(
            {
                "num": num,
                "text": text_line,
                "kind": kind,
                "phase": phase,
                "parse_method": "llm",
            }
        )
    return out or None


def parse_precondition_items(
    text: str,
    *,
    use_llm: bool = True,
) -> List[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return []
    cache_key = f"pre:{int(use_llm and case_text_llm_enabled())}:{raw}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    rows: Optional[List[Dict[str, Any]]] = None
    if use_llm and case_text_llm_enabled() and _llm_configured():
        rows = _llm_parse_precondition_items(raw)
    if not rows:
        parts = re.split(r"(?:\n|^)\s*\d+[.、．)\）]\s*", raw, flags=re.M)
        lines = [p.strip() for p in parts if p and p.strip()]
        if len(lines) <= 1:
            lines = [raw]
        rows = []
        for i, line in enumerate(lines):
            kind, phase = _classify_precondition_rules(line)
            rows.append(
                {
                    "num": i + 1,
                    "text": line,
                    "kind": kind,
                    "phase": phase,
                    "parse_method": "rules",
                }
            )
    _cache_put(cache_key, rows)
    return [dict(r) for r in rows]


def parse_precondition_lines(text: str, *, use_llm: bool = True) -> List[str]:
    return [r["text"] for r in parse_precondition_items(text, use_llm=use_llm)]


def is_conditional_step_line(line: str) -> bool:
    """描述性/条件步骤（无明确 UI 动作），仅做预期校验。"""
    text = _strip_number_prefix(line)
    if not text:
        return False
    if _STEP_VERB_RE.search(text):
        return False
    return bool(_CONDITIONAL_STEP_RE.search(text))


def _normalize_step_command_rules(line: str) -> str:
    line = _strip_number_prefix(line)
    if not line:
        return ""
    if is_conditional_step_line(line):
        return ""
    if re.search(r"同意并继续", line):
        if not _STEP_VERB_RE.search(line):
            return f"点击{line}"
        return line
    if not _STEP_VERB_RE.search(line):
        return f"点击{line}"
    return line


def _llm_normalize_step_command(line: str) -> Optional[str]:
    text = _strip_number_prefix(line)
    if not text:
        return ""
    system = (
        "你是移动 App 自动化 Copilot 指令改写助手。"
        "把飞书测试步骤改写为一条可规划的 UI 自动化指令。"
        "规则：\n"
        "1. 保留用户意图，补全动词（点击/滑动/打开/等待等）。\n"
        "2. 不要拆成多条；只输出一条指令字符串。\n"
        "3. 只输出 JSON：{\"command\":\"...\"}"
    )
    data = _llm_chat_json(system=system, user_payload={"step": text}, max_tokens=200)
    if not data:
        return None
    cmd = str(data.get("command") or "").strip()
    return cmd or None


def normalize_step_command(line: str, *, use_llm: bool = True) -> str:
    """将单条测试步骤规范为 Copilot 可规划的指令。"""
    raw = (line or "").strip()
    if not raw:
        return ""
    cache_key = f"cmd:{int(use_llm and case_text_llm_enabled())}:{raw}"
    cached = _cache_get(cache_key)
    if cached and cached[0].get("text"):
        return str(cached[0]["text"])

    cmd: Optional[str] = None
    if use_llm and case_text_llm_enabled() and _llm_configured():
        cmd = _llm_normalize_step_command(raw)
    if not cmd:
        cmd = _normalize_step_command_rules(raw)
    _cache_put(cache_key, [{"text": cmd}])
    return cmd


__all__ = [
    "FIELD_PRECONDITION",
    "FIELD_STEP",
    "FIELD_EXPECTED",
    "case_text_llm_enabled",
    "parse_numbered_field",
    "parse_numbered_items_rules",
    "parse_precondition_items",
    "parse_precondition_lines",
    "normalize_step_command",
    "parse_expectation_claims",
    "parse_expectation_texts",
]
