# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""飞书电子表格：鉴权、读取、用例行解析。"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from script.log import SLog

TAG = "FeishuService"

_FEISHU_BASE = os.environ.get("FEISHU_API_BASE", "https://open.feishu.cn/open-apis")
_TOKEN_CACHE: Dict[str, Dict[str, Any]] = {}


def get_feishu_credentials(bot_id: Optional[str] = None) -> Tuple[str, str]:
    from server.services.system_settings_service import get_feishu_credentials as _creds

    return _creds(bot_id)


def get_tenant_access_token(bot_id: Optional[str] = None) -> str:
    app_id, app_secret = get_feishu_credentials(bot_id)
    if not app_id or not app_secret:
        raise RuntimeError(
            "未配置飞书机器人。请在「设置 → 飞书机器人」中添加至少一个机器人，"
            "并在应用飞书回归中选择要使用的机器人。"
        )
    cache_key = str(bot_id or "__default__")
    cached = _TOKEN_CACHE.get(cache_key) or {}
    now = time.time()
    if cached.get("token") and cached.get("expire_at", 0) > now + 60:
        return str(cached["token"])

    url = f"{_FEISHU_BASE}/auth/v3/tenant_access_token/internal"
    resp = requests.post(
        url,
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=30,
    )
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"飞书鉴权失败: {data.get('msg', data)}")
    token = data.get("tenant_access_token", "")
    expire = int(data.get("expire", 7200))
    _TOKEN_CACHE[cache_key] = {"token": token, "expire_at": now + expire}
    return token

# 表头别名 → 标准字段
_HEADER_ALIASES = {
    "case_id": ("用例编号", "编号", "case id", "id"),
    "platform": ("端", "平台", "platform"),
    "module": ("模块", "module"),
    "name": ("用例名称", "名称", "用例名", "case name"),
    "precondition": ("前置条件", "前置", "precondition"),
    "steps": ("步骤", "操作步骤", "steps"),
    "expected": ("预期", "预期结果", "expected"),
    "ios_status": ("ios", "ios状态", "ios 状态"),
    "android_status": ("android", "安卓", "android状态", "android 状态"),
}


def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", "", str(cell or "").strip().lower())


def parse_feishu_sheet_url(url: str) -> Dict[str, str]:
    """从飞书链接解析 spreadsheet_token、sheet_id、wiki_token。"""
    raw = (url or "").strip()
    out = {
        "doc_url": raw,
        "spreadsheet_token": "",
        "sheet_id": "",
        "wiki_token": "",
        "link_type": "",
    }
    if not raw:
        return out

    parsed = urlparse(raw)
    qs = parse_qs(parsed.query)

    for key in ("sheet", "sheetId", "sheet_id"):
        vals = qs.get(key) or []
        if vals:
            out["sheet_id"] = str(vals[0]).strip()
            break

    for key in ("spreadsheetToken", "spreadsheet_token", "token"):
        vals = qs.get(key) or []
        if vals and str(vals[0]).strip().lower().startswith("sht"):
            out["spreadsheet_token"] = str(vals[0]).strip()
            break

    m = re.search(r"/sheets/([A-Za-z0-9]+)", raw, re.I)
    if m:
        out["spreadsheet_token"] = m.group(1)
        out["link_type"] = "sheet"
        return out

    wiki_m = re.search(r"/wiki/([A-Za-z0-9]+)", raw, re.I)
    if wiki_m:
        out["wiki_token"] = wiki_m.group(1)
        out["link_type"] = "wiki"
        return out

    if "/base/" in raw.lower() or "bitable" in raw.lower():
        out["link_type"] = "bitable"
    return out


