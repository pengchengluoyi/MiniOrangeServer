# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""页面导航：识别当前页/目标页、规划路径、执行恢复后再断言。"""
from __future__ import annotations

import json
import re
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from script.log import SLog
from server.services.shared.page_context.page_context_service import (
    extract_page_tokens,
    expected_matches_page,
    find_graph_node_by_label,
    identify_for_app,
    is_navigation_expectation,
    load_app_graph_by_app_id,
)

TAG = "PageNavigation"

BOTTOM_TAB_LABELS = ["首页", "消息", "我的", "想要"]
SEGMENT_TAB_LABELS = ["造物秀", "AI创意", "想要成真"]
OVERLAY_MARKERS = ["隐私政策", "服务协议", "个人信息保护"]
OVERLAY_DISMISS = ["同意", "同意并继续", "接受", "我知道了", "继续"]
CONSENT_DIALOG_MARKERS = ("不同意", "同意并继续", "点击\"同意\"", "点击“同意”")
_GENERIC_LEGAL_MARKERS = (
    "隐私",
    "协议",
    "条款",
    "政策",
    "个人信息",
    "服务协议",
    "用户协议",
    "隐私政策",
    "隐私条款",
)
_CONSENT_DISAGREE_LABELS = ("不同意", "拒绝", "暂不使用")
_CONSENT_AGREE_LABELS = ("同意", "同意并继续", "接受", "我知道了")
LEGAL_LINK_MARKERS = ["用户协议", "隐私条款", "隐私政策", "服务协议"]
BACK_MARKERS = ["返回", "关闭", "取消"]


def _engine_screen_size(engine) -> Tuple[int, int]:
    if hasattr(engine, "screen_size"):
        return engine.screen_size()
    if hasattr(engine, "_display_size"):
        return engine._display_size()
    return 1080, 1920


def _screen_is_phone_login_form(screen_text: str) -> bool:
    """手机号登录表单（非启动 consent 弹窗）。"""
    blob = screen_text or ""
    if "请输入手机号" in blob or "请输入验证码" in blob:
        return True
    if "手机号" in blob and "验证码" in blob and "不同意" not in blob:
        return True
    hits = sum(
        1
        for m in (
            "请输入手机号",
            "手机号登录",
            "下一步",
            "+86",
            "已仔细阅读并同意",
            "发送验证码",
        )
        if m in blob
    )
    return hits >= 2


def _screen_is_login_surface(screen_text: str) -> bool:
    """已在登录流程界面（含手机号表单 / 登录首页），不是启动隐私弹窗。"""
    blob = screen_text or ""
    if _screen_is_phone_login_form(blob) or _screen_is_verification_code_page(blob):
        return True
    if _screen_is_login_home(blob):
        return True
    if "其他登录方式" in blob and ("登录" in blob or "微信" in blob):
        return True
    return False


def _screen_is_verification_code_page(screen_text: str) -> bool:
    blob = screen_text or ""
    return any(
        m in blob
        for m in ("验证码已发送", "验证码已送至", "重新发送", "输入验证码", "验证码")
    ) and "请输入手机号" not in blob


def _clip_tap_label(
    engine,
    screen_w: int,
    screen_h: int,
    label: str,
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    region: Optional[str] = None,
    phase: str = "navigation",
    source: str = "clip_nav",
    exact_label: bool = False,
) -> Dict[str, Any]:
    """PageNavigation 统一点击：CLIP-first（走 copilot _resolve_click_target）。"""
    from server.services.copilot_service import _resolve_click_target
    from server.services.shared.run_context.regression_run_context import finish_gesture, record_gesture

    pos, method, detail, rect = _resolve_click_target(
        engine,
        screen_w,
        screen_h,
        label=label,
        icon_targets=icon_targets,
        exact_label=exact_label,
    )
    if not pos:
        return {
            "ok": False,
            "method": method or "clip",
            "msg": detail or f"CLIP 未定位「{label}」",
            "x": 0,
            "y": 0,
        }
    cx, cy = int(pos[0]), int(pos[1])
    gesture = record_gesture(
        "click",
        f"Tap · {label}",
        method=method or "clip",
        x=cx,
        y=cy,
        label=label,
        source=source,
        phase=phase,
        extra={"action_name": "Tap", "detail": detail},
    )
    ok = False
    try:
        if hasattr(engine, "click"):
            ok = bool(
                engine.click(
                    None,
                    position=(cx, cy),
                    label=label,
                    skip_label_lookup=True,
                    consent_dismiss=("同意" in label),
                )
            )
    except Exception as e:
        SLog.w(TAG, f"clip tap failed label={label!r}: {e}")
    half = 44
    gesture["screen_size"] = {"w": screen_w, "h": screen_h}
    gesture["target_rect"] = rect or {
        "left": max(0, cx - half),
        "top": max(0, cy - half),
        "width": half * 2,
        "height": half * 2,
        "center": [cx, cy],
        "label": label,
    }
    gesture["action_name"] = "Tap"
    settle = 500 if phase == "overlay_guard" else (900 if "同意" in (label or "") else 450)
    finish_gesture(
        gesture,
        ok=ok,
        msg=f"Tap「{label}」@({cx},{cy}) [{method}]" if ok else (detail or "点击失败"),
        settle_ms=settle,
    )
    try:
        from server.services.executor.locate_debug import pop_locate_debug

        dbg = pop_locate_debug()
        if dbg:
            gesture["locate_debug"] = dbg
    except Exception:
        pass
    return {
        "ok": ok,
        "method": method or "clip",
        "msg": gesture.get("msg") or detail,
        "x": cx,
        "y": cy,
        "gesture": gesture,
        "target_rect": gesture.get("target_rect"),
        "locate_debug": gesture.get("locate_debug"),
    }


def _collect_ocr_text_only(engine, *, force: bool = False) -> str:
    """仅截图 OCR（可见内容），回归批次内复用屏快照。"""
    try:
        from server.services.shared.page_context.page_context_service import collect_ocr_text

        return collect_ocr_text(engine, force=force)
    except Exception as e:
        SLog.w(TAG, f"ocr-only collect failed: {e}")
        return ""


def _is_legal_bearing_text(text: str) -> bool:
    """含协议/条款链接或 consent 正文的长文案，不可当作「同意」按钮。"""
    t = (text or "").strip()
    if not t:
        return False
    if any(k in t for k in LEGAL_LINK_MARKERS):
        return True
    if len(t) > 10 and any(k in t for k in ("《", "》", "阅读", "点击", "条款", "协议", "隐私")):
        return True
    return False


def _hierarchy_consent_button_pair(engine, screen_h: int) -> Tuple[bool, bool]:
    """检测底部是否同时存在「不同意」「同意」按钮（consent 弹窗最可靠信号）。"""
    try:
        xml_data = ""
        if hasattr(engine, "dump_hierarchy_xml"):
            xml_data = engine.dump_hierarchy_xml() or ""
        if not xml_data or "<?xml" not in xml_data:
            engine.shell("uiautomator dump /sdcard/view.xml")
            xml_data = engine.shell("cat /sdcard/view.xml")
        if not xml_data or "<?xml" not in xml_data:
            return False, False
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_data)
        has_disagree = False
        has_agree = False
        for node in root.iter("node"):
            text = (node.get("text") or node.get("content-desc") or "").strip()
            if not text or _is_legal_bearing_text(text):
                continue
            if text == "不同意" or text.startswith("不同意"):
                has_disagree = True
            if text in ("同意", "同意并继续"):
                has_agree = True
        return has_disagree, has_agree
    except Exception as e:
        SLog.w(TAG, f"consent button scan failed: {e}")
        return False, False


def _consent_button_y_band(screen_h: int) -> Tuple[int, int]:
    """consent 居中弹窗的按钮行通常在屏幕中部，而非物理底栏。"""
    return int(screen_h * 0.38), int(screen_h * 0.78)


def _hierarchy_find_consent_button_pair(
    engine,
    screen_w: int,
    screen_h: int,
) -> Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int]]]:
    """返回 (不同意中心, 同意中心)；仅底栏短文案按钮。"""
    disagree_pos: Optional[Tuple[int, int]] = None
    agree_pos: Optional[Tuple[int, int]] = None
    try:
        xml_data = ""
        if hasattr(engine, "dump_hierarchy_xml"):
            xml_data = engine.dump_hierarchy_xml() or ""
        if not xml_data or "<?xml" not in xml_data:
            engine.shell("uiautomator dump /sdcard/view.xml")
            xml_data = engine.shell("cat /sdcard/view.xml")
        if not xml_data or "<?xml" not in xml_data:
            return None, None
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_data)
        for node in root.iter("node"):
            text = (node.get("text") or node.get("content-desc") or "").strip()
            if not text or _is_legal_bearing_text(text):
                continue
            nums = re.findall(r"\d+", node.get("bounds") or "")
            if len(nums) != 4:
                continue
            x1, y1, x2, y2 = map(int, nums)
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            if text == "不同意" or text.startswith("不同意"):
                disagree_pos = (cx, cy)
            elif text in ("同意", "同意并继续"):
                agree_pos = (cx, cy)
    except Exception as e:
        SLog.w(TAG, f"consent button pair scan failed: {e}")
    return disagree_pos, agree_pos


def _hierarchy_find_consent_agree_button(
    engine,
    screen_w: int,
    screen_h: int,
) -> Optional[Tuple[int, int]]:
    """底栏「不同意+同意」成对出现时，只点右侧的「同意」。"""
    disagree, agree = _hierarchy_find_consent_button_pair(engine, screen_w, screen_h)
    if disagree and agree and agree[0] > disagree[0]:
        return agree
    if agree:
        return agree
    return None


def _hierarchy_has_app_consent_modal(
    engine,
    screen_w: Optional[int] = None,
    screen_h: Optional[int] = None,
) -> bool:
    """无障碍树同时存在「不同意」「同意」→ 应用隐私弹窗（不依赖 OCR 全文）。"""
    if engine is None:
        return False
    if screen_w is None or screen_h is None:
        screen_w, screen_h = _engine_screen_size(engine)
    disagree, agree = _hierarchy_find_consent_button_pair(engine, screen_w, screen_h)
    return bool(disagree and agree)


def get_blocking_screen_state(engine, *, force: bool = False) -> Dict[str, Any]:
    """
    判断当前是否被 consent / 协议 / 权限弹窗阻塞。
    同一手势水位内缓存，与 get_engine_screen_snapshot 共用一次 OCR。
    """
    try:
        from server.services.shared.page_context.page_context_service import get_engine_screen_snapshot
        from server.services.shared.screenshot.screen_frame_service import screen_frame_watermark

        wm = screen_frame_watermark()
        if not force and engine is not None:
            cached = getattr(engine, "_mo_blocking_state", None)
            if isinstance(cached, dict) and cached.get("wm") == wm and cached.get("state"):
                return dict(cached["state"])

        snap = get_engine_screen_snapshot(engine, force=force)
        screen_w = int(snap.get("screen_w") or 0) or _engine_screen_size(engine)[0]
        screen_h = int(snap.get("screen_h") or 0) or _engine_screen_size(engine)[1]

        if snap.get("screen_not_ready"):
            state = {
                "consent": False,
                "agreement": False,
                "ocr_text": "",
                "ocr_len": 0,
                "has_disagree_btn": False,
                "has_agree_btn": False,
                "reason": snap.get("reason") or "screen_not_ready",
                "screen_w": screen_w,
                "screen_h": screen_h,
                "layer_scores": {},
                "screen_not_ready": True,
            }
            if engine is not None:
                engine._mo_blocking_state = {"wm": wm, "state": state}
            return state

        ocr_items = list(snap.get("ocr_items") or [])
        state = _blocking_state_from_ocr_items(ocr_items, screen_w, screen_h)
        if engine is not None:
            engine._mo_blocking_state = {"wm": wm, "state": state}
        return state
    except Exception as e:
        SLog.w(TAG, f"get_blocking_screen_state failed: {e}")
        screen_w, screen_h = _engine_screen_size(engine)
        return {
            "consent": False,
            "agreement": False,
            "ocr_text": "",
            "ocr_len": 0,
            "has_disagree_btn": False,
            "has_agree_btn": False,
            "reason": "error",
            "screen_w": screen_w,
            "screen_h": screen_h,
            "layer_scores": {},
            "screen_not_ready": False,
        }


