# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""回放失败分析与应用知识库纠错条目生成。"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from server.services.system_settings_service import list_testing_knowledge, save_testing_knowledge

_METHOD_HINTS = {
    "none": "未能将指令映射到屏幕控件（层级/OCR/图标库/底栏均未命中）。",
    "hierarchy": "曾尝试无障碍层级匹配但未成功或后续点击失败。",
    "ocr": "曾尝试 OCR 文字匹配。",
    "icon_target": "曾尝试无字图标库匹配。",
    "bottom_tab": "曾尝试在当前屏底栏（层级/OCR）匹配 Tab。",
    "clip": "曾尝试 OpenCLIP 中英文视觉匹配（区域约束 + 图标库）。",
    "clip_patch": "曾尝试 OpenCLIP 中英文视觉匹配（区域约束 + 图标库）。",
    "clip_gallery_patch": "曾尝试 OpenCLIP 图标库 + 屏上候选匹配。",
    "clip_gallery_coord": "曾尝试 OpenCLIP 图标库坐标匹配。",
    "coordinate": "使用了显式坐标。",
    "label": "使用了无障碍文案点击。",
}


def _guess_category(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ("tab", "底栏", "切换")):
        return "Tab切换"
    if any(k in t for k in ("登录", "注册", "账号")):
        return "登录注册"
    if any(k in t for k in ("feed", "首页", "详情", "作品", "卡片", "列表")):
        return "UI导航"
    if any(k in t for k in ("校验", "断言", "预期", "展示")):
        return "业务逻辑"
    return "其他"


def _extract_tags(text: str) -> List[str]:
    tags: List[str] = []
    for kw in ("feed", "首页", "详情", "tab", "底栏", "登录", "作品", "卡片", "点击", "滑动"):
        if kw in (text or ""):
            tags.append(kw)
    return list(dict.fromkeys(tags))


def analyze_step_failure(payload: Dict[str, Any]) -> Dict[str, Any]:
    """根据失败步骤上下文生成分析与知识库草稿。"""
    method = (payload.get("method") or "").strip()
    msg = (payload.get("msg") or "").strip()
    action_text = (payload.get("action_text") or "").strip()
    step_text = (payload.get("step_text") or "").strip()
    command = (payload.get("command") or "").strip()
    case_name = (payload.get("case_name") or "").strip()
    role = (payload.get("role") or "action").strip()
    title_part = (payload.get("title") or "").strip()
    expected_text = (payload.get("expected_text") or "").strip()

    combined = " ".join(filter(None, [command, step_text, action_text, expected_text, msg, title_part]))
    causes: List[str] = []
    suggestions: List[str] = []

    if method == "none" or "未找到可点击" in msg:
        causes.append(_METHOD_HINTS.get("none", "点击目标定位失败。"))
        suggestions.extend(
            [
                "说明目标控件在页面中的区域（如首页 feed 双列卡片、右上角按钮）。",
                "写明可操作文案（无障碍/OCR 可见文字），避免仅写「进入详情」等抽象描述。",
                "若为无字图标，先在「无字图标」库录入后再执行。",
                "口语含「任意/随便」时，约定默认点第一个可见卡片或指定坐标规则。",
            ]
        )
    elif method and method in _METHOD_HINTS:
        causes.append(_METHOD_HINTS[method])

    assert_invalid = payload.get("assert_invalid")
    if assert_invalid or (role == "verify" and payload.get("ok") is False):
        if assert_invalid == "operation_failed":
            causes.append("前置 Tap/操作失败，但断言仍显示通过（历史数据或文案误匹配）。")
            suggestions.extend(
                [
                    "断言必须在前置操作成功后才有效；请重新执行用例验证。",
                    "在知识库补充详情页独有文案（如「立即购买」「作品详情」），避免首页误匹配。",
                ]
            )
        elif assert_invalid == "wrong_page":
            causes.append("界面仍为首页/列表，却断言已进入目标页（页面状态与预期矛盾）。")
            suggestions.extend(
                [
                    "补充详情页必须出现的界面特征（标题区、购买按钮、返回箭头等）。",
                    "补充从首页 feed 进入详情页的正确点击区域与操作步骤。",
                ]
            )
        else:
            causes.append("预期校验未通过，当前页面状态与用例描述不一致。")
            suggestions.extend(
                [
                    "补充进入目标页后的关键文案或界面特征。",
                    "说明需等待加载/动画结束后再断言的条件。",
                ]
            )

    if "未识别到指令" in step_text or "未识别" in msg:
        causes.append("步骤描述无法解析为可执行操作。")
        suggestions.append("将口语改写为「动作 + 具体目标」，如：在首页 feed 点击任意作品卡片。")

    if not causes:
        causes.append("步骤执行未成功，需补充本应用内的界面与操作说明。")

    if not suggestions:
        suggestions.append("根据截图补充该步骤在本应用中的正确操作路径与可点击目标。")

    category = _guess_category(combined)
    tags = _extract_tags(combined)
    if method == "none":
        tags = list(dict.fromkeys(tags + ["定位失败", "点击"]))
    if role == "verify":
        tags = list(dict.fromkeys(tags + ["断言失败"]))

    short_action = action_text or re.sub(r"^(Tap|Plan|Assert)\s*[-—]\s*", "", title_part)
    if method == "none" and short_action:
        k_title = f"点击「{short_action[:24]}」操作说明"
    elif case_name:
        k_title = f"纠错：{case_name[:28]}"
    else:
        k_title = f"纠错：{short_action[:28] or '失败步骤'}"

    content_parts = [
        f"【失败现象】{msg or '步骤执行失败'}",
        f"【用例】{case_name or '—'}",
        f"【步骤描述】{step_text or command or '—'}",
        f"【操作意图】{action_text or title_part or '—'}",
    ]
    if expected_text:
        content_parts.append(f"【预期】{expected_text}")
    if method:
        content_parts.append(f"【定位方式】{method}")
    content_parts.extend(
        [
            "",
            "【本应用正确操作方式】（请按实际界面补充，保存后规划执行将自动匹配）",
            "1. ",
            "2. ",
        ]
    )

    return {
        "analysis": "\n".join(f"• {c}" for c in causes),
        "suggestions": suggestions,
        "knowledge": {
            "title": k_title[:48],
            "category": category,
            "tags": tags[:8],
            "content": "\n".join(content_parts),
            "enabled": True,
        },
    }


def append_app_knowledge(app_id: str, item: Dict[str, Any]) -> Dict[str, Any]:
    """追加一条应用专属知识（app_ids 绑定）。"""
    aid = (app_id or "").strip()
    if not aid:
        raise ValueError("app_id 不能为空")
    title = (item.get("title") or "").strip()
    content = (item.get("content") or "").strip()
    if not title or not content:
        raise ValueError("标题与知识内容不能为空")

    existing = list_testing_knowledge()
    new_row = {
        "id": "",
        "title": title,
        "content": content,
        "category": (item.get("category") or "").strip() or "其他",
        "tags": [str(t).strip() for t in (item.get("tags") or []) if str(t).strip()],
        "app_ids": [aid],
        "enabled": item.get("enabled", True) is not False,
    }
    saved = save_testing_knowledge(existing + [new_row])
    for row in saved:
        if row.get("title") == title and aid in (row.get("app_ids") or []):
            return row
    return new_row