def _wiki_get_node(node_token: str, bot_id: Optional[str] = None) -> Dict[str, Any]:
    access = get_tenant_access_token(bot_id)
    url = f"{_FEISHU_BASE}/wiki/v2/spaces/get_node"
    resp = requests.get(
        url,
        params={"token": node_token},
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(
            f"解析知识库文档失败: {body.get('msg', body)}。"
            "请确认应用已开通 wiki 权限且机器人有权访问该文档。"
        )
    return (body.get("data") or {}).get("node") or {}


def query_sheet_tabs(
    spreadsheet_token: str,
    *,
    bot_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """查询表格下所有工作表。"""
    access = get_tenant_access_token(bot_id)
    url = f"{_FEISHU_BASE}/sheets/v3/spreadsheets/{spreadsheet_token}/sheets/query"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {access}"},
        timeout=30,
    )
    body = resp.json()
    if body.get("code") != 0:
        # 回退 v2 metainfo
        url2 = f"{_FEISHU_BASE}/sheets/v2/spreadsheets/{spreadsheet_token}/metainfo"
        resp2 = requests.get(url2, headers={"Authorization": f"Bearer {access}"}, timeout=30)
        body2 = resp2.json()
        if body2.get("code") != 0:
            raise RuntimeError(f"获取工作表列表失败: {body.get('msg', body)}")
        sheets = (body2.get("data") or {}).get("sheets") or []
        return [
            {"sheet_id": s.get("sheetId") or s.get("sheet_id"), "title": s.get("title", "")}
            for s in sheets
        ]
    sheets = (body.get("data") or {}).get("sheets") or []
    out = []
    for s in sheets:
        props = s.get("sheet_properties") or s
        sid = props.get("sheet_id") or props.get("sheetId") or s.get("sheet_id")
        title = props.get("title") or s.get("title") or ""
        if sid:
            out.append({"sheet_id": sid, "title": title})
    return out


def resolve_spreadsheet_target(
    config: Dict[str, Any],
) -> Tuple[str, str, str]:
    """
    解析 spreadsheet_token + sheet_id。
    返回 (spreadsheet_token, sheet_id, resolve_note)
    """
    cfg = config or {}
    bot_id = (cfg.get("bot_id") or "").strip() or None
    if not bot_id:
        from server.services.system_settings_service import list_feishu_bots

        configured = [b for b in list_feishu_bots() if b.get("configured")]
        if not configured:
            raise RuntimeError("请先在「设置 → 飞书机器人」中添加机器人")
        raise RuntimeError("请在本应用「飞书配置」中选择飞书机器人并保存")

    url = (cfg.get("doc_url") or cfg.get("url") or "").strip()
    if not url and not cfg.get("spreadsheet_token"):
        raise RuntimeError("请填写飞书表格链接")

    parsed = parse_feishu_sheet_url(url)
    spreadsheet_token = (cfg.get("spreadsheet_token") or parsed.get("spreadsheet_token") or "").strip()
    sheet_id = (cfg.get("sheet_id") or parsed.get("sheet_id") or "").strip()
    note = ""

    if not spreadsheet_token and parsed.get("wiki_token"):
        node = _wiki_get_node(parsed["wiki_token"], bot_id)
        obj_type = (node.get("obj_type") or "").strip()
        obj_token = (node.get("obj_token") or "").strip()
        if obj_type != "sheet":
            raise RuntimeError(
                f"该知识库节点类型为「{obj_type}」，不是电子表格。"
                "请打开表格子页面后复制浏览器地址（含 /sheets/ 或 ?sheet=）。"
            )
        spreadsheet_token = obj_token
        note = "已从知识库链接解析表格 token"

    if not spreadsheet_token:
        if parsed.get("link_type") == "bitable":
            raise RuntimeError("当前链接为多维表格（bitable），暂仅支持电子表格（sheets）")
        raise RuntimeError(
            "无法从链接解析表格 token。请复制浏览器地址栏完整 URL，"
            "需包含 /sheets/sht... 或知识库 /wiki/... 表格页链接。"
        )

    if not sheet_id:
        tabs = query_sheet_tabs(spreadsheet_token, bot_id=bot_id)
        if not tabs:
            raise RuntimeError("表格下没有可用工作表，请检查链接与机器人权限")
        # 优先匹配「回归」「用例」等工作表名
        preferred = None
        for t in tabs:
            title = (t.get("title") or "").lower()
            if any(k in title for k in ("回归", "用例", "app", "case")):
                preferred = t["sheet_id"]
                note = f"已自动选择工作表「{t.get('title')}」"
                break
        sheet_id = preferred or tabs[0]["sheet_id"]
        if not note:
            note = f"已自动选择首个工作表「{tabs[0].get('title', sheet_id)}」"

    return spreadsheet_token, sheet_id, note


def fetch_sheet_values(
    spreadsheet_token: str,
    sheet_id: str,
    cell_range: str = "A1:O500",
    *,
    bot_id: Optional[str] = None,
) -> List[List[Any]]:
    """读取单个 sheet 范围，返回二维数组。"""
    token = get_tenant_access_token(bot_id)
    range_id = f"{sheet_id}!{cell_range}" if sheet_id else cell_range
    # 飞书要求 range 作为 path 段，需编码但保留 ! :
    range_encoded = requests.utils.quote(range_id, safe="!")
    url = f"{_FEISHU_BASE}/sheets/v2/spreadsheets/{spreadsheet_token}/values/{range_encoded}"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"valueRenderOption": "ToString", "dateTimeRenderOption": "FormattedString"},
        timeout=60,
    )
    try:
        body = resp.json()
    except Exception:
        raise RuntimeError(f"读取飞书表格失败: HTTP {resp.status_code} {resp.text[:200]}")
    if body.get("code") != 0:
        msg = body.get("msg") or body
        raise RuntimeError(
            f"读取飞书表格失败(code={body.get('code')}): {msg}。"
            f"请检查：1) 机器人有表格权限 2) sheet_id={sheet_id} 正确 3) 表格已授权给应用"
        )
    vr = (body.get("data") or {}).get("valueRange") or {}
    return vr.get("values") or []