def classify_blocking_screen(engine) -> Dict[str, Any]:
    """兼容旧调用方；内部走带缓存的 get_blocking_screen_state。"""
    return get_blocking_screen_state(engine)


def _blocking_state_from_ocr_items(
    ocr_items: List[Dict[str, Any]],
    screen_w: int,
    screen_h: int,
) -> Dict[str, Any]:
    """由 OCR 条目计算阻塞屏状态（不重复截图）。"""
    ocr = "\n".join(
        (it.get("text") or "").strip()
        for it in ocr_items
        if (it.get("text") or "").strip()
    )
    layer = _analyze_consent_modal_layout(ocr_items, screen_h)
    scores = layer["scores"]
    consent_actions = layer.get("consent_actions") or set()
    legal_in_modal = int(layer.get("legal_in_modal") or 0)
    has_disagree = "disagree" in consent_actions
    has_agree = "agree" in consent_actions
    is_consent_ocr = _screen_is_consent_dialog(ocr)

    consent_score = float(scores.get("consent") or 0)
    login_score = float(scores.get("login") or 0)
    permission_score = float(scores.get("permission") or 0)

    consent = False
    agreement = False
    reason = "none"

    # 隐私弹窗叠在登录页上时，中部「不同意+同意」优先于底栏登录控件
    if _screen_is_login_confirm_sheet(ocr):
        consent = False
        reason = "login_confirm_sheet"
    elif _screen_is_startup_consent_modal(ocr):
        consent = True
        reason = "startup_consent_modal"
    elif has_disagree and has_agree and (
        consent_score >= 5.0
        or legal_in_modal >= 1
        or consent_score >= login_score + 1.5
    ):
        consent = True
        reason = "smart_consent_modal"
    elif _screen_is_login_surface(ocr) and not (has_disagree and has_agree):
        consent = False
        if _screen_is_phone_login_form(ocr) or _screen_is_verification_code_page(ocr):
            reason = "phone_login_or_sms"
        elif _screen_is_login_home(ocr):
            reason = "login_home"
    elif (
        permission_score >= 2.0
        and consent_score < 5.0
        and not _screen_is_app_consent_or_privacy_overlay(ocr)
    ):
        reason = "system_permission"
    elif has_disagree and has_agree and consent_score >= 6.0 and consent_score >= login_score + 2.0:
        consent = True
        reason = "smart_consent_modal"
    elif is_consent_ocr and has_disagree and has_agree:
        consent = True
        reason = "smart_consent_text"
    elif _screen_is_user_agreement_page(ocr):
        agreement = True
        reason = "ocr_agreement"
    elif _screen_is_phone_login_form(ocr) or _screen_is_verification_code_page(ocr):
        if consent_score < login_score + 1.5:
            reason = "phone_login_or_sms"
    elif _screen_is_login_home(ocr) and consent_score < 5.0:
        reason = "login_home"

    return {
        "consent": consent,
        "agreement": agreement,
        "ocr_text": ocr,
        "ocr_len": len(ocr or ""),
        "has_disagree_btn": has_disagree,
        "has_agree_btn": has_agree,
        "reason": reason,
        "screen_w": screen_w,
        "screen_h": screen_h,
        "layer_scores": scores,
    }


def resolve_target_page_from_expected(
    expected: str,
    app_graph: Dict[str, Any],
    figma_logic: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """从预期文案 + Figma/图谱解析目标页面。"""
    tokens = extract_page_tokens(expected)
    if not tokens and is_navigation_expectation(expected):
        tokens = [expected[:20]]

    candidates: List[Dict[str, Any]] = []
    for tok in tokens:
        node = find_graph_node_by_label(app_graph, tok)
        if node:
            candidates.append(
                {
                    "node_id": node.get("id"),
                    "label": node.get("label"),
                    "source": "graph",
                    "token": tok,
                }
            )
        if figma_logic:
            for page in figma_logic.get("pages") or []:
                name = (page.get("name") or "").strip()
                if tok in name or name in tok:
                    candidates.append(
                        {
                            "node_id": page.get("node_id"),
                            "label": name,
                            "source": "figma",
                            "token": tok,
                            "page": page,
                        }
                    )
    if not candidates:
        return None
    candidates.sort(key=lambda c: len(c.get("label") or ""))
    return candidates[0]


def find_graph_path(
    edges: List[Dict[str, Any]],
    start_id: str,
    goal_id: str,
    *,
    max_depth: int = 6,
) -> List[Dict[str, Any]]:
    """BFS 查找图谱上的导航边序列。"""
    if not start_id or not goal_id or start_id == goal_id:
        return []
    adj: Dict[str, List[Dict[str, Any]]] = {}
    for e in edges or []:
        src = e.get("source")
        if src:
            adj.setdefault(str(src), []).append(e)

    queue = deque([(start_id, [])])
    seen = {start_id}
    while queue:
        node, path = queue.popleft()
        if len(path) >= max_depth:
            continue
        for edge in adj.get(node, []):
            nxt = str(edge.get("target") or "")
            if not nxt or nxt in seen:
                continue
            new_path = path + [edge]
            if nxt == goal_id:
                return new_path
            seen.add(nxt)
            queue.append((nxt, new_path))
    return []


def _parse_trigger(edge: Dict[str, Any]) -> Dict[str, Any]:
    raw = edge.get("trigger")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return {"label": raw}
    label = (edge.get("label") or "").strip()
    return {"type": "click", "label": label} if label else {}


def edge_to_click_step(edge: Dict[str, Any], *, index: int = 0) -> Dict[str, Any]:
    trig = _parse_trigger(edge)
    label = (trig.get("label") or edge.get("label") or "导航").strip()
    return {
        "kind": "click",
        "label": label,
        "summary": f"导航：点击「{label}」",
        "index": index,
        "nav_source": edge.get("source"),
        "nav_target": edge.get("target"),
    }


def _screen_is_overlay(screen_text: str) -> bool:
    if _screen_is_consent_dialog(screen_text):
        return True
    blob = screen_text or ""
    return any(m in blob for m in OVERLAY_MARKERS)


def _screen_is_login_home(screen_text: str) -> bool:
    """已进入登录主页（非启动 consent 弹窗）。"""
    blob = screen_text or ""
    return any(
        m in blob
        for m in (
            "一键登录",
            "本机号码",
            "手机号登录",
            "验证码登录",
            "其他登录",
            "其他方式登录",
            "其他登录方式",
            "请输入手机号",
        )
    )


def _is_short_consent_action(text: str, candidates: Tuple[str, ...]) -> bool:
    t = (text or "").strip()
    if not t or len(t) > 10:
        return False
    return t in candidates or any(t.startswith(c) for c in candidates if len(c) <= 4)


def _screen_is_consent_dialog(screen_text: str) -> bool:
    """
    启动隐私弹窗：必须同时出现「不同意」类 + 「同意」类操作按钮。
    登录页底部协议勾选只有同意文案，不能判为 consent 弹窗。
    弹窗叠在登录首页上时底栏仍可能被 OCR 读到，不能因 login_surface 误判为非阻塞。
    """
    blob = screen_text or ""
    has_disagree = any(m in blob for m in _CONSENT_DISAGREE_LABELS)
    has_agree = any(m in blob for m in _CONSENT_AGREE_LABELS)
    return bool(has_disagree and has_agree)


def _screen_is_startup_consent_modal(screen_text: str) -> bool:
    """冷启动全屏隐私弹窗（造物者 + 协议文案，常含不同意/同意）。"""
    blob = screen_text or ""
    if _screen_is_login_confirm_sheet(blob):
        return False
    if _screen_is_consent_dialog(blob):
        return True
    if "造物者" in blob and any(m in blob for m in _GENERIC_LEGAL_MARKERS):
        if any(m in blob for m in _CONSENT_DISAGREE_LABELS) or "同意" in blob:
            return True
    return False


def _screen_is_login_confirm_sheet(screen_text: str) -> bool:
    """
    登录页底部二次确认 sheet（仅「同意并继续」，无「不同意」）。
    与冷启动隐私弹窗区分：此时登录主界面（本机号码/一键登录）已可见。
    """
    blob = screen_text or ""
    if any(m in blob for m in _CONSENT_DISAGREE_LABELS):
        return False
    if "同意并继续" not in blob:
        return False
    if not (_screen_is_login_home(blob) or _screen_is_login_surface(blob)):
        return False
    return any(
        m in blob
        for m in (
            "本机号码",
            "一键登录",
            "+86",
            "手机号登录",
            "验证码登录",
        )
    )


def is_blocking_login_confirm_screen(
    engine=None,
    *,
    screen_state: Optional[Dict[str, Any]] = None,
    screen_text: str = "",
) -> bool:
    """登录页「同意并继续」底 sheet 阻塞业务点击。"""
    state = screen_state or (classify_blocking_screen(engine) if engine is not None else {})
    blob = (state.get("ocr_text") or screen_text or "").strip()
    if state.get("has_disagree_btn") or any(m in blob for m in _CONSENT_DISAGREE_LABELS):
        return False
    if (
        state.get("consent")
        or _screen_is_startup_consent_modal(blob)
        or _screen_is_consent_dialog(blob)
    ):
        return False
    if (state.get("reason") or "") == "login_confirm_sheet":
        return True
    return _screen_is_login_confirm_sheet(blob)


def is_blocking_consent_screen(
    engine=None,
    *,
    screen_state: Optional[Dict[str, Any]] = None,
    screen_text: str = "",
) -> bool:
    """当前是否被启动隐私弹窗阻塞（统一入口，避免各模块规则不一致）。"""
    state = screen_state or (classify_blocking_screen(engine) if engine is not None else {})
    blob = (state.get("ocr_text") or screen_text or "").strip()
    if engine is not None and _hierarchy_has_app_consent_modal(engine):
        return True
    if is_blocking_login_confirm_screen(screen_state=state, screen_text=blob):
        return False
    if state.get("consent"):
        return True
    if state.get("has_disagree_btn") and state.get("has_agree_btn"):
        return True
    if _screen_is_app_consent_or_privacy_overlay(blob):
        return True
    if _screen_is_login_surface(blob):
        return False
    return False


def _screen_is_user_agreement_page(screen_text: str) -> bool:
    """协议全文页（WebView），与 consent 弹窗严格区分。"""
    blob = screen_text or ""
    if _screen_is_consent_dialog(blob):
        return False
    if any(m in blob for m in _CONSENT_DISAGREE_LABELS):
        return False
    if "平台用户协议" in blob or "造好物 - 平台" in blob or "造好物- 平台" in blob:
        return True
    if any(k in blob for k in ("发布日期", "更新日期", "生效日期")) and "协议" in blob:
        return True
    if "用户协议" in blob and len(blob) > 280 and "隐私条款" not in blob[:120]:
        return True
    return False


def _ocr_box_center(item: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    bounds = _ocr_box_bounds(item)
    if not bounds:
        return None
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2


def _ocr_box_bounds(item: Dict[str, Any]) -> Optional[Tuple[int, int, int, int]]:
    coords = item.get("coordinates") or {}
    box = coords.get("box") or item.get("box")
    if not box:
        return None
    try:
        xs = [int(p[0]) for p in box]
        ys = [int(p[1]) for p in box]
    except (TypeError, ValueError, IndexError):
        return None
    if not xs or not ys:
        return None
    return min(xs), min(ys), max(xs), max(ys)


_CONSENT_MODAL_BAND = (0.12, 0.88)
_LOGIN_LOWER_BAND = (0.60, 1.02)
_LOGIN_SURFACE_MARKERS = (
    "一键登录",
    "本机号码",
    "手机号登录",
    "验证码登录",
    "其他登录",
    "其他方式登录",
    "访客浏览",
    "微信登录",
    "账号密码",
    "请输入手机号",
    "+86",
    "已仔细阅读并同意",
)


def _collect_ocr_layout(engine, *, force: bool = False) -> Tuple[List[Dict[str, Any]], int, int]:
    """复用屏快照 OCR 条目（带框），避免重复截图 analyze。"""
    try:
        from server.services.shared.page_context.page_context_service import get_engine_screen_snapshot

        snap = get_engine_screen_snapshot(engine, force=force)
        return (
            list(snap.get("ocr_items") or []),
            int(snap.get("screen_w") or 0) or _engine_screen_size(engine)[0],
            int(snap.get("screen_h") or 0) or _engine_screen_size(engine)[1],
        )
    except Exception as e:
        SLog.w(TAG, f"ocr layout collect failed: {e}")
        screen_w, screen_h = _engine_screen_size(engine)
        return [], screen_w, screen_h


def _ocr_item_ycenter_norm(item: Dict[str, Any], screen_h: int) -> Optional[float]:
    bounds = _ocr_box_bounds(item)
    if not bounds or screen_h <= 0:
        return None
    return ((bounds[1] + bounds[3]) / 2) / screen_h


def _analyze_consent_modal_layout(
    ocr_items: List[Dict[str, Any]],
    screen_h: int,
) -> Dict[str, Any]:
    """
    通用隐私弹窗结构识别（与 App 文案无关）：
    - 中部出现「不同意 + 同意」短按钮对（同一水平带）
    - 中部有隐私/协议类说明长文案
    - 底栏登录控件单独计分，不与弹窗按钮混淆
    """
    scores = {"consent": 0.0, "login": 0.0, "permission": 0.0, "agreement": 0.0}
    consent_actions: set = set()
    disagree_yn: Optional[float] = None
    agree_yn: Optional[float] = None
    legal_in_modal = 0

    for it in ocr_items:
        text = (it.get("text") or "").strip()
        if not text:
            continue
        yn = _ocr_item_ycenter_norm(it, screen_h)
        if yn is None:
            continue

        if any(m in text for m in SYSTEM_PERMISSION_STRONG_MARKERS):
            scores["permission"] += 2.5
            continue

        in_modal = _CONSENT_MODAL_BAND[0] <= yn <= _CONSENT_MODAL_BAND[1]
        in_lower = yn >= _LOGIN_LOWER_BAND[0]

        if in_modal and len(text) >= 6 and any(m in text for m in _GENERIC_LEGAL_MARKERS):
            legal_in_modal += 1
            scores["consent"] += 1.5

        if _is_short_consent_action(text, _CONSENT_DISAGREE_LABELS) and in_modal:
            scores["consent"] += 3.5
            consent_actions.add("disagree")
            disagree_yn = yn
        elif _is_short_consent_action(text, _CONSENT_AGREE_LABELS) and in_modal:
            scores["consent"] += 3.5
            consent_actions.add("agree")
            agree_yn = yn
        elif _is_short_consent_action(text, _CONSENT_AGREE_LABELS) and in_lower:
            scores["login"] += 0.5

        if any(m in text for m in _LOGIN_SURFACE_MARKERS):
            if in_lower:
                scores["login"] += 2.0
            elif in_modal:
                scores["login"] += 0.3

    if "disagree" in consent_actions and "agree" in consent_actions:
        scores["consent"] += 5.0
        if (
            disagree_yn is not None
            and agree_yn is not None
            and abs(disagree_yn - agree_yn) < 0.14
        ):
            scores["consent"] += 4.0
    if legal_in_modal >= 1 and consent_actions:
        scores["consent"] += 2.0

    return {
        "scores": scores,
        "consent_actions": consent_actions,
        "disagree_yn": disagree_yn,
        "agree_yn": agree_yn,
        "legal_in_modal": legal_in_modal,
    }


def consent_modal_visible(engine) -> bool:
    """当前屏是否被通用隐私 consent 弹窗遮挡。"""
    return is_blocking_consent_screen(engine)


def _is_consent_action_label(text: str) -> bool:
    t = (text or "").strip()
    if not t or _is_legal_bearing_text(t):
        return False
    return t in ("同意", "同意并继续") or t.startswith("不同意")


def _node_bounds_center_area(
    bounds: Dict[str, Any],
) -> Tuple[int, int, int]:
    x1 = int(bounds.get("left", 0))
    y1 = int(bounds.get("top", 0))
    x2 = int(bounds.get("right", 0))
    y2 = int(bounds.get("bottom", 0))
    cx = (x1 + x2) // 2
    cy = (y1 + y2) // 2
    area = max(1, (x2 - x1) * (y2 - y1))
    return cx, cy, area


def _pick_u2_consent_agree_candidate(
    d,
    screen_w: int,
    screen_h: int,
) -> Optional[Tuple[Any, int, int, str]]:
    """
    从 u2 节点中选出 consent「同意」/「同意并继续」主按钮。
    仅排除协议正文链接，不再用屏幕百分比硬过滤位置。
    """
    has_disagree = False
    try:
        sel_d = d(text="不同意")
        if sel_d.exists(timeout=0.4):
            has_disagree = True
    except Exception:
        pass

    candidates: List[Tuple[Any, int, int, int, str]] = []
    for label in ("同意并继续", "同意"):
        try:
            sel = d(text=label)
            if not sel.exists(timeout=1.0):
                continue
            count = int(getattr(sel, "count", 1) or 1)
            for idx in range(min(count, 12)):
                node = sel[idx] if count > 1 else sel
                info = node.info or {}
                node_text = (info.get("text") or info.get("contentDescription") or "").strip()
                if not _is_consent_action_label(node_text) and not _is_consent_action_label(label):
                    continue
                if _is_legal_bearing_text(node_text):
                    continue
                bounds = info.get("bounds") or {}
                if not bounds:
                    continue
                cx, cy, area = _node_bounds_center_area(bounds)
                candidates.append((node, cx, cy, area, label))
        except Exception as e:
            SLog.w(TAG, f"u2 consent candidate scan failed label={label!r}: {e}")

    if not candidates:
        return None

    if has_disagree:
        agree_nodes = [c for c in candidates if c[4] in ("同意", "同意并继续")]
        if not agree_nodes:
            return None
        node, cx, cy, _, label = max(agree_nodes, key=lambda c: c[1])
        return node, cx, cy, label

    node, cx, cy, area, label = max(
        candidates,
        key=lambda c: (
            2 if c[4] == "同意并继续" else 1,
            c[2],
            c[3],
        ),
    )
    return node, cx, cy, label


def _ocr_legal_link_boxes(
    engine,
    screen_h: int,
) -> List[Tuple[int, int, int, int]]:
    """正文区《用户协议》《隐私条款》链接包围盒（须避开，误点会进协议页）。"""
    boxes: List[Tuple[int, int, int, int]] = []
    try:
        if not hasattr(engine, "screenshot"):
            return boxes
        shot = engine.screenshot()
        if shot is None:
            return boxes
        from driver.agent.Crawl.ui_discovery import _ocr_analyze_shot

        y_body_max = int(screen_h * 0.82)
        for it in _ocr_analyze_shot(shot) or []:
            text = (it.get("text") or "").strip()
            if not text:
                continue
            is_link = any(k in text for k in LEGAL_LINK_MARKERS) or (
                "《" in text and "》" in text
            )
            if not is_link:
                continue
            coords = it.get("coordinates") or {}
            box = coords.get("box") or it.get("box")
            if not box:
                continue
            xs = [int(p[0]) for p in box]
            ys = [int(p[1]) for p in box]
            cy = int(sum(ys) / len(ys))
            if cy > y_body_max:
                continue
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
    except Exception as e:
        SLog.w(TAG, f"ocr legal link boxes failed: {e}")
    return boxes


def _point_near_boxes(
    x: int,
    y: int,
    boxes: List[Tuple[int, int, int, int]],
    *,
    margin: int = 20,
    screen_h: int = 0,
) -> bool:
    """仅当落点在对话框正文区（非按钮行）且靠近协议链接时才拒绝。"""
    if screen_h:
        y_lo, y_hi = _consent_button_y_band(screen_h)
        if y_lo <= y <= y_hi:
            return False
    for x1, y1, x2, y2 in boxes:
        if x1 - margin <= x <= x2 + margin and y1 - margin <= y <= y2 + margin:
            return True
    return False


def _ocr_find_consent_button_pair(
    engine,
    screen_w: int,
    screen_h: int,
) -> Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int]]]:
    """OCR 定位底栏「不同意」「同意」按钮中心。"""
    disagree_pos: Optional[Tuple[int, int]] = None
    agree_pos: Optional[Tuple[int, int]] = None
    try:
        if not hasattr(engine, "screenshot"):
            return None, None
        shot = engine.screenshot()
        if shot is None:
            return None, None
        from driver.agent.Crawl.ui_discovery import _ocr_analyze_shot

        for it in _ocr_analyze_shot(shot) or []:
            text = (it.get("text") or "").strip()
            center = _ocr_box_center(it)
            if not center:
                continue
            cx, cy = center
            if text == "不同意" or text.startswith("不同意"):
                disagree_pos = (cx, cy)
            elif text in ("同意", "同意并继续"):
                agree_pos = (cx, cy)
    except Exception as e:
        SLog.w(TAG, f"ocr consent button pair failed: {e}")
    return disagree_pos, agree_pos


