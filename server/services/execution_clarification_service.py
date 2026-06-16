# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""执行遇阻时向用户提问，并将确认结果写入图标库与应用知识库。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from script.log import SLog
from server.services.failure_knowledge_service import append_app_knowledge

TAG = "ExecutionClarify"

LOGIN_ICON_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "登录-微信",
        "intent": "wechat",
        "aliases": ["微信", "微信登录", "微信登录方式", "WeChat", "wechat"],
        "note": "intent:wechat · 登录页底部图标（未安装微信时可能不显示）",
    },
    {
        "name": "登录-手机号验证码",
        "intent": "phone_sms",
        "aliases": [
            "手机号登录",
            "手机登录",
            "验证码登录",
            "手机号登录方式",
            "短信登录",
        ],
        "note": "intent:phone_sms · 登录页底部图标；未装微信时常为从左第 1 个",
    },
    {
        "name": "登录-账号密码",
        "intent": "email_password",
        "aliases": ["账号密码", "邮箱密码", "密码登录", "邮箱登录", "账号密码登录"],
        "note": "intent:email_password · 登录页底部图标",
    },
    {
        "name": "登录-Apple",
        "intent": "apple",
        "aliases": ["苹果", "Apple", "Apple ID", "appleid", "苹果登录", "苹果账号"],
        "note": "intent:apple · 登录页底部图标",
    },
]

_INTENT_LABEL = {
    "wechat": "微信登录",
    "phone_sms": "手机号/验证码登录",
    "email_password": "账号密码登录",
    "apple": "Apple 登录",
}


def ensure_default_login_icon_templates(db, app_id: str, app=None) -> Dict[str, Any]:
    """
    仅补全本地图标库占位（名称/别名），不请求 Figma。
    Figma 导入仅在「设计稿同步 / 应用逻辑学习 / 手动从 Figma 导入」时触发。
    """
    from server.models.app_icon_target import AppIconTarget
    from server.services.shared.icon_target_service import upsert_icon_target

    created = 0
    skipped = 0
    for tpl in LOGIN_ICON_TEMPLATES:
        name = tpl["name"]
        existing = (
            db.query(AppIconTarget)
            .filter(AppIconTarget.app_id == app_id, AppIconTarget.name == name)
            .first()
        )
        if existing:
            skipped += 1
            continue
        upsert_icon_target(
            db,
            app_id,
            {
                "name": name,
                "aliases": list(tpl.get("aliases") or []),
                "x": 0,
                "y": 0,
                "w": 0,
                "h": 0,
                "note": tpl.get("note") or "",
            },
        )
        created += 1
    if created:
        db.commit()
    return {"created": created, "skipped": skipped, "total_templates": len(LOGIN_ICON_TEMPLATES)}


def template_for_intent(intent: str) -> Optional[Dict[str, Any]]:
    for tpl in LOGIN_ICON_TEMPLATES:
        if tpl.get("intent") == intent:
            return tpl
    return None


def needs_clarification_for_step(step_text: str, action_block: Dict[str, Any]) -> bool:
    if action_block.get("ok"):
        return False
    try:
        from server.services.copilot_service import _classify_login_method_intent

        intent = _classify_login_method_intent(step_text or "")
        if intent and intent != "one_click":
            return True
    except Exception:
        pass
    msg = (action_block.get("msg") or "").lower()
    if "登录" in (step_text or "") and any(
        k in msg for k in ("未找到", "图标", "icon", "定位")
    ):
        return True
    return False