def _detect_header_row(rows: List[List[Any]]) -> Tuple[int, Dict[str, int]]:
    for i, row in enumerate(rows[:15]):
        cells = [_norm_header(c) for c in row]
        joined = "".join(cells)
        if "用例编号" in joined or ("步骤" in joined and "预期" in joined):
            col_map: Dict[str, int] = {}
            for idx, cell in enumerate(cells):
                for field, aliases in _HEADER_ALIASES.items():
                    if any(a in cell or cell in _norm_header(a) for a in aliases):
                        col_map.setdefault(field, idx)
            if "steps" in col_map or "name" in col_map:
                return i, col_map
    return 1, {
        "case_id": 0,
        "platform": 1,
        "module": 2,
        "name": 3,
        "precondition": 4,
        "steps": 5,
        "expected": 6,
        "ios_status": 13,
        "android_status": 14,
    }


def _cell(row: List[Any], col_map: Dict[str, int], key: str) -> str:
    idx = col_map.get(key)
    if idx is None or idx >= len(row):
        return ""
    return str(row[idx] or "").strip()


def _split_numbered_lines(text: str) -> List[str]:
    """按出现顺序返回步骤/预期文本（不含编号）。"""
    return [item["text"] for item in _parse_numbered_items(text)]


def _strip_leading_number(text: str) -> str:
    """去掉正文开头残留的「2. 」等编号（飞书常见「1. 2. xxx」连写）。"""
    t = (text or "").strip()
    while t:
        m = re.match(r"^\d+[.、．)\）]\s*", t)
        if not m:
            break
        t = t[m.end() :].strip()
    return t


def _parse_numbered_items(text: str, field: str = "") -> List[Dict[str, Any]]:
    """
    解析飞书单元格中带编号的条目，保留原始编号。
    预期列可能写 2. 3. 4. 而步骤列写 1. 2. 3. 4. — 需按编号对齐而非数组下标。
    field: step | expected — 启用 LLM 语义解析（CASE_TEXT_PARSE_LLM）。
    """
    if field in ("step", "expected"):
        try:
            from server.services.case_text_semantic_service import (
                FIELD_EXPECTED,
                FIELD_STEP,
                parse_numbered_field,
            )

            f = FIELD_STEP if field == "step" else FIELD_EXPECTED
            return parse_numbered_field(text, f)
        except Exception as e:
            SLog.w(TAG, f"semantic numbered parse failed, fallback rules: {e}")
    try:
        from server.services.case_text_semantic_service import parse_numbered_items_rules

        return parse_numbered_items_rules(text)
    except Exception:
        pass
    raw = (text or "").strip()
    if not raw:
        return []
    return [{"num": 1, "text": _strip_leading_number(raw)}]