def _ocr_find_consent_agree_button(
    engine,
    screen_w: int,
    screen_h: int,
) -> Optional[Tuple[int, int]]:
    disagree, agree = _ocr_find_consent_button_pair(engine, screen_w, screen_h)
    if disagree and agree and agree[0] > disagree[0]:
        return agree
    if agree:
        return agree
    return None


def resolve_consent_agree_tap(
    engine,
    screen_w: int,
    screen_h: int,
) -> Optional[Tuple[int, int, str]]:
    """
    解析 consent 弹窗「同意」按钮坐标。
    优先 hierarchy/OCR 成对按钮（右侧同意），再走文本相似度；绝不误点「不同意」。
    返回 (x, y, method)。
    """
    from server.services.copilot_service import (
        _label_similarity,
        _pick_best_text_clickable,
    )

    pair = _hierarchy_find_consent_agree_button(engine, screen_w, screen_h)
    if pair:
        return pair[0], pair[1], "hierarchy_pair"
    pair = _ocr_find_consent_agree_button(engine, screen_w, screen_h)
    if pair:
        return pair[0], pair[1], "ocr_pair"

    legal_boxes = _ocr_legal_link_boxes(engine, screen_h)
    query = "同意"

    def _accept(
        cx: int,
        cy: int,
        txt: str,
        score: float,
        method_name: str,
    ) -> Optional[Tuple[int, int, str]]:
        if _point_near_boxes(cx, cy, legal_boxes, screen_h=screen_h):
            SLog.w(
                TAG,
                f"consent agree candidate ({cx},{cy}) overlaps legal link, skip",
            )
            return None
        SLog.i(
            TAG,
            f"consent agree sim label={txt!r} score={score:.3f} @({cx},{cy}) [{method_name}]",
        )
        return cx, cy, method_name

    try:
        from driver.agent.Crawl.ui_discovery import (
            discover_clickables_from_hierarchy,
            discover_clickables_ocr,
        )

        for pool, method in (
            (
                list(
                    discover_clickables_from_hierarchy(
                        engine, screen_w, screen_h, max_items=64
                    )
                ),
                "hierarchy",
            ),
        ):
            pick = _pick_best_text_clickable(
                query,
                pool,
                screen_h=screen_h,
                consent_modal=True,
                max_label_len=12,
            )
            if pick:
                cx, cy, txt, score, _t = pick
                hit = _accept(cx, cy, txt, score, f"label_sim_{method}")
                if hit:
                    return hit

        if hasattr(engine, "screenshot"):
            shot = engine.screenshot()
            if shot is not None:
                ocr_pool = list(
                    discover_clickables_ocr(shot, screen_w, screen_h, max_items=32)
                )
                pick = _pick_best_text_clickable(
                    query,
                    ocr_pool,
                    screen_h=screen_h,
                    consent_modal=True,
                    max_label_len=12,
                )
                if pick:
                    cx, cy, txt, score, _t = pick
                    hit = _accept(cx, cy, txt, score, "label_sim_ocr")
                    if hit:
                        return hit
    except Exception as e:
        SLog.w(TAG, f"consent similarity resolve failed: {e}")

    try:
        if hasattr(engine, "_ensure_u2"):
            d = engine._ensure_u2()
            if d:
                best_u2 = None
                best_score = 0.0
                for text in ("同意", "同意并继续"):
                    for factory in (
                        lambda t=text: d(text=t),
                        lambda t=text: d(textContains=t),
                    ):
                        try:
                            sel = factory()
                            if not sel.exists(timeout=0.3):
                                continue
                            info = sel.info or {}
                            bounds = info.get("bounds") or {}
                            cx = (
                                int(bounds.get("left", 0))
                                + int(bounds.get("right", 0))
                            ) // 2
                            cy = (
                                int(bounds.get("top", 0))
                                + int(bounds.get("bottom", 0))
                            ) // 2
                            node_text = (
                                info.get("text")
                                or info.get("contentDescription")
                                or text
                            ).strip()
                            score = _label_similarity(query, node_text)
                            if score > best_score:
                                best_score = score
                                best_u2 = (cx, cy, node_text, score)
                        except Exception:
                            continue
                if best_u2 and best_score >= 0.82:
                    cx, cy, txt, score = best_u2
                    hit = _accept(cx, cy, txt, score, "label_sim_u2")
                    if hit:
                        return hit
    except Exception as e:
        SLog.w(TAG, f"u2 consent agree lookup failed: {e}")

    return None