def build_login_icon_clarification(
    *,
    sn: str,
    platform: str,
    app_id: str,
    step_text: str,
    action_block: Dict[str, Any],
    run_id: str = "",
    case_name: str = "",
) -> Optional[Dict[str, Any]]:
    from server.services.copilot_service import (
        _classify_login_method_intent,
        _discover_login_icon_row,
    )
    from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

    intent = _classify_login_method_intent(step_text or "")
    if not intent or intent == "one_click":
        return None

    import builtins

    builtins.TARGET_DEVICE_SN = sn
    engine, (screen_w, screen_h) = bootstrap_mobile_engine(sn, platform)
    row = _discover_login_icon_row(engine, screen_w, screen_h)
    options: List[Dict[str, Any]] = []
    for idx, t in enumerate(row):
        cx, cy = t.center
        options.append(
            {
                "id": f"row_{idx}",
                "label": f"从左第 {idx + 1} 个图标",
                "x": int(t.x),
                "y": int(t.y),
                "w": int(t.w),
                "h": int(t.h),
                "center": [int(cx), int(cy)],
            }
        )

    screenshot = ""
    if run_id:
        try:
            from server.services.shared.screenshot.regression_capture import capture_device_screenshot

            screenshot = (
                capture_device_screenshot(
                    sn,
                    platform,
                    run_id=run_id,
                    tag="clarify_login_icon",
                )
                or ""
            )
        except Exception as e:
            SLog.w(TAG, f"clarify screenshot failed: {e}")

    intent_label = _INTENT_LABEL.get(intent, intent)
    tpl = template_for_intent(intent) or {}
    question = (
        f"无法自动定位「{intent_label}」入口（{action_block.get('msg') or '未找到可点击目标'}）。\n"
        f"请在下方选择当前屏幕上对应的图标，或说明正确操作；确认后将写入图标库与应用知识，供后续同类步骤复用。"
    )
    if not options:
        question += "\n（未扫描到底部图标行，请根据截图在图标库中手动标定坐标。）"

    return {
        "kind": "login_icon",
        "intent": intent,
        "intent_label": intent_label,
        "template_name": tpl.get("name") or f"登录-{intent}",
        "question": question,
        "step_text": step_text,
        "case_name": case_name,
        "fail_msg": action_block.get("msg") or "",
        "screenshot": screenshot,
        "screen_size": {"w": screen_w, "h": screen_h},
        "options": options,
        "allow_custom_rect": True,
        "suggested_aliases": list(tpl.get("aliases") or []),
    }


def apply_clarification_answer(
    db,
    app_id: str,
    clarification: Dict[str, Any],
    answer: Dict[str, Any],
) -> Dict[str, Any]:
    """
    应用用户确认：更新图标库坐标 + 写入应用知识库。
    answer: { option_id?, x?, y?, w?, h?, note? }
    """
    from server.services.shared.icon_target_service import upsert_icon_target

    intent = clarification.get("intent") or ""
    tpl = template_for_intent(intent) or {}
    name = (answer.get("name") or clarification.get("template_name") or tpl.get("name") or "").strip()
    if not name:
        raise ValueError("缺少图标名称")

    x = y = w = h = 0
    option_id = (answer.get("option_id") or "").strip()
    if option_id:
        for opt in clarification.get("options") or []:
            if opt.get("id") == option_id:
                x, y, w, h = int(opt["x"]), int(opt["y"]), int(opt["w"]), int(opt["h"])
                break
    if w <= 0 or h <= 0:
        x = int(answer.get("x") or 0)
        y = int(answer.get("y") or 0)
        w = int(answer.get("w") or 0)
        h = int(answer.get("h") or 0)
    if w <= 0 or h <= 0:
        raise ValueError("请选择图标选项或提供有效区域坐标")

    user_note = (answer.get("note") or "").strip()
    aliases = list(dict.fromkeys((tpl.get("aliases") or []) + (answer.get("aliases") or [])))
    from server.models.app_icon_target import AppIconTarget

    existing = (
        db.query(AppIconTarget)
        .filter(AppIconTarget.app_id == app_id, AppIconTarget.name == name)
        .first()
    )
    payload: Dict[str, Any] = {
        "name": name,
        "aliases": aliases,
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "note": (
            f"{tpl.get('note') or ''}; 人工标定"
            + (f"; {user_note}" if user_note else "")
        ).strip("; "),
    }
    if existing:
        payload["id"] = existing.id
    icon_row = upsert_icon_target(db, app_id, payload)
    db.commit()

    intent_label = clarification.get("intent_label") or intent
    step_text = clarification.get("step_text") or ""
    cx, cy = x + w // 2, y + h // 2
    knowledge_content = "\n".join(
        [
            f"【场景】登录页底部无字图标 · {intent_label}",
            f"【步骤】{step_text}",
            f"【人工确认】点击区域中心约 ({cx},{cy})，区域 {w}×{h}",
            f"【说明】{user_note or '执行时由用户确认图标位置'}",
            "【复用】后续同类「登录方式」步骤优先 icon_target 匹配；"
            "若未安装微信等导致图标缺失/左移，仍以本次标定或语义匹配为准。",
        ]
    )
    knowledge = append_app_knowledge(
        app_id,
        {
            "title": f"登录图标 · {intent_label}"[:48],
            "category": "登录注册",
            "tags": ["登录", "图标", intent, "人工确认"],
            "content": knowledge_content,
            "enabled": True,
        },
    )
    SLog.i(TAG, f"clarification applied app={app_id} intent={intent} icon={name}")
    return {
        "icon": icon_row,
        "knowledge": knowledge,
        "center": [cx, cy],
    }