def _rebalance_single_expected(
    step_items: List[Dict[str, Any]],
    expected_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    if len(expected_items) != 1 or len(step_items) <= 1:
        return expected_items
    only = expected_items[0]
    if only.get("num") != 1:
        return expected_items
    last_no = int(step_items[-1]["num"])
    return [{"num": last_no, "text": only.get("text") or ""}]


def _expected_by_step_number(text: str) -> Dict[int, str]:
    """编号 → 预期文案（飞书预期列可跳号，如 2/3/4 无 1）。"""
    return {
        int(item["num"]): item["text"]
        for item in _parse_numbered_items(text, field="expected")
    }


def normalize_feishu_case(case: Dict[str, Any]) -> Dict[str, Any]:
    """补齐编号字段；JSON 缓存会把 expected_by_step 的 int 键变成 str。"""
    out = dict(case or {})
    steps = list(out.get("steps") or [])
    if steps:
        if out.get("step_nums"):
            out["step_nums"] = [int(x) for x in out["step_nums"]]
        else:
            raw_steps = out.get("steps_raw") or ""
            items = _parse_numbered_items(raw_steps, field="step")
            out["step_nums"] = (
                [int(it["num"]) for it in items]
                if items
                else list(range(1, len(steps) + 1))
            )

    expected = list(out.get("expected") or [])
    expected_raw = out.get("expected_raw") or ""
    raw_steps = out.get("steps_raw") or ""
    step_items = (
        [{"num": int(n), "text": t} for n, t in zip(out.get("step_nums") or [], steps)]
        if steps and out.get("step_nums")
        else (_parse_numbered_items(raw_steps, field="step") if raw_steps else [])
    )
    expected_items = _rebalance_single_expected(
        step_items, _parse_numbered_items(expected_raw, field="expected")
    )
    if expected_items:
        out["expected"] = [it["text"] for it in expected_items]
        out["expected_nums"] = [int(it["num"]) for it in expected_items]
    elif expected:
        if out.get("expected_nums"):
            out["expected_nums"] = [int(x) for x in out["expected_nums"]]
        else:
            out["expected_nums"] = list(range(1, len(expected) + 1))
    else:
        out["expected_nums"] = []

    ebs = out.get("expected_by_step") or {}
    if ebs:
        out["expected_by_step"] = {
            int(k): str(v)
            for k, v in ebs.items()
            if str(k).strip().lstrip("-").isdigit()
        }
    elif expected_items:
        out["expected_by_step"] = _expected_by_step_number(expected_raw)
    else:
        out["expected_by_step"] = {}
    return out


def parse_cases_from_rows(rows: List[List[Any]]) -> List[Dict[str, Any]]:
    if not rows:
        return []
    header_idx, col_map = _detect_header_row(rows)
    cases: List[Dict[str, Any]] = []
    for row_idx, row in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
        case_id = _cell(row, col_map, "case_id")
        name = _cell(row, col_map, "name")
        steps_raw = _cell(row, col_map, "steps")
        if not case_id and not name and not steps_raw:
            continue
        if _norm_header(case_id) in ("用例编号", "编号") and not steps_raw:
            continue

        step_items = _parse_numbered_items(steps_raw, field="step")
        expected_raw = _cell(row, col_map, "expected")
        expected_items = _rebalance_single_expected(
            step_items, _parse_numbered_items(expected_raw, field="expected")
        )
        if not (case_id or "").strip():
            for cell in row:
                m = re.search(r"\b(app-\d+)\b", str(cell or ""), flags=re.I)
                if m:
                    case_id = m.group(1)
                    break
        step_lines = [item["text"] for item in step_items]
        expected_lines = [item["text"] for item in expected_items]
        cases.append(
            normalize_feishu_case(
                {
                    "row_index": row_idx,
                    "case_id": case_id or f"row-{row_idx}",
                    "platform": _cell(row, col_map, "platform"),
                    "module": _cell(row, col_map, "module"),
                    "name": name,
                    "precondition": _cell(row, col_map, "precondition"),
                    "steps_raw": steps_raw,
                    "steps": step_lines,
                    "step_nums": [item["num"] for item in step_items],
                    "expected_raw": expected_raw,
                    "expected": expected_lines,
                    "expected_nums": [item["num"] for item in expected_items],
                    "expected_by_step": _expected_by_step_number(expected_raw),
                    "ios_status": _cell(row, col_map, "ios_status"),
                    "android_status": _cell(row, col_map, "android_status"),
                }
            )
        )
    return cases


def load_cases_from_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """根据应用绑定的飞书配置拉取用例列表。"""
    cfg = config or {}
    spreadsheet_token, sheet_id, resolve_note = resolve_spreadsheet_target(cfg)
    cell_range = cfg.get("data_range") or "A1:O500"
    bot_id = (cfg.get("bot_id") or "").strip() or None

    try:
        rows = fetch_sheet_values(
            spreadsheet_token, sheet_id, cell_range, bot_id=bot_id
        )
    except RuntimeError:
        raise
    except Exception as e:
        SLog.e(TAG, f"fetch sheet failed: {e}")
        raise RuntimeError(f"读取飞书表格异常: {e}") from e

    if not rows:
        raise RuntimeError(
            "表格数据为空。请确认工作表有用例数据，或调整读取范围（如 A1:O500）。"
        )

    cases = parse_cases_from_rows(rows)
    if not cases:
        raise RuntimeError(
            "未解析到用例行。请确认表头包含「用例编号」「步骤」「预期」等列，且数据从第 2 行开始。"
        )

    return {
        "spreadsheet_token": spreadsheet_token,
        "sheet_id": sheet_id,
        "doc_url": cfg.get("doc_url") or cfg.get("url") or "",
        "total": len(cases),
        "cases": cases,
        "resolve_note": resolve_note,
    }