def _try_u2_click_consent_agree(
    engine,
    screen_w: int,
    screen_h: int,
) -> Tuple[bool, str, int, int, Optional[Dict[str, Any]]]:
    """直接点 u2 节点「同意」，并写入 gesture 审计。"""
    from server.services.shared.run_context.regression_run_context import finish_gesture, record_gesture

    if not hasattr(engine, "_ensure_u2"):
        return False, "u2_unavailable", 0, 0, None
    d = engine._ensure_u2()
    if not d:
        return False, "u2_unavailable", 0, 0, None
    picked = _pick_u2_consent_agree_candidate(d, screen_w, screen_h)
    best_node = picked[0] if picked else None
    best_cx = picked[1] if picked else 0
    best_cy = picked[2] if picked else 0
    best_label = picked[3] if picked else "同意"

    def _audit_click(method: str, click_fn) -> Tuple[bool, str, int, int, Optional[Dict[str, Any]]]:
        from script.sleep import mSleep

        gesture = record_gesture(
            "click",
            f"Tap · 点击「{best_label}」",
            method=method,
            x=best_cx,
            y=best_cy,
            label=best_label,
            source="consent_dismiss",
            phase="consent_agree",
            extra={"action_name": "Tap"},
        )
        ok = False
        msg = ""
        try:
            ret = click_fn()
            if _consent_dismiss_succeeded(engine, tap_ok=ret is not False):
                ok = True
            elif ret is False:
                ok = False
            else:
                # uiautomator2 .click() 成功时常返回 None
                ok = True
            msg = (
                f"Tap「{best_label}」@({best_cx},{best_cy}) [{method}]"
                if ok
                else f"{method} 点击失败"
            )
        except Exception as e:
            msg = str(e)
            SLog.w(TAG, f"consent {method} click failed: {e}")
        finish_gesture(gesture, ok=ok, msg=msg, settle_ms=900)
        gesture["action_name"] = "Tap"
        gesture["target_label"] = best_label
        gesture["screen_size"] = {"w": screen_w, "h": screen_h}
        if best_cx > 0 and best_cy > 0:
            half = 44
            gesture["target_rect"] = {
                "left": max(0, best_cx - half),
                "top": max(0, best_cy - half),
                "width": half * 2,
                "height": half * 2,
                "center": [best_cx, best_cy],
                "label": best_label,
            }
        return ok, method, best_cx, best_cy, gesture

    if best_node is not None:
        return _audit_click("u2_element", best_node.click)
    return False, "u2_element", 0, 0, None


def _consent_dialog_gone(engine) -> bool:
    """隐私 consent 弹窗是否已关闭（系统权限弹窗/登录页视为 consent 已关）。"""
    return _overlay_dismiss_target_cleared(engine, "同意")


def _overlay_dismiss_target_cleared(engine, label: str) -> bool:
    """同意/同意并继续类点击是否可跳过（屏上已无对应阻塞层）。"""
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import is_adb_device_online

        sn = getattr(builtins, "TARGET_DEVICE_SN", None)
        if sn and not is_adb_device_online(str(sn)):
            return False

        state = classify_blocking_screen(engine)
        ocr = (state.get("ocr_text") or "").strip()
        scores = (state.get("layer_scores") or {})
        want = (label or "").strip()

        if is_blocking_login_confirm_screen(screen_state=state, screen_text=ocr):
            if want in ("同意并继续", "同意") or "同意并继续" in want:
                return False
        if is_blocking_consent_screen(screen_state=state, screen_text=ocr):
            return False
        if _screen_is_startup_consent_modal(ocr) or _screen_is_consent_dialog(ocr):
            return False

        if _screen_is_system_permission_dialog(ocr, engine=engine):
            return True
        if "同意并继续" in want or want == "同意并继续":
            if is_blocking_login_confirm_screen(screen_state=state, screen_text=ocr):
                return False
            if engine is not None and _hierarchy_has_app_consent_modal(engine):
                return False
            return False
        if _screen_is_verification_code_page(ocr):
            return want in ("同意", "接受", "我知道了", "继续") and not is_blocking_login_confirm_screen(
                screen_state=state, screen_text=ocr
            )
        if (_screen_is_login_home(ocr) or _screen_is_phone_login_form(ocr)) and float(
            scores.get("consent") or 0
        ) < 5.0:
            if want in ("同意并继续",) or is_blocking_login_confirm_screen(
                screen_state=state, screen_text=ocr
            ):
                return False
            return True

        if not ocr:
            return not bool(state.get("consent"))

        if state.get("consent"):
            return False
        if float(scores.get("consent") or 0) >= 8.0:
            return False
        if _engine_on_home_launcher(engine):
            SLog.w(
                TAG,
                "consent gone check: foreground is launcher — likely tapped 不同意",
            )
            return False
        return not bool(state.get("consent"))
    except Exception:
        return False


def _consent_dismiss_succeeded(engine, *, tap_ok: bool) -> bool:
    """点击「同意」后是否已离开隐私弹层（含紧接的系统权限弹窗）。"""
    if not tap_ok:
        return False
    return _consent_dialog_gone(engine)


def _ocr_find_text_center(
    engine,
    screen_h: int,
    *needles: str,
) -> Optional[Tuple[int, int]]:
    bounds = _ocr_find_text_bounds(engine, *needles)
    if not bounds:
        return None
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2


def _ocr_find_text_bounds(
    engine,
    *needles: str,
) -> Optional[Tuple[int, int, int, int]]:
    try:
        shot = engine.screenshot() if hasattr(engine, "screenshot") else None
        if shot is None:
            return None
        from driver.agent.Crawl.ui_discovery import _ocr_analyze_shot

        for it in _ocr_analyze_shot(shot) or []:
            text = (it.get("text") or "").strip()
            if not text or not any(n in text for n in needles if n):
                continue
            bounds = _ocr_box_bounds(it)
            if bounds:
                return bounds
    except Exception as e:
        SLog.w(TAG, f"ocr find text bounds failed: {e}")
    return None


def _login_checkbox_checked(engine) -> bool:
    d = engine._ensure_u2() if hasattr(engine, "_ensure_u2") else None
    if not d:
        return False
    try:
        for sel in (d(className="android.widget.CheckBox"), d(checkable=True)):
            if not sel.exists(timeout=0.4):
                continue
            count = int(getattr(sel, "count", 1) or 1)
            for idx in range(min(count, 12)):
                node = sel[idx] if count > 1 else sel
                info = node.info or {}
                if info.get("checked"):
                    return True
    except Exception as e:
        SLog.w(TAG, f"login checkbox checked probe failed: {e}")
    return False


SYSTEM_PERMISSION_STRONG_MARKERS = (
    "获取已安装",
    "已安装的应用",
    "访问已安装",
    "应用信息",
    "读取应用列表",
    "读取已安装",
    "发送通知",
    "发送通知？",
    "通知？",
    "permissioncontroller",
    "是否允许",
    "授予",
)
# 桌面图标名与权限文案重叠，单独命中易误判（如 MIUI 桌面的「相册」「相机」）
SYSTEM_PERMISSION_WEAK_MARKERS = (
    "定位信息",
    "位置信息",
    "相机",
    "麦克风",
    "通讯录",
    "相册",
    "存储空间",
    "蓝牙",
    "附近设备",
)
_LAUNCHER_GRID_HINTS = ("应用商店", "手机管家", "游戏中心", "Q 搜索", "系统工具")
SYSTEM_ALLOW_LABELS = (
    "仅在使用中允许",
    "仅在使用时允许",
    "仅在使用期间允许",
    "使用时允许",
    "始终允许",
    "允许",
    "仅本次",
    "仅此一次",
)


def is_planned_overlay_step_label(label: str) -> bool:
    """业务 Plan 自身要点同意/不同意类按钮时，守卫不应抢先点击。"""
    import re

    t = (label or "").strip()
    if not t:
        return False
    if re.search(r"同意并继续", t):
        return True
    if re.search(r"^不同意$|点击.*不同意|点.*不同意", t):
        return True
    try:
        from server.services.copilot_service import (
            _extract_ui_text_core,
            _is_consent_action_label,
        )

        core, _ = _extract_ui_text_core(t)
        if _is_consent_action_label(core) or _is_consent_action_label(t):
            return True
        if core in ("不同意",) or (core and re.match(r"^不同意$", core)):
            return True
        if re.search(r"同意并继续", core or ""):
            return True
    except Exception:
        pass
    if re.search(r"[「『\"']同意[」』\"']", t) and "不同意" not in t:
        return True
    if re.search(r"[「『\"']同意并继续[」』\"']", t):
        return True
    return False


def is_overlay_dismiss_target_label(label: str) -> bool:
    """守卫处置目标（同意 / 系统允许）：阻塞屏上仍需定位点击。"""
    if is_planned_overlay_step_label(label):
        return True
    t = (label or "").strip()
    if not t:
        return False
    if t in ("同意", "同意并继续", "接受", "我知道了", "继续", "不同意"):
        return True
    return any(t == a or a in t for a in SYSTEM_ALLOW_LABELS)


def _screen_looks_like_launcher_home(blob: str) -> bool:
    if not blob:
        return False
    hits = sum(1 for h in _LAUNCHER_GRID_HINTS if h in blob)
    if hits >= 2:
        return True
    if "进行授权" in blob and hits >= 1:
        return True
    return False


def _engine_on_home_launcher(engine) -> bool:
    try:
        pkg = ""
        if hasattr(engine, "current_package"):
            pkg = (engine.current_package() or "").lower()
        elif hasattr(engine, "_ensure_u2"):
            d = engine._ensure_u2()
            if d:
                info = d.app_current() or {}
                pkg = (info.get("package") or "").lower()
        if not pkg:
            return False
        return any(
            k in pkg
            for k in (
                "launcher",
                "miui.home",
                "nexuslauncher",
                "trebuchet",
            )
        )
    except Exception:
        return False
    return False


def _u2_on_system_permission_screen(engine) -> bool:
    d = engine._ensure_u2() if hasattr(engine, "_ensure_u2") else None
    if not d:
        return False
    try:
        info = d.app_current() or {}
        pkg = (info.get("package") or "").lower()
        if any(
            k in pkg
            for k in (
                "permissioncontroller",
                "packageinstaller",
                "securitycenter",
                "lbe.security",
            )
        ):
            return True
    except Exception:
        pass
    return False


def _screen_is_app_consent_or_privacy_overlay(blob: str) -> bool:
    """应用内隐私/协议弹层（非 Android 系统权限框）。"""
    text = blob or ""
    if _screen_is_startup_consent_modal(text) or _screen_is_consent_dialog(text):
        return True
    if any(m in text for m in _CONSENT_DISAGREE_LABELS) and any(
        m in text for m in _CONSENT_AGREE_LABELS
    ):
        return True
    if "造物者" in text and any(m in text for m in _GENERIC_LEGAL_MARKERS):
        return True
    return False


def _screen_is_system_permission_dialog(ocr: str, *, engine=None) -> bool:
    blob = ocr or ""
    if _screen_looks_like_launcher_home(blob):
        return False
    if engine is not None and _hierarchy_has_app_consent_modal(engine):
        return False
    if _screen_is_app_consent_or_privacy_overlay(blob):
        return False
    if engine is not None and _u2_on_system_permission_screen(engine):
        return True
    if any(m in blob for m in SYSTEM_PERMISSION_STRONG_MARKERS):
        if "授予" in blob and (
            _screen_is_app_consent_or_privacy_overlay(blob)
            or any(m in blob for m in ("隐私", "协议", "条款", "造物者"))
        ):
            pass
        else:
            return True
    if any(m in blob for m in SYSTEM_PERMISSION_WEAK_MARKERS):
        if any(ctx in blob for ctx in ("是否允许", "发送通知", "permission")):
            return True
    if "是否允许" in blob and any(m in blob for m in ("允许", "拒绝")):
        return True
    if "拒绝" in blob and any(m in blob for m in SYSTEM_ALLOW_LABELS):
        return True
    if re.search(r"允许\s*[「\"']?.+[」\"']?\s*发送", blob):
        return True
    return False


def _hierarchy_find_system_allow_button(
    engine,
    screen_h: int,
) -> Optional[Tuple[int, int, str]]:
    try:
        xml_data = ""
        if hasattr(engine, "dump_hierarchy_xml"):
            xml_data = engine.dump_hierarchy_xml() or ""
        if not xml_data or "<?xml" not in xml_data:
            engine.shell("uiautomator dump /sdcard/view.xml")
            xml_data = engine.shell("cat /sdcard/view.xml")
        if not xml_data or "<?xml" not in xml_data:
            return None
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml_data)
        candidates: List[Tuple[int, int, str]] = []
        for node in root.iter("node"):
            text = (node.get("text") or node.get("content-desc") or "").strip()
            if not text or "拒绝" in text:
                continue
            if not any(label in text for label in SYSTEM_ALLOW_LABELS):
                continue
            nums = re.findall(r"\d+", node.get("bounds") or "")
            if len(nums) != 4:
                continue
            x1, y1, x2, y2 = map(int, nums)
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            if cy < int(screen_h * 0.2):
                continue
            candidates.append((cx, cy, text))
        if not candidates:
            return None
        for prefer in SYSTEM_ALLOW_LABELS:
            matched = [c for c in candidates if prefer in c[2]]
            if matched:
                return max(matched, key=lambda c: (c[1], c[0]))
        return max(candidates, key=lambda c: (c[1], c[0]))
    except Exception as e:
        SLog.w(TAG, f"hierarchy system allow scan failed: {e}")
        return None


def _ocr_find_system_allow_button(engine) -> Optional[Tuple[int, int, str]]:
    try:
        shot = engine.screenshot() if hasattr(engine, "screenshot") else None
        if shot is None:
            return None
        from driver.agent.Crawl.ui_discovery import _ocr_analyze_shot

        candidates: List[Tuple[int, int, str]] = []
        for it in _ocr_analyze_shot(shot) or []:
            text = (it.get("text") or "").strip()
            if not text or "拒绝" in text:
                continue
            if not any(label in text for label in SYSTEM_ALLOW_LABELS):
                continue
            center = _ocr_box_center(it)
            if center:
                candidates.append((center[0], center[1], text))
        if not candidates:
            return None
        for prefer in SYSTEM_ALLOW_LABELS:
            matched = [c for c in candidates if prefer in c[2]]
            if matched:
                return max(matched, key=lambda c: (c[1], c[0]))
        return max(candidates, key=lambda c: (c[1], c[0]))
    except Exception as e:
        SLog.w(TAG, f"ocr system allow scan failed: {e}")
        return None


def _tap_system_permission_allow(
    engine,
    screen_w: int,
    screen_h: int,
    cx: int,
    cy: int,
    label: str,
    method: str,
) -> Dict[str, Any]:
    from server.services.shared.run_context.regression_run_context import finish_gesture, record_gesture

    from script.sleep import mSleep

    gesture = record_gesture(
        "click",
        f"Tap · 系统权限「{label}」",
        method=method,
        x=cx,
        y=cy,
        label=label,
        source="system_permission",
        phase="permission_dismiss",
        extra={"action_name": "Tap"},
    )
    clicked = False
    try:
        if hasattr(engine, "click"):
            clicked = bool(
                engine.click(
                    None,
                    position=(cx, cy),
                    label=label,
                    skip_label_lookup=True,
                )
            )
        if not clicked and hasattr(engine, "_ensure_u2"):
            d = engine._ensure_u2()
            if d:
                d.click(cx, cy)
                clicked = True
    except Exception as e:
        SLog.w(TAG, f"system permission tap failed: {e}")
    mSleep(0.8)
    finish_gesture(
        gesture,
        ok=clicked,
        msg=f"允许权限 @({cx},{cy}) [{method}]" if clicked else f"{method} 点击失败",
    )
    half = 44
    gesture["screen_size"] = {"w": screen_w, "h": screen_h}
    gesture["target_rect"] = {
        "left": max(0, cx - half),
        "top": max(0, cy - half),
        "width": half * 2,
        "height": half * 2,
        "center": [cx, cy],
        "label": label,
    }
    gesture["action_name"] = "Tap"
    return gesture


def _system_permission_dialog_gone(engine) -> bool:
    try:
        ocr = _collect_ocr_text_only(engine)
        return not _screen_is_system_permission_dialog(ocr, engine=engine)
    except Exception:
        return False


def tap_system_permission_on_engine(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    phase: str = "overlay_guard",
    source: str = "overlay_guard",
) -> Dict[str, Any]:
    """系统权限弹窗：hierarchy → OCR → 多通道，单次 Tap（守卫用）。"""
    from script.sleep import mSleep

    locate_debug: Optional[Dict[str, Any]] = None
    gestures: List[Dict[str, Any]] = []

    def _try_tap(cx: int, cy: int, label: str, method: str) -> bool:
        nonlocal locate_debug
        gesture = _tap_system_permission_allow(
            engine, screen_w, screen_h, cx, cy, label, method
        )
        gestures.append(gesture)
        mSleep(0.5)
        return bool(gesture.get("ok")) and _system_permission_dialog_gone(engine)

    hier = _hierarchy_find_system_allow_button(engine, screen_h)
    if hier:
        cx, cy, label = hier
        if _try_tap(cx, cy, label, "hierarchy_permission"):
            g = gestures[-1]
            return {
                "ok": True,
                "method": "hierarchy_permission",
                "x": cx,
                "y": cy,
                "msg": f"Tap「{label}」@({cx},{cy}) [hierarchy_permission]",
                "gesture": g,
                "gestures": gestures,
                "target_rect": g.get("target_rect"),
                "locate_debug": locate_debug,
            }

    ocr_hit = _ocr_find_system_allow_button(engine)
    if ocr_hit:
        cx, cy, label = ocr_hit
        if _try_tap(cx, cy, label, "ocr_permission"):
            g = gestures[-1]
            return {
                "ok": True,
                "method": "ocr_permission",
                "x": cx,
                "y": cy,
                "msg": f"Tap「{label}」@({cx},{cy}) [ocr_permission]",
                "gesture": g,
                "gestures": gestures,
                "target_rect": g.get("target_rect"),
                "locate_debug": locate_debug,
            }

    for perm_label in ("仅在使用中允许", "始终允许", "允许"):
        try:
            from server.services.local.locate.resolver import resolve_locate_target

            loc = resolve_locate_target(
                engine,
                screen_w,
                screen_h,
                label=perm_label,
                icon_targets=icon_targets,
            )
            locate_debug = loc.debug
            if loc.position:
                cx, cy = int(loc.position[0]), int(loc.position[1])
                if _try_tap(cx, cy, perm_label, loc.method or "multichannel"):
                    g = gestures[-1]
                    return {
                        "ok": True,
                        "method": loc.method or "multichannel",
                        "x": cx,
                        "y": cy,
                        "msg": loc.detail or f"Tap「{perm_label}」",
                        "gesture": g,
                        "gestures": gestures,
                        "target_rect": loc.target_rect,
                        "locate_debug": locate_debug,
                    }
        except Exception as e:
            SLog.w(TAG, f"permission multichannel {perm_label!r} failed: {e}")

    last_g = gestures[-1] if gestures else None
    return {
        "ok": False,
        "method": "permission_allow",
        "msg": "系统权限弹窗未命中允许按钮",
        "gesture": last_g,
        "gestures": gestures,
        "locate_debug": locate_debug,
    }


def dismiss_system_permission_dialog(
    engine,
    screen_w: int,
    screen_h: int,
) -> Optional[Dict[str, Any]]:
    """关闭 Android 系统权限弹窗，避免挡住应用内操作。"""
    ocr = _collect_ocr_text_only(engine)
    if not _screen_is_system_permission_dialog(ocr, engine=engine):
        return None
    SLog.i(
        TAG,
        f"system permission dialog detected ocr_snip={(ocr or '')[:160]!r}",
    )

    hit = tap_system_permission_on_engine(
        engine,
        screen_w,
        screen_h,
        phase="permission_dismiss",
        source="system_permission",
    )
    if hit.get("ok"):
        SLog.i(TAG, f"system permission dismissed method={hit.get('method')}")
        return hit

    SLog.w(TAG, "system permission dialog visible but allow tap failed")
    return {"ok": False, "method": "permission_allow", "msg": hit.get("msg") or "系统权限弹窗未命中允许按钮"}


def dismiss_blocking_on_engine(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    max_rounds: int = 5,
) -> List[Dict[str, Any]]:
    """在当前 engine 上连续关闭 consent / 协议页 / 系统权限弹窗。"""
    from script.sleep import mSleep

    gestures: List[Dict[str, Any]] = []
    for round_i in range(max_rounds):
        state = classify_blocking_screen(engine)
        ocr = state.get("ocr_text") or _collect_ocr_text_only(engine)
        consent_visible = bool(state.get("consent") or _screen_is_consent_dialog(ocr))
        if consent_visible:
            ok_u2, _method, _cx, _cy, g_u2 = _try_u2_click_consent_agree(
                engine, screen_w, screen_h
            )
            if g_u2:
                gestures.append(g_u2)
            if not ok_u2:
                tap = resolve_consent_agree_tap(engine, screen_w, screen_h)
                if tap:
                    cx, cy, method = tap
                    try:
                        if hasattr(engine, "click"):
                            engine.click(
                                None,
                                position=(cx, cy),
                                label="同意",
                                skip_label_lookup=True,
                                exact_label=True,
                            )
                    except Exception as e:
                        SLog.w(TAG, f"consent coordinate tap failed: {e}")
            mSleep(0.9 if round_i == 0 else 0.6)
            continue
        if state.get("agreement") or _screen_is_user_agreement_page(ocr):
            try:
                if hasattr(engine, "back"):
                    engine.back()
                elif hasattr(engine, "shell"):
                    engine.shell("input keyevent 4")
            except Exception as e:
                SLog.w(TAG, f"agreement page back failed: {e}")
            mSleep(0.6)
            continue
        if _screen_is_system_permission_dialog(ocr, engine=engine):
            r = dismiss_system_permission_dialog(engine, screen_w, screen_h)
            if not r:
                break
            g = r.get("gesture")
            if g:
                gestures.append(g)
            mSleep(0.7 if round_i == 0 else 0.5)
            if not r.get("ok"):
                break
            continue
        break
    return gestures


def clear_blocking_overlays(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    max_rounds: int = 5,
) -> List[Dict[str, Any]]:
    """连续关闭 consent / 系统权限等阻塞弹窗，直到界面可继续操作。"""
    return dismiss_blocking_on_engine(
        engine, screen_w, screen_h, max_rounds=max_rounds
    )


def ensure_system_permissions_cleared(
    sn: str,
    platform: str = "android",
    *,
    run_id: str = "",
    max_rounds: int = 5,
) -> Dict[str, Any]:
    """循环处理系统权限弹窗（consent 后常出现）。"""
    import builtins
    from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

    from script.sleep import mSleep

    builtins.TARGET_DEVICE_SN = sn
    gestures: List[Dict[str, Any]] = []
    results: List[Dict[str, Any]] = []
    for round_i in range(max_rounds):
        engine, (screen_w, screen_h) = bootstrap_mobile_engine(sn, platform)
        ocr = _collect_ocr_text_only(engine)
        if not _screen_is_system_permission_dialog(ocr, engine=engine):
            break
        r = dismiss_system_permission_dialog(engine, screen_w, screen_h)
        if not r:
            break
        results.append(r)
        g = r.get("gesture")
        if g:
            gestures.append(g)
        mSleep(1.0 if round_i == 0 else 0.6)
        if not r.get("ok"):
            break
    ok = bool(results) and all(x.get("ok") for x in results)
    if results:
        SLog.i(TAG, f"system permissions cleared rounds={len(results)} ok={ok}")
    return {
        "attempted": bool(results),
        "ok": ok if results else True,
        "results": results,
        "gestures": gestures,
        "reason": "关闭系统权限弹窗",
    }


def resolve_login_checkbox_tap(
    engine,
    screen_w: int,
    screen_h: int,
) -> Optional[Tuple[int, int, str]]:
    """登录页协议勾选（委托通用 toggle 定位）。"""
    from server.services.local.locate.toggle_locate_service import resolve_toggle_tap

    for label in (
        "用户协议勾选框",
        "勾选用户协议",
        "已仔细阅读并同意用户协议",
        "用户协议",
    ):
        hit = resolve_toggle_tap(engine, screen_w, screen_h, label)
        if hit:
            return hit
    return _login_checkbox_target(engine, screen_w, screen_h)


def _login_checkbox_target(
    engine,
    screen_w: int,
    screen_h: int,
) -> Optional[Tuple[int, int, str]]:
    """定位登录页底部左侧小复选框（非「一键登录」大按钮）。"""
    y_lo = int(screen_h * 0.76)
    agree_bounds = _ocr_find_text_bounds(
        engine, "已仔细阅读", "已阅读", "阅读并同意", "用户协议"
    )
    agree_left = agree_bounds[0] if agree_bounds else None

    d = engine._ensure_u2() if hasattr(engine, "_ensure_u2") else None
    if d:
        try:
            best: Optional[Tuple[int, int, str]] = None
            for sel in (d(className="android.widget.CheckBox"), d(checkable=True)):
                if not sel.exists(timeout=0.5):
                    continue
                count = int(getattr(sel, "count", 1) or 1)
                for idx in range(min(count, 12)):
                    node = sel[idx] if count > 1 else sel
                    info = node.info or {}
                    if info.get("checked"):
                        return None
                    bounds = info.get("bounds") or {}
                    x1, y1 = int(bounds.get("left", 0)), int(bounds.get("top", 0))
                    x2, y2 = int(bounds.get("right", 0)), int(bounds.get("bottom", 0))
                    w, h = max(1, x2 - x1), max(1, y2 - y1)
                    cy = (y1 + y2) // 2
                    if cy < y_lo:
                        continue
                    if agree_left is not None and x1 > agree_left:
                        continue
                    if w * h > int(screen_w * screen_h * 0.02):
                        continue
                    tap_x = x1 + min(28, max(16, w // 4))
                    tap_y = cy
                    if best is None or tap_x < best[0]:
                        best = (tap_x, tap_y, "u2_checkbox")
            if best:
                return best
        except Exception as e:
            SLog.w(TAG, f"login checkbox u2 scan failed: {e}")

    if agree_bounds:
        x1, y1, x2, y2 = agree_bounds
        cy = (y1 + y2) // 2
        if cy >= y_lo:
            cx = max(40, x1 - 40)
            return cx, cy, "ocr_checkbox_left"
    return None


def _gesture_rect_for_tap(x: int, y: int, label: str, *, half: int = 44) -> Dict[str, Any]:
    half = 22 if "checkbox" in (label or "") or "勾选" in (label or "") else half
    return {
      "left": max(0, int(x) - half),
      "top": max(0, int(y) - half),
      "width": half * 2,
      "height": half * 2,
      "center": [int(x), int(y)],
      "label": label,
  }


def prepare_login_page(
    engine,
    screen_w: int,
    screen_h: int,
) -> Optional[Dict[str, Any]]:
    """已废弃：协议勾选由用例步骤显式执行，不再在登录点击前自动勾选。"""
    return None


def _consent_multichannel_rejects_winner(loc) -> bool:
    """多通道命中「不同意」或无效坐标时退回几何定位。"""
    if not loc or not getattr(loc, "position", None):
        return True
    cx, cy = int(loc.position[0]), int(loc.position[1])
    if cx <= 0 or cy <= 0:
        return True
    rect = getattr(loc, "target_rect", None) or {}
    lbl = (rect.get("label") or "").strip()
    if "不同意" in lbl:
        return True
    detail = (getattr(loc, "detail", None) or "").strip()
    if "不同意" in detail:
        return True
    return False


def tap_consent_agree_on_engine(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    phase: str = "consent_dismiss",
    source: str = "consent_agree",
    exact_label: bool = True,
    single_tap: bool = False,
    agree_label: str = "同意",
) -> Dict[str, Any]:
    """在当前 engine 上安全点击 consent「同意」/「同意并继续」：多通道仲裁优先，几何/OCR 兜底。"""
    agree_label = (agree_label or "同意").strip() or "同意"
    if phase == "overlay_guard":
        single_tap = True
    gestures: List[Dict[str, Any]] = []
    clicked = False
    method = "none"
    cx, cy = 0, 0
    locate_debug: Optional[Dict[str, Any]] = None
    geometry_tap: Optional[Tuple[int, int, str]] = None

    def _tap_consent_at(
        tap: Tuple[int, int, str],
        *,
        debug: Optional[Dict[str, Any]] = None,
        rect_override: Optional[Dict[str, Any]] = None,
    ) -> bool:
        nonlocal cx, cy, method, clicked
        cx, cy, method = tap
        from server.services.shared.run_context.regression_run_context import finish_gesture, record_gesture

        gesture = record_gesture(
            "click",
            f"Tap · {agree_label}",
            method=method or "geometry_consent",
            x=cx,
            y=cy,
            label=agree_label,
            source=source,
            phase=phase,
            extra={"action_name": "Tap"},
        )
        if debug:
            gesture["locate_debug"] = debug
        coord_ok = False
        try:
            if hasattr(engine, "click"):
                coord_ok = bool(
                    engine.click(
                        None,
                        position=(cx, cy),
                        label=agree_label,
                        skip_label_lookup=True,
                        exact_label=exact_label,
                        consent_dismiss=True,
                    )
                )
        except Exception as e:
            SLog.w(TAG, f"consent geometry tap failed: {e}")
        half = 44
        gesture["screen_size"] = {"w": screen_w, "h": screen_h}
        gesture["target_rect"] = rect_override or {
            "left": max(0, cx - half),
            "top": max(0, cy - half),
            "width": half * 2,
            "height": half * 2,
            "center": [cx, cy],
            "label": agree_label,
        }
        effective = _overlay_dismiss_target_cleared(engine, agree_label) if coord_ok else False
        tap_ok = effective if not single_tap else coord_ok
        finish_gesture(
            gesture,
            ok=tap_ok,
            msg=f"Tap「{agree_label}」@({cx},{cy}) [{method}]",
            settle_ms=500 if single_tap else (700 if phase == "overlay_guard" else 900),
        )
        gestures.append(gesture)
        return tap_ok

    labels_to_try = [agree_label]
    if agree_label == "同意并继续":
        labels_to_try.append("同意")

    try:
        from server.services.local.locate.resolver import resolve_locate_target

        for try_label in labels_to_try:
            loc = resolve_locate_target(
                engine,
                screen_w,
                screen_h,
                label=try_label,
                icon_targets=icon_targets,
            )
            locate_debug = loc.debug
            if loc.position and not _consent_multichannel_rejects_winner(loc):
                mc_method = loc.method or "multichannel"
                mc_rect = loc.target_rect
                clicked = _tap_consent_at(
                    (int(loc.position[0]), int(loc.position[1]), mc_method),
                    debug=locate_debug,
                    rect_override=mc_rect,
                )
                if clicked:
                    break
    except Exception as e:
        SLog.w(TAG, f"consent multichannel probe failed: {e}")

    geometry_tap: Optional[Tuple[int, int, str]] = None
    if not clicked:
        geometry_tap = resolve_consent_agree_tap(engine, screen_w, screen_h)
        if geometry_tap:
            clicked = _tap_consent_at(geometry_tap, debug=locate_debug)

    if not clicked and not single_tap:
        clip_hit = _clip_tap_label(
            engine,
            screen_w,
            screen_h,
            agree_label,
            icon_targets=icon_targets,
            phase=phase,
            source=source,
            exact_label=exact_label,
        )
        locate_debug = clip_hit.get("locate_debug")
        if clip_hit.get("gesture"):
            gestures.append(clip_hit["gesture"])
        if clip_hit.get("ok"):
            method = clip_hit.get("method") or "clip"
            cx, cy = int(clip_hit.get("x") or 0), int(clip_hit.get("y") or 0)
            if _consent_dismiss_succeeded(engine, tap_ok=True):
                clicked = True
            else:
                SLog.w(TAG, "consent dialog still up after clip fallback")

    if not clicked and _overlay_dismiss_target_cleared(engine, agree_label):
        SLog.i(TAG, f"overlay dismiss target {agree_label!r} already cleared")
        clicked = True
        if not method or method == "none":
            method = "screen_advanced"

    if clicked and (cx <= 0 or cy <= 0) and method != "screen_advanced":
        SLog.w(TAG, "consent agree invalid tap coords; treat as failure")
        clicked = False

    last_rect = gestures[-1].get("target_rect") if gestures else None
    last_gesture = gestures[-1] if gestures else None
    return {
        "ok": clicked,
        "method": method,
        "x": cx,
        "y": cy,
        "msg": (
            f"Tap「{agree_label}」@({cx},{cy}) [{method}]"
            if clicked
            else (
                f"无法安全定位「{agree_label}」按钮（请检查弹窗是否可见）"
                if not geometry_tap
                else f"「{agree_label}」未生效 @({cx},{cy}) [{method}]"
            )
        ),
        "gesture": last_gesture,
        "gestures": gestures,
        "target_rect": last_rect,
        "locate_debug": locate_debug,
    }


def run_consent_agree_tap(
    sn: str,
    platform: str = "android",
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """安全点击 consent「同意」：几何/OCR 优先，CLIP 兜底。"""
    gestures: List[Dict[str, Any]] = []
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

        builtins.TARGET_DEVICE_SN = sn
        engine, (screen_w, screen_h) = bootstrap_mobile_engine(sn, platform)
        state = classify_blocking_screen(engine)
        ocr = state.get("ocr_text") or _collect_ocr_text_only(engine)
        consent_blocking = is_blocking_consent_screen(screen_state=state, screen_text=ocr)
        if not consent_blocking and (
            _screen_is_phone_login_form(ocr) or _screen_is_verification_code_page(ocr)
        ):
            return {
                "ok": False,
                "msg": "当前为登录/验证码页，跳过 consent 点击",
                "method": "skip_phone_login",
                "gestures": gestures,
            }

        hit = tap_consent_agree_on_engine(
            engine,
            screen_w,
            screen_h,
            icon_targets=icon_targets,
            phase="consent_dismiss",
            source="geometry_consent",
        )
        gestures = list(hit.get("gestures") or [])
        clicked = bool(hit.get("ok"))
        method = hit.get("method") or "none"
        cx, cy = int(hit.get("x") or 0), int(hit.get("y") or 0)

        if clicked:
            try:
                perm = ensure_system_permissions_cleared(sn, platform, run_id="")
                for g in perm.get("gestures") or []:
                    if g not in gestures:
                        gestures.append(g)
            except Exception as e:
                SLog.w(TAG, f"consent follow-up permission clear failed: {e}")

        SLog.i(TAG, f"consent agree tap ({cx},{cy}) method={method} ok={clicked}")
        payload = {
            "method": method,
            "x": cx,
            "y": cy,
            "gestures": gestures,
            "target_label": "同意",
            "action_name": "Tap",
            "screen_size": {"w": screen_w, "h": screen_h},
            "target_rect": hit.get("target_rect"),
        }
        if not clicked:
            return {**payload, "ok": False, "msg": hit.get("msg") or "consent 点击失败"}
        return {**payload, "ok": True, "msg": hit.get("msg") or f"Tap「同意」@({cx},{cy})"}
    except Exception as e:
        SLog.w(TAG, f"consent agree tap failed: {e}")
        return {"ok": False, "msg": str(e), "method": "consent_agree", "gestures": gestures}


def _consent_agree_step(
    screen_w: int,
    screen_h: int,
    engine=None,
) -> Dict[str, Any]:
    return {
        "kind": "click",
        "label": "同意",
        "summary": "Tap · 同意",
        "exact_label": True,
    }


def _overlay_recovery_steps(
    screen_text: str,
    *,
    screen_w: int = 1080,
    screen_h: int = 1920,
    engine=None,
) -> List[Dict[str, Any]]:
    blob = screen_text or ""
    steps: List[Dict[str, Any]] = []
    if _screen_is_consent_dialog(blob):
        steps.append(_consent_agree_step(screen_w, screen_h, engine))
        return steps
    if "不同意" in blob and any(w in blob for w in _CONSENT_AGREE_LABELS):
        steps.append(_consent_agree_step(screen_w, screen_h, engine))
        return steps
    for word in OVERLAY_DISMISS:
        if word in ("同意", "同意并继续"):
            continue
        if word in blob:
            steps.append(
                {
                    "kind": "click",
                    "label": word,
                    "summary": f"关闭弹层：点击「{word}」",
                    "exact_label": True,
                }
            )
            return steps
    if _screen_is_overlay(blob):
        steps.append({"kind": "back", "summary": "关闭协议弹层：返回", "immediate": True})
    return steps


def _recovery_steps_for_screen(
    screen_text: str,
    *,
    screen_w: int = 1080,
    screen_h: int = 1920,
    engine=None,
    screen_state: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    state = screen_state or (classify_blocking_screen(engine) if engine is not None else {})
    blob = state.get("ocr_text") or screen_text or ""
    consent_visible = is_blocking_consent_screen(
        screen_state=state,
        screen_text=blob,
    )
    if _screen_is_system_permission_dialog(blob, engine=engine):
        return [
            {
                "kind": "system_permission",
                "summary": "Tap · 关闭系统权限弹窗",
            }
        ]
    # consent 弹窗：只点「同意」，绝不按返回（返回会关掉弹窗但留在协议 WebView）
    if consent_visible:
        return [_consent_agree_step(screen_w, screen_h, engine)]
    if state.get("agreement") or _screen_is_user_agreement_page(blob):
        return [{"kind": "back", "summary": "从用户协议页返回", "immediate": True}]
    if _screen_is_overlay(blob):
        return _overlay_recovery_steps(blob, screen_w=screen_w, screen_h=screen_h, engine=engine)
    return []


def _execute_recovery_steps(
    steps: List[Dict[str, Any]],
    *,
    sn: str,
    platform: str,
    app_id: str,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    run_id: str = "",
    target_package: str = "",
) -> List[Dict[str, Any]]:
    from server.services.executor.execute_steps import execute_steps

    return execute_steps(
        steps,
        sn=sn,
        platform=platform,
        icon_targets=icon_targets or [],
        run_id=run_id,
        capture_screenshots=bool(run_id),
        app_id=app_id,
        target_package=target_package,
        stop_on_failure=True,
    )


def _tab_recovery_steps(
    current_label: str,
    target_label: str,
    screen_text: str,
) -> List[Dict[str, Any]]:
    cur = (current_label or "").lower()
    tgt = (target_label or "").lower()
    blob = screen_text or ""
    steps: List[Dict[str, Any]] = []

    for tab in SEGMENT_TAB_LABELS:
        if tab.lower() in tgt or tab in (target_label or ""):
            if tab in blob or tab.lower() in cur:
                return []
            steps.append(
                {
                    "kind": "click",
                    "label": tab,
                    "summary": f"顶栏导航：点击「{tab}」",
                    "segment_tab": True,
                }
            )
            return steps

    for tab in BOTTOM_TAB_LABELS:
        if tab.lower() in tgt or tab in (target_label or ""):
            if tab.lower() not in cur and tab not in (current_label or ""):
                if tab in blob or tab in BOTTOM_TAB_LABELS:
                    steps.append(
                        {
                            "kind": "click",
                            "label": tab,
                            "summary": f"底栏导航：点击「{tab}」",
                            "bottom_tab": True,
                        }
                    )
                    return steps
    return steps


def _heuristic_recovery_steps(
    current_page: Dict[str, Any],
    target_page: Dict[str, Any],
    screen_text: str,
) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []
    steps.extend(_overlay_recovery_steps(screen_text))
    steps.extend(
        _tab_recovery_steps(
            current_page.get("label") or "",
            target_page.get("label") or "",
            screen_text,
        )
    )
    target_page_data = target_page.get("page") or {}
    for frame in (target_page_data.get("frames") or [])[:6]:
        for hint in ("按钮", "登录", "一键", "Tab", "导航", "navbar"):
            fname = (frame.get("name") or "")
            if hint in fname:
                for t in frame.get("texts") or []:
                    if len(t) >= 2 and t in (screen_text or ""):
                        steps.append(
                            {
                                "kind": "click",
                                "label": t,
                                "summary": f"设计稿导航：点击「{t}」",
                            }
                        )
                        return steps
    tok = (target_page.get("token") or target_page.get("label") or "").strip()
    if tok and len(tok) >= 2 and tok in (screen_text or ""):
        steps.append(
            {
                "kind": "click",
                "label": tok,
                "summary": f"目标页入口：点击「{tok}」",
            }
        )
    return steps


def plan_recovery_steps(
    *,
    current_page: Dict[str, Any],
    target_page: Dict[str, Any],
    app_graph: Dict[str, Any],
    screen_text: str,
) -> Dict[str, Any]:
    """规划从当前页到目标页的点击/返回步骤。"""
    from_id = current_page.get("node_id") or ""
    to_id = target_page.get("node_id") or ""
    steps: List[Dict[str, Any]] = []

    if _screen_is_overlay(screen_text):
        steps.extend(_overlay_recovery_steps(screen_text, screen_w=1080, screen_h=1920))

    path = find_graph_path(app_graph.get("edges") or [], str(from_id), str(to_id))
    if path:
        for i, edge in enumerate(path):
            steps.append(edge_to_click_step(edge, index=i))
    else:
        steps.extend(_heuristic_recovery_steps(current_page, target_page, screen_text))

    deduped: List[Dict[str, Any]] = []
    seen = set()
    for s in steps:
        key = (s.get("kind"), s.get("label"), s.get("summary"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(s)

    return {
        "steps": deduped,
        "graph_path_len": len(path),
        "from": current_page.get("label"),
        "to": target_page.get("label"),
    }


def _action_already_reached_target(
    expected: str,
    step_results: List[Dict[str, Any]],
    screen_text: str,
) -> bool:
    blob = screen_text or ""
    for r in step_results or []:
        if not r.get("ok"):
            continue
        label = (r.get("target_label") or r.get("summary") or "").strip()
        for tok in extract_page_tokens(expected):
            if tok and tok in label and tok in blob:
                return True
    return False


def _should_attempt_page_recovery(
    expected: str,
    *,
    checks_ok: bool,
    checks: List[Dict[str, Any]],
    page_context: Dict[str, Any],
    screen_text: str,
    app_id: str,
    step_results: Optional[List[Dict[str, Any]]] = None,
) -> bool:
    if not app_id or checks_ok:
        return False
    if _action_already_reached_target(expected, step_results or [], screen_text):
        return False
    from server.services.shared.page_context.page_context_service import expected_matches_page
    from server.services.local.navigation.page_navigation_service import _screen_is_overlay

    if _screen_is_overlay(screen_text):
        return True
    if page_context.get("matched") and expected_matches_page(expected, page_context) is False:
        return True
    for c in checks or []:
        reason = c.get("reason") or ""
        if not c.get("ok") and (
            "页面识别" in reason
            or "未进入" in reason
            or "不在" in reason
            or "页面状态" in reason
        ):
            return True
    return False


def dismiss_startup_overlays(
    *,
    sn: str,
    platform: str,
    app_id: str,
    session,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    run_id: str = "",
    target_package: str = "",
    requires_fresh_startup: bool = False,
) -> Optional[Dict[str, Any]]:
    """应用拉起后关闭隐私 consent / 协议弹层，避免后续误点正文链接。

    requires_fresh_startup: 本用例已清缓存，首次启动必经隐私弹窗+系统权限，强制规划关闭步骤。
    """
    if not sn or not app_id:
        return None
    import builtins
    from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
    from server.services.shared.page_context.page_context_service import (
        _collect_full_screen_text,
        identify_page_for_trace,
    )
    from script.sleep import mSleep

    builtins.TARGET_DEVICE_SN = sn
    engine, (screen_w, screen_h) = bootstrap_mobile_engine(sn, platform)

    state: Dict[str, Any] = {}
    for attempt in range(8):
        if attempt:
            mSleep(1.2)
        state = classify_blocking_screen(engine)
        if state.get("screen_not_ready"):
            SLog.w(
                TAG,
                f"startup dismiss: screen not ready reason={state.get('reason')} "
                f"attempt={attempt}",
            )
            if hasattr(engine, "ensure_screen_ready"):
                try:
                    engine.ensure_screen_ready(node_sn=sn)
                except Exception as e:
                    SLog.w(TAG, f"startup dismiss wake/unlock failed: {e}")
            if attempt < 6:
                continue
            break
        if state.get("consent") or state.get("agreement"):
            break
        if _screen_is_system_permission_dialog(
            state.get("ocr_text") or "", engine=engine
        ):
            break
        # 登录页 OCR 先出现时 consent 弹层可能尚未渲染，多等几轮
        if state.get("reason") in ("login_home", "phone_login_or_sms") and attempt < (
            7 if requires_fresh_startup else 5
        ):
            continue
        if requires_fresh_startup and attempt < 6:
            continue
        if int(state.get("ocr_len") or 0) >= 40 and not requires_fresh_startup:
            break

    screen_text = state.get("ocr_text") or ""
    page_before = identify_page_for_trace(
        app_id,
        engine,
        session=session,
        frame_count=2,
        screen_text=screen_text,
        sn=sn,
        platform=platform,
        run_id=run_id,
        tag="startup_before",
    )

    steps = _recovery_steps_for_screen(
        screen_text,
        screen_w=screen_w,
        screen_h=screen_h,
        engine=engine,
        screen_state=state,
    )

    if not steps and requires_fresh_startup:
        if _screen_is_system_permission_dialog(screen_text, engine=engine):
            steps = [
                {
                    "kind": "system_permission",
                    "summary": "Tap · 关闭系统权限弹窗",
                }
            ]
        else:
            steps = [_consent_agree_step(screen_w, screen_h, engine)]
        SLog.i(
            TAG,
            f"Startup overlay forced after cache clear: reason={state.get('reason')} "
            f"steps={len(steps)}",
        )

    if not steps:
        SLog.d(
            TAG,
            f"Startup overlay skip: reason={state.get('reason')} "
            f"consent={state.get('consent')} agreement={state.get('agreement')} "
            f"ocr_len={state.get('ocr_len')}",
        )
        return None

    action = steps[0].get("summary") or steps[0].get("kind")
    SLog.i(
        TAG,
        f"Startup overlay dismiss steps={len(steps)} action={action} "
        f"reason={state.get('reason')} disagree={state.get('has_disagree_btn')} "
        f"agree_btn={state.get('has_agree_btn')} ocr_snip={(screen_text or '')[:120]!r}",
    )
    nav_results = _execute_recovery_steps(
        steps,
        sn=sn,
        platform=platform,
        app_id=app_id,
        icon_targets=icon_targets,
        run_id=run_id,
        target_package=target_package,
    )
    nav_ok = all(r.get("ok") for r in nav_results) if nav_results else False
    mSleep(1.0)
    engine2, (screen_w2, screen_h2) = bootstrap_mobile_engine(sn, platform)
    after_state = classify_blocking_screen(engine2)
    new_screen = _collect_full_screen_text(engine2)
    page_after = identify_page_for_trace(
        app_id,
        engine2,
        session=session,
        frame_count=2,
        screen_text=new_screen,
        sn=sn,
        platform=platform,
        run_id=run_id,
        tag="startup_after",
    )
    still_blocked = bool(after_state.get("consent") or after_state.get("agreement"))
    if still_blocked and nav_ok:
        retry_steps = _recovery_steps_for_screen(
            new_screen,
            screen_w=screen_w2,
            screen_h=screen_h2,
            engine=engine2,
            screen_state=after_state,
        )
        if retry_steps:
            SLog.w(
                TAG,
                f"Startup overlay still blocked after dismiss "
                f"page={page_after.get('label')!r}, retry steps={len(retry_steps)}",
            )
            retry_results = _execute_recovery_steps(
                retry_steps,
                sn=sn,
                platform=platform,
                app_id=app_id,
                icon_targets=icon_targets,
                run_id=run_id,
                target_package=target_package,
            )
            nav_results.extend(retry_results)
            nav_ok = all(r.get("ok") for r in nav_results) if nav_results else False
            mSleep(0.8)
            engine2, _ = bootstrap_mobile_engine(sn, platform)
            after_state = classify_blocking_screen(engine2)
            new_screen = _collect_full_screen_text(engine2)
            page_after = identify_page_for_trace(
                app_id,
                engine2,
                session=session,
                frame_count=2,
                screen_text=new_screen,
                sn=sn,
                platform=platform,
                run_id=run_id,
                tag="startup_retry_after",
            )
            still_blocked = bool(after_state.get("consent") or after_state.get("agreement"))
    if still_blocked:
        nav_ok = False

    permission_recovery: Dict[str, Any] = {"attempted": False, "ok": True}
    if nav_ok:
        try:
            permission_recovery = ensure_system_permissions_cleared(
                sn, platform, run_id=run_id
            )
            if permission_recovery.get("gestures"):
                perm_step = {
                    "index": len(nav_results),
                    "kind": "click",
                    "summary": "Tap · 允许系统权限",
                    "ok": permission_recovery.get("ok"),
                    "msg": permission_recovery.get("reason") or "系统权限",
                    "gestures": permission_recovery.get("gestures") or [],
                }
                nav_results.append(perm_step)
            if permission_recovery.get("attempted"):
                mSleep(0.8)
                engine2, _ = bootstrap_mobile_engine(sn, platform)
                new_screen = _collect_full_screen_text(engine2)
                page_after = identify_page_for_trace(
                    app_id,
                    engine2,
                    session=session,
                    frame_count=2,
                    screen_text=new_screen,
                    sn=sn,
                    platform=platform,
                    run_id=run_id,
                    tag="startup_perm_after",
                )
        except Exception as e:
            SLog.w(TAG, f"startup permission clear failed: {e}")

    return {
        "attempted": True,
        "ok": nav_ok and permission_recovery.get("ok", True),
        "reason": "应用启动后关闭隐私/协议弹层",
        "plan": {"steps": steps, "from": page_before.get("label"), "to": "继续主流程"},
        "current_page_before": page_before,
        "current_page_after": page_after,
        "screen_text_after": new_screen,
        "nav_results": nav_results,
        "permission_recovery": permission_recovery,
    }


def ensure_page_ready_before_action(
    *,
    sn: str,
    platform: str,
    app_id: str,
    session,
    step_text: str = "",
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    run_id: str = "",
    target_package: str = "",
) -> Optional[Dict[str, Any]]:
    """每步操作前：识别当前页，若在协议/弹层等阻塞页则先导航离开。"""
    if not sn or not app_id:
        return None
    import builtins
    from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
    from server.services.shared.page_context.page_context_service import (
        _collect_full_screen_text,
        identify_page_for_trace,
    )

    builtins.TARGET_DEVICE_SN = sn
    engine, (screen_w, screen_h) = bootstrap_mobile_engine(sn, platform)
    state = classify_blocking_screen(engine)
    screen_text = state.get("ocr_text") or ""

    in_regression = False
    try:
        from server.services.shared.run_context.regression_run_context import get_ctx

        gctx = get_ctx()
        in_regression = bool(gctx and gctx.get("run_id"))
    except Exception:
        pass
    if run_id and not in_regression:
        in_regression = True

    steps = _recovery_steps_for_screen(
        screen_text,
        screen_w=screen_w,
        screen_h=screen_h,
        engine=engine,
        screen_state=state,
    )
    step_l = (step_text or "").lower()
    if not steps and state.get("agreement") and not state.get("consent"):
        if any(k in step_l for k in ("登录", "一键", "首页", "feed")):
            steps = [{"kind": "back", "summary": "从用户协议页返回", "immediate": True}]

    if not steps:
        page_before: Dict[str, Any]
        if in_regression:
            from server.services.shared.page_context.page_context_service import _identify_page_by_screen_keywords

            page_before = _identify_page_by_screen_keywords(screen_text) or {
                "matched": False,
                "label": "",
                "score": 0.0,
                "method": "keyword",
                "source": "keyword",
            }
        else:
            page_before = identify_page_for_trace(
                app_id,
                engine,
                session=session,
                frame_count=1,
                screen_text=screen_text,
                sn=sn,
                platform=platform,
                run_id=run_id,
                tag="pre_action_before",
            )
        return {
            "attempted": False,
            "current_page_before": page_before,
            "screen_text": screen_text[:500],
            "screen_state": state,
        }

    # 回归执行：阻塞弹窗由 Plan 内 Overlay Guard 逐步处置，不在此预执行固定点击序列
    if in_regression:
        from server.services.shared.page_context.page_context_service import _identify_page_by_screen_keywords

        page_before = _identify_page_by_screen_keywords(screen_text) or {
            "matched": False,
            "label": "",
            "score": 0.0,
            "method": "keyword",
            "source": "keyword",
        }
        return {
            "attempted": False,
            "overlay_guard_delegated": True,
            "reason": "阻塞弹窗由 Overlay Guard 按当前屏逐步处置",
            "current_page_before": page_before,
            "screen_text": screen_text[:500],
            "screen_state": state,
            "planned_recovery_steps": steps,
        }

    page_before = identify_page_for_trace(
        app_id,
        engine,
        session=session,
        frame_count=1,
        screen_text=screen_text,
        sn=sn,
        platform=platform,
        run_id=run_id,
        tag="pre_action_before",
    )

    action = steps[0].get("summary") or steps[0].get("kind")
    SLog.i(
        TAG,
        f"Pre-action page ready: page={page_before.get('label')} steps={len(steps)} "
        f"action={action} reason={state.get('reason')} step={step_text[:30]!r}",
    )
    nav_results = _execute_recovery_steps(
        steps,
        sn=sn,
        platform=platform,
        app_id=app_id,
        icon_targets=icon_targets,
        run_id=run_id,
        target_package=target_package,
    )
    nav_ok = all(r.get("ok") for r in nav_results) if nav_results else False
    engine2, _ = bootstrap_mobile_engine(sn, platform)
    new_screen = _collect_full_screen_text(engine2)
    page_after = identify_page_for_trace(
        app_id,
        engine2,
        session=session,
        frame_count=2,
        screen_text=new_screen,
        sn=sn,
        platform=platform,
        run_id=run_id,
        tag="pre_action_after",
    )
    return {
        "attempted": True,
        "ok": nav_ok,
        "reason": "操作前页面路径修正",
        "plan": {"steps": steps, "from": page_before.get("label"), "to": page_after.get("label")},
        "current_page_before": page_before,
        "current_page_after": page_after,
        "screen_text_after": new_screen,
        "nav_results": nav_results,
    }


def try_dismiss_blocking_overlay(
    *,
    sn: str,
    platform: str,
    app_id: str,
    session,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    run_id: str = "",
    target_package: str = "",
) -> Optional[Dict[str, Any]]:
    """操作失败时尝试关闭协议/弹层，便于重试点击。"""
    if not app_id or not sn:
        return None
    if run_id:
        return None
    import builtins
    from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
    from server.services.shared.page_context.page_context_service import (
        _collect_full_screen_text,
        identify_page_for_trace,
    )

    builtins.TARGET_DEVICE_SN = sn
    engine, (screen_w, screen_h) = bootstrap_mobile_engine(sn, platform)
    state = classify_blocking_screen(engine)
    screen_text = state.get("ocr_text") or ""
    if not (state.get("consent") or state.get("agreement") or _screen_is_overlay(screen_text)):
        return None

    page_context = identify_page_for_trace(
        app_id,
        engine,
        session=session,
        frame_count=2,
        screen_text=screen_text,
        sn=sn,
        platform=platform,
        run_id=run_id,
        tag="overlay_before",
    )
    steps = _recovery_steps_for_screen(
        screen_text,
        screen_w=screen_w,
        screen_h=screen_h,
        engine=engine,
        screen_state=state,
    )
    if not steps:
        return None

    SLog.i(TAG, f"Pre-action overlay dismiss steps={len(steps)}")
    nav_results = _execute_recovery_steps(
        steps,
        sn=sn,
        platform=platform,
        app_id=app_id,
        icon_targets=icon_targets,
        run_id=run_id,
        target_package=target_package,
    )
    nav_ok = all(r.get("ok") for r in nav_results) if nav_results else False
    engine2, _ = bootstrap_mobile_engine(sn, platform)
    new_screen = _collect_full_screen_text(engine2)
    new_page = identify_page_for_trace(
        app_id,
        engine2,
        session=session,
        frame_count=2,
        screen_text=new_screen,
        sn=sn,
        platform=platform,
        run_id=run_id,
        tag="overlay_after",
    )
    return {
        "attempted": True,
        "ok": nav_ok,
        "reason": "检测到协议/弹层，已尝试关闭",
        "plan": {"steps": steps, "from": page_context.get("label"), "to": "关闭弹层"},
        "current_page_before": page_context,
        "current_page_after": new_page,
        "screen_text_after": new_screen,
        "nav_results": nav_results,
    }


def try_recover_and_reverify(
    expected: str,
    *,
    sn: str,
    platform: str,
    app_id: str,
    session,
    page_context: Dict[str, Any],
    screen_text: str,
    step_results: List[Dict[str, Any]],
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    run_id: str = "",
) -> Optional[Dict[str, Any]]:
    """
    当前页与预期不符时，尝试导航到目标页并重新识别。
    返回 recovery 结果；无法规划/执行时返回 None。
    """
    if not app_id or not sn:
        return None
    if expected_matches_page(expected, page_context) is True:
        return None

    overlay = _screen_is_overlay(screen_text)
    if expected_matches_page(expected, page_context) is not False and not overlay:
        return None

    from server.models.project import App
    from server.services.figma_logic_service import load_figma_logic_for_app

    app = session.query(App).filter(App.id == str(app_id)).first() if session else None
    figma_logic = load_figma_logic_for_app(app) if app else None
    app_graph = load_app_graph_by_app_id(session, app_id) if session else {"nodes": [], "edges": []}

    target = resolve_target_page_from_expected(expected, app_graph, figma_logic)
    if not target:
        if overlay:
            target = {
                "node_id": None,
                "label": "关闭协议弹层",
                "source": "overlay",
            }
        else:
            SLog.d(TAG, f"no target page for expected={expected[:40]}")
            return None

    plan = plan_recovery_steps(
        current_page=page_context,
        target_page=target,
        app_graph=app_graph,
        screen_text=screen_text,
    )
    steps = plan.get("steps") or []
    if not steps:
        return {
            "attempted": False,
            "reason": "无法规划到目标页的路径",
            "target_page": target,
            "current_page": page_context,
        }

    from server.services import copilot_service as cs

    SLog.i(
        TAG,
        f"Recover navigate {page_context.get('label')} -> {target.get('label')} "
        f"steps={len(steps)}",
    )
    nav_results = _execute_recovery_steps(
        steps,
        sn=sn,
        platform=platform,
        app_id=app_id,
        icon_targets=icon_targets,
        run_id=run_id,
    )
    nav_ok = all(r.get("ok") for r in nav_results) if nav_results else False

    import builtins
    from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
    from server.services.shared.page_context.page_context_service import _collect_screen_text_from_engine

    builtins.TARGET_DEVICE_SN = sn
    engine, _ = bootstrap_mobile_engine(sn, platform)
    new_screen = _collect_screen_text_from_engine(engine)
    new_page = identify_for_app(app_id, engine, session=session, frame_count=2)

    return {
        "attempted": True,
        "ok": nav_ok,
        "plan": plan,
        "target_page": target,
        "current_page_before": page_context,
        "current_page_after": new_page,
        "screen_text_after": new_screen,
        "nav_results": nav_results,
    }
