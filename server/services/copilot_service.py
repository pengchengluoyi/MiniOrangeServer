# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
对话流：自然语言 → 可执行步骤 → Manager/引擎执行（类似 Midscene 的规划+执行循环）。
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from script.log import SLog

TAG = "CopilotService"

_LAST_LOCATE_DEBUG: Optional[Dict[str, Any]] = None


def pop_locate_debug() -> Optional[Dict[str, Any]]:
    global _LAST_LOCATE_DEBUG
    dbg = _LAST_LOCATE_DEBUG
    _LAST_LOCATE_DEBUG = None
    return dbg


def clear_locate_debug() -> None:
    global _LAST_LOCATE_DEBUG
    _LAST_LOCATE_DEBUG = None


def _make_toggle_locate_debug(
    cx: int,
    cy: int,
    method: str,
    label: str,
    *,
    w: int = 44,
    h: int = 44,
) -> Dict[str, Any]:
    """协议勾选等开关快路径的定位诊断（不经多通道仲裁）。"""
    row = {
        "channel": "toggle",
        "method": method,
        "label": label,
        "raw_score": 1.0,
        "final_score": 1.0,
        "cx": cx,
        "cy": cy,
        "w": w,
        "h": h,
        "selected": True,
        "detail": f"{method}@({cx},{cy})",
    }
    return {
        "query": label,
        "profile": "login",
        "target_kind": "toggle",
        "spatial_zones": [],
        "candidates": [row],
        "overlay": [row],
        "winner_channel": "toggle",
    }


def _with_locate_debug(payload: Dict[str, Any]) -> Dict[str, Any]:
    dbg = pop_locate_debug()
    if dbg:
        payload = {**payload, "locate_debug": dbg}
    return payload

# 规划阶段无坐标时的占位；执行时会按文案/层级/OCR 重新定位
_DEFAULT_CLICK_XY = (0, 0)

# 登录页底部图标行默认顺序（左→右，仅作兜底）；未安装微信等导致图标缺失时不能盲用槽位
_DEFAULT_LOGIN_ICON_ORDER = {
    "wechat": 0,
    "phone_sms": 1,
    "email_password": 2,
    "apple": 3,
}
_LOGIN_INTENT_RESOURCE_HINTS = {
    "wechat": ("wechat", "weixin", "wx", "微信"),
    "phone_sms": ("phone", "mobile", "sms", "verify", "手机", "验证码", "短信"),
    "email_password": ("email", "password", "account", "mail", "邮箱", "密码", "账号"),
    "apple": ("apple", "苹果"),
}
_LOGIN_METHOD_U2_HINTS = {
    "wechat": ("微信", "WeChat", "wechat"),
    "phone_sms": ("手机号登录", "手机登录", "验证码登录", "短信登录", "phone login"),
    "email_password": ("密码", "邮箱", "账号", "email", "password", "帐号"),
    "apple": ("Apple", "苹果", "apple", "Apple ID"),
}
_LOGIN_ICON_ALIAS_GROUPS = {
    "wechat": ("微信", "微信登录", "微信登录方式", "WeChat"),
    "phone_sms": ("手机号登录", "手机登录", "验证码登录", "手机号登录方式", "短信登录"),
    "email_password": ("账号密码", "邮箱密码", "密码登录", "邮箱登录", "账号密码登录"),
    "apple": ("苹果", "Apple", "appleid", "Apple ID", "苹果账号", "苹果登录"),
}

# 全局手势计数（用于「每 50 次点击/滑动最多按一次返回」）
_GESTURE_COUNT = 0
_BACK_PENDING = False
_BACK_FLUSH_EVERY = 50


def _gesture_tick() -> None:
    global _GESTURE_COUNT, _BACK_PENDING
    _GESTURE_COUNT += 1
    if _BACK_PENDING and _GESTURE_COUNT >= _BACK_FLUSH_EVERY:
        _flush_back()


def _schedule_back() -> None:
    global _BACK_PENDING
    _BACK_PENDING = True
    _gesture_tick()


def _flush_back() -> None:
    global _GESTURE_COUNT, _BACK_PENDING
    if not _BACK_PENDING:
        return
    try:
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
        import builtins

        sn = getattr(builtins, "TARGET_DEVICE_SN", None)
        if sn:
            engine, _ = bootstrap_mobile_engine(str(sn), "android")
            if hasattr(engine, "press_key"):
                SLog.i(TAG, "Copilot deferred back key")
                engine.press_key("back")
    except Exception as e:
        SLog.w(TAG, f"deferred back failed: {e}")
    _BACK_PENDING = False
    _GESTURE_COUNT = 0


def _task_payload(
    node_code: str,
    *,
    platform: str = "mobile",
    data: Optional[Dict] = None,
    display_name: str = "copilot",
) -> Dict[str, Any]:
    return {
        "id": f"copilot-{uuid.uuid4().hex[:8]}",
        "nodeCode": node_code,
        "nodeType": 200,
        "platform": platform,
        "displayName": display_name,
        "lastCodes": [],
        "nextCodes": [],
        "data": data or {},
    }


def _execute_ability(payload: Dict[str, Any]) -> Dict[str, Any]:
    from driver.tentacle.manager import Manager

    try:
        result = Manager().execute_interface(payload)
        if result is None:
            return {"ok": False, "msg": "组件未执行或节点被跳过"}
        if hasattr(result, "to_dict"):
            d = result.to_dict()
            ok = d.get("success", d.get("code") in (200, None))
            return {"ok": bool(ok), "msg": d.get("msg", ""), "data": d}
        return {"ok": True, "data": result}
    except Exception as e:
        SLog.e(TAG, f"execute failed: {e}")
        return {"ok": False, "msg": str(e)}


def _run_mobile_back(sn: str, platform: str = "android", *, immediate: bool = True) -> Dict[str, Any]:
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
        from script.sleep import mSleep

        builtins.TARGET_DEVICE_SN = sn
        engine, _ = bootstrap_mobile_engine(sn, platform)
        if immediate and hasattr(engine, "press_key"):
            engine.press_key("back")
            mSleep(0.6)
            SLog.i(TAG, f"Back audit immediate sn={sn}")
            return {"ok": True, "msg": "已执行返回", "method": "back_immediate"}
        _schedule_back()
        return {"ok": True, "msg": "已登记返回（满 50 次手势后执行一次）", "method": "back_deferred"}
    except Exception as e:
        return {"ok": False, "msg": str(e), "method": "back"}


def _run_mobile_stop_app(sn: str, package: str, platform: str = "android") -> Dict[str, Any]:
    if not package:
        return {"ok": False, "msg": "未指定应用包名"}
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

        builtins.TARGET_DEVICE_SN = sn
        engine, _ = bootstrap_mobile_engine(sn, platform)
        if hasattr(engine, "stop_app"):
            engine.stop_app(package)
            return {"ok": True, "msg": f"已关闭 {package}"}
        return {"ok": False, "msg": "引擎不支持 stop_app"}
    except Exception as e:
        SLog.w(TAG, f"stop app failed: {e}")
        return {"ok": False, "msg": str(e)}


def _run_mobile_swipe(
    sn: str, direction: str, platform: str = "android"
) -> Dict[str, Any]:
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

        builtins.TARGET_DEVICE_SN = sn
        engine, (w, h) = bootstrap_mobile_engine(sn, platform)
        if not hasattr(engine, "swipe_norm"):
            return {"ok": False, "msg": "设备引擎不支持滑动"}
        if direction == "up":
            engine.swipe_norm(0.5, 0.72, 0.5, 0.38, 0.35)
        elif direction == "down":
            engine.swipe_norm(0.5, 0.38, 0.5, 0.72, 0.35)
        elif direction == "left":
            engine.swipe_norm(0.78, 0.5, 0.22, 0.5, 0.35)
        else:
            engine.swipe_norm(0.22, 0.5, 0.78, 0.5, 0.35)
        _gesture_tick()
        return {"ok": True, "msg": f"滑动 {direction}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def _run_mobile_input(
    sn: str,
    text: str,
    *,
    field_hint: str = "",
    platform: str = "android",
) -> Dict[str, Any]:
    """向当前页输入框填入文本（优先 u2 EditText，兜底 adb input text）。"""
    value = (text or "").strip()
    if not value:
        return {"ok": False, "msg": "输入内容为空", "method": "input"}
    gesture = None
    try:
        from server.services.regression_run_context import finish_gesture, record_gesture

        summary = f"输入{field_hint or '文本'} {value}"
        gesture = record_gesture(
            "input",
            summary,
            label=field_hint or "输入框",
            method="input",
            source="copilot",
        )
    except Exception:
        gesture = None
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

        builtins.TARGET_DEVICE_SN = sn
        engine, (screen_w, screen_h) = bootstrap_mobile_engine(sn, platform)
        if hasattr(engine, "ensure_screen_ready"):
            try:
                engine.ensure_screen_ready(node_sn=sn)
            except Exception:
                pass

        hints = [field_hint, "手机号", "手机", "请输入", "验证码"]
        hints = [h for h in hints if h]
        typed = False
        method = "input"

        d = engine._ensure_u2() if hasattr(engine, "_ensure_u2") else None
        if d:
            selectors = []
            for hint in hints:
                selectors.extend([
                    {"className": "android.widget.EditText", "textContains": hint},
                    {"className": "android.widget.EditText", "descriptionContains": hint},
                ])
            selectors.append({"className": "android.widget.EditText"})
            selectors.append({"focused": True})
            for spec in selectors:
                try:
                    sel = d(**spec)
                    if not sel.exists(timeout=0.6):
                        continue
                    sel.click()
                    time.sleep(0.25)
                    try:
                        sel.clear_text()
                    except Exception:
                        pass
                    sel.set_text(value)
                    typed = True
                    method = "u2_input"
                    break
                except Exception:
                    continue

        if not typed and hasattr(engine, "send_keys"):
            try:
                engine.click(screen_w // 2, int(screen_h * 0.38))
            except Exception:
                pass
            time.sleep(0.3)
            engine.send_keys(None, value)
            typed = True
            method = "adb_input"

        if typed:
            _gesture_tick()
            if gesture is not None:
                try:
                    finish_gesture(gesture, ok=True, msg=f"输入「{value}」")
                    gesture["method"] = method
                    gesture["action_name"] = "Input"
                except Exception:
                    pass
            out: Dict[str, Any] = {
                "ok": True,
                "msg": f"输入「{value}」",
                "method": method,
                "kind": "input",
                "text": value,
                "field_hint": field_hint,
            }
            if gesture:
                out["gestures"] = [gesture]
                out["screenshot_before"] = gesture.get("screenshot_before") or ""
                out["screenshot_after"] = gesture.get("screenshot_after") or ""
            return out
        if gesture is not None:
            try:
                finish_gesture(gesture, ok=False, msg=f"未找到可输入的输入框：{value}")
            except Exception:
                pass
        return {"ok": False, "msg": f"未找到可输入的输入框：{value}", "method": method}
    except Exception as e:
        SLog.w(TAG, f"mobile input failed: {e}")
        if gesture is not None:
            try:
                finish_gesture(gesture, ok=False, msg=str(e))
            except Exception:
                pass
        return {"ok": False, "msg": str(e), "method": "input"}


def _label_variants(label: str) -> List[str]:
    raw = (label or "").strip()
    if not raw:
        return []
    out: List[str] = []

    def _add(cand: str) -> None:
        c = (cand or "").strip()
        if c and c not in out:
            out.append(c)

    _add(raw)
    _add(re.sub(r"(按钮|按键|图标|入口|菜单)$", "", raw).strip())
    _add(re.sub(r"(按钮|按键|图标|入口|菜单|tab|Tab|TAB)$", "", raw, flags=re.I).strip())
    if re.search(r"底部|底栏", raw):
        core_tab = re.sub(r"底部|底栏", "", raw, flags=re.I).strip()
        core_tab = re.sub(r"tab|Tab|TAB", "", core_tab, flags=re.I).strip()
        core_tab = core_tab.strip("「」『』\"' ")
        _add(core_tab)
    parsed_tab = parse_bottom_tab_label(raw)
    if parsed_tab:
        _add(parsed_tab)
    # 用例里常见「登录页面一键登录按钮」→ 抽核心文案
    _add(re.sub(r"^.{0,16}(页面|页|界面|屏幕)", "", raw).strip())
    _add(re.sub(r"^(点击|点|选择|进入|打开)", "", raw).strip())
    core = re.sub(r"^.{0,16}(页面|页|界面|屏幕)", "", raw)
    core = re.sub(r"(按钮|按键|图标|入口|菜单)$", "", core).strip()
    _add(core)
    if _is_one_click_login_label(raw):
        _add("一键登录")
        _add("本机号码一键登录")
        _add("本机号码")
    intent = _classify_login_method_intent(raw)
    if intent and intent != "one_click":
        for alias in _LOGIN_ICON_ALIAS_GROUPS.get(intent, ()):
            _add(alias)
    return out


def _make_target_rect(
    left: int,
    top: int,
    width: int,
    height: int,
    *,
    label: str = "",
) -> Dict[str, Any]:
    w = max(1, int(width))
    h = max(1, int(height))
    l = int(left)
    t = int(top)
    return {
        "left": l,
        "top": t,
        "width": w,
        "height": h,
        "center": [l + w // 2, t + h // 2],
        "label": (label or "").strip(),
    }


def parse_bottom_tab_label(label: str) -> Optional[str]:
    """从「底部想要tab」「底栏[消息]」等口语描述提取 Tab 文案。"""
    raw = (label or "").strip()
    if not raw:
        return None
    if re.search(r"协议|勾选|checkbox|复选框|单选框|隐私条款|用户协议", raw, re.I):
        return None
    if not (re.search(r"底部|底栏", raw) or re.search(r"tab", raw, re.I)):
        return None
    t = re.sub(r"^(点击|点一下|tap|click)\s*", "", raw, flags=re.I).strip()
    t = re.sub(r"底部|底栏", "", t, flags=re.I).strip()
    t = re.sub(r"tab|Tab|TAB", "", t, flags=re.I).strip()
    t = t.strip("「」『』\"' \t")
    return t or None


def _is_bottom_tab_intent(label: str) -> bool:
    return bool(parse_bottom_tab_label(label))


_BOTTOM_TAB_NAMES = frozenset(
    {"首页", "想要", "消息", "我的", "我", "发现", "探索", "分类", "购物车"}
)
_NON_BOTTOM_TAB_SHORT = frozenset(
    {
        "下一步",
        "确认",
        "确定",
        "取消",
        "提交",
        "完成",
        "发送",
        "关注",
        "返回",
        "关闭",
        "保存",
        "删除",
        "编辑",
        "搜索",
    }
)
_LOGIN_DISCLAIMER_MARKERS = ("运营商", "认证服务", "服务由", "条款", "协议", "隐私")
_SEGMENT_TAB_NAMES = frozenset(
    {"造物秀", "AI创意", "想要成真", "真造物秀", "怪兽", "艺术家专区"}
)


def _is_segment_tab_query(label: str) -> bool:
    raw = (label or "").strip()
    if not raw:
        return False
    core = re.sub(r"^(点击|点一下|tap|click|进入|打开)\s*", "", raw, flags=re.I).strip()
    core = re.sub(r"(列表|页面|界面|tab)$", "", core, flags=re.I).strip()
    if core in _SEGMENT_TAB_NAMES:
        return True
    return any(name in raw for name in _SEGMENT_TAB_NAMES)


def _is_probable_bottom_tab_query(label: str) -> bool:
    """仅明确底栏 Tab 才走底栏定位，避免「下一步」「【关注】」误走底栏。"""
    raw = (label or "").strip()
    if not raw:
        return False
    if _is_toggle_intent(raw):
        return False
    if _is_segment_tab_query(raw):
        return False
    if _is_bottom_tab_intent(raw):
        return True
    if re.search(r"[【\[]", raw) or re.search(r"登录|一键|按钮|协议|隐私|同意|密码|验证码|下一步|关注", raw):
        return False
    core = re.sub(r"^(点击|点一下|tap|click)\s*", "", raw, flags=re.I).strip()
    core = core.strip("「」『』【】\"' \t")
    if core in _NON_BOTTOM_TAB_SHORT:
        return False
    if core in _BOTTOM_TAB_NAMES:
        return True
    if re.search(r"底部|底栏", raw):
        return len(core) <= 6
    return False


def _match_bottom_tab_label(query: str, target: str) -> bool:
    """底栏 Tab 仅精确匹配，禁止「想要」命中「想要成真」。"""
    q = (query or "").strip()
    t = (target or "").strip()
    if not q or not t:
        return False
    if q == t:
        return True
    for vq in _label_variants(q):
        if vq == t:
            return True
        for vt in _label_variants(t):
            if vq == vt:
                return True
    return False


def _in_bottom_bar_band(cy: int, screen_h: int, *, ratio: float = 0.86) -> bool:
    return cy >= int(screen_h * ratio)


def _discover_segment_tab_targets(
    engine,
    screen_w: int,
    screen_h: int,
) -> List[Tuple[Any, str]]:
    """顶栏分段 Tab（如「造物秀」「AI创意」），y 约在屏高 5%~28%。"""
    try:
        from driver.agent.Crawl.ui_discovery import (
            discover_clickables_from_hierarchy,
            discover_clickables_ocr,
        )

        y_max = int(screen_h * 0.28)
        y_min = int(screen_h * 0.04)
        merged: List[Tuple[int, Any, str]] = []
        seen: set = set()

        def consider(t, source: str) -> None:
            cy = t.y + t.h // 2
            if cy < y_min or cy > y_max:
                return
            name = (t.label or "").strip()
            if not name or len(name) > 12:
                return
            key = (name, int(t.x) // 12, int(t.y) // 12)
            if key in seen:
                return
            seen.add(key)
            merged.append((int(t.x), t, source))

        for t in discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=80):
            consider(t, "hierarchy")

        shot = None
        if hasattr(engine, "screenshot"):
            try:
                shot = engine.screenshot()
            except Exception:
                shot = None
        if shot is not None:
            for t in discover_clickables_ocr(shot, screen_w, screen_h, max_items=48):
                consider(t, "ocr")

        merged.sort(key=lambda x: x[0])
        return [(t, src) for _, t, src in merged]
    except Exception as e:
        SLog.w(TAG, f"discover segment tab targets failed: {e}")
        return []


def _resolve_segment_tab_target(
    label: str,
    engine,
    screen_w: int,
    screen_h: int,
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[Tuple[int, int]], str, str, Optional[Dict[str, Any]]]:
    raw = (label or "").strip()
    search_names: List[str] = []
    for name in _SEGMENT_TAB_NAMES:
        if name in raw:
            search_names.append(name)
    if not search_names:
        core = re.sub(r"^(点击|点一下|tap|click|进入|打开)\s*", "", raw, flags=re.I).strip()
        core = re.sub(r"(列表|页面|界面)$", "", core).strip()
        if core:
            search_names.append(core)
    search_names.extend(_label_variants(raw))

    targets = _discover_segment_tab_targets(engine, screen_w, screen_h)
    for t, source in targets:
        for cand in search_names:
            if cand and (cand == t.label or cand in (t.label or "")):
                cx, cy = t.center
                rect = _make_target_rect(t.x, t.y, t.w, t.h, label=t.label)
                method = "hierarchy" if source == "hierarchy" else "ocr"
                return (cx, cy), method, f"顶栏「{t.label}」@({cx},{cy})", rect

    clip_hit = _try_clip_resolve(
        engine, screen_w, screen_h, label=raw, icon_targets=icon_targets
    )
    if clip_hit and clip_hit[0]:
        cx, cy = clip_hit[0]
        rect = clip_hit[3]
        method = clip_hit[1] or "clip"
        if rect and int(rect.get("top") or cy) <= int(screen_h * 0.28):
            return clip_hit

    visible = list({(t.label or "").strip() for t, _ in targets if (t.label or "").strip()})
    detail = (
        f"顶栏未找到「{search_names[0] if search_names else raw}」"
        + (f"，当前可见：{'、'.join(visible)}" if visible else "")
    )
    return None, "none", detail, None


def _discover_bottom_tab_targets(
    engine,
    screen_w: int,
    screen_h: int,
) -> List[Tuple[Any, str]]:
    """从当前屏底栏区域收集可点击 Tab（真实 bounds，来自层级 + OCR）。"""
    try:
        from driver.agent.Crawl.ui_discovery import (
            discover_clickables_from_hierarchy,
            discover_clickables_ocr,
        )

        y_min = int(screen_h * 0.86)
        merged: List[Tuple[int, Any, str]] = []
        seen: set = set()

        def consider(t, source: str) -> None:
            cy = t.y + t.h // 2
            if cy < y_min:
                return
            name = (t.label or "").strip()
            if not name or len(name) > 10:
                return
            if re.search(r"随意|随机|切换", name):
                return
            key = (name, int(t.x) // 12, int(t.y) // 12)
            if key in seen:
                return
            seen.add(key)
            merged.append((int(t.x), t, source))

        for t in discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=80):
            consider(t, "hierarchy")

        shot = None
        if hasattr(engine, "screenshot"):
            try:
                shot = engine.screenshot()
            except Exception:
                shot = None
        if shot is not None:
            for t in discover_clickables_ocr(shot, screen_w, screen_h, max_items=48):
                consider(t, "ocr")

        merged.sort(key=lambda x: x[0])
        return [(t, src) for _, t, src in merged]
    except Exception as e:
        SLog.w(TAG, f"discover bottom tab targets failed: {e}")
        return []


def _resolve_bottom_tab_target(
    label: str,
    engine,
    screen_w: int,
    screen_h: int,
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Optional[Tuple[int, int]], str, str, Optional[Dict[str, Any]]]:
    """仅在当前屏底栏已识别到的控件上匹配 Tab，不做槽位估算。"""
    parsed = parse_bottom_tab_label(label) or (label or "").strip()
    search_names: List[str] = []
    if parse_bottom_tab_label(label):
        search_names.append(parsed)
    search_names.extend(_label_variants(label))

    targets = _discover_bottom_tab_targets(engine, screen_w, screen_h)
    for t, source in targets:
        for cand in search_names:
            if _match_bottom_tab_label(cand, t.label):
                cx, cy = t.center
                rect = _make_target_rect(t.x, t.y, t.w, t.h, label=t.label)
                method = "hierarchy" if source == "hierarchy" else "ocr"
                return (cx, cy), method, f"底栏「{t.label}」@({cx},{cy})", rect

    icon_hit = _resolve_icon_target(
        parsed, icon_targets, screen_w=screen_w, screen_h=screen_h
    )
    if icon_hit:
        cx, cy, detail, rect = icon_hit
        return (cx, cy), "icon_target", detail, rect

    clip_hit = _try_clip_resolve(
        engine,
        screen_w,
        screen_h,
        label=parsed,
        icon_targets=icon_targets,
        region="bottom",
    )
    if clip_hit and clip_hit[0]:
        cx, cy = clip_hit[0]
        if _in_bottom_bar_band(cy, screen_h):
            return clip_hit
        SLog.i(TAG, f"CLIP bottom tab reject: cy={cy} label={label!r}")

    visible: List[str] = []
    for t, _ in targets:
        name = (t.label or "").strip()
        if name and name not in visible:
            visible.append(name)
    if visible:
        detail = f"底栏未找到「{parsed}」，当前可见 Tab：{'、'.join(visible)}"
    else:
        detail = (
            f"底栏未找到「{parsed}」：当前屏未识别到底栏 Tab 文案"
            "（请确认在首页且底栏可见；无字 Tab 请录入图标库）"
        )
    return None, "none", detail, None


def _is_login_related_label(label: str) -> bool:
    return any(k in (label or "") for k in ("登录", "一键", "手机号", "注册"))


def _is_one_click_login_label(label: str) -> bool:
    """仅「本机号码一键登录」主按钮，不含底部图标入口。"""
    raw = (label or "").strip()
    if not raw:
        return False
    if re.search(r"微信|苹果|Apple|邮箱|密码|验证码|游客|账号密码", raw, re.I):
        return False
    if re.search(r"手机(号)?.*(方式|登录方式)", raw) and "一键" not in raw and "本机" not in raw:
        return False
    return bool(re.search(r"一键|本机号码", raw))


def _classify_login_method_intent(label: str) -> Optional[str]:
    """登录页操作意图：one_click / wechat / phone_sms / email_password / apple。"""
    raw = (label or "").strip()
    if not raw or "登录" not in raw:
        return None
    if _is_one_click_login_label(raw):
        return "one_click"
    if re.search(r"微信", raw):
        return "wechat"
    if re.search(r"苹果|apple\s*id|appleid", raw, re.I):
        return "apple"
    if re.search(r"邮箱|账号密码|密码登录|密码方式|帐号密码", raw):
        return "email_password"
    if re.search(r"手机(号)?.*(登录|方式)|验证码|短信", raw):
        return "phone_sms"
    if re.search(r"访客|游客", raw):
        return None
    return None


def _discover_login_icon_row(
    engine,
    screen_w: int,
    screen_h: int,
) -> List:
    """登录页主按钮与协议行之间的无字图标行（左→右）。"""
    from driver.agent.Crawl.ui_discovery import discover_clickables_from_hierarchy

    y_lo = int(screen_h * 0.66)
    y_hi = int(screen_h * 0.87)
    max_w = int(screen_w * 0.22)
    max_h = int(screen_h * 0.08)
    skip_fragments = (
        "一键",
        "本机",
        "访客",
        "同意",
        "协议",
        "阅读",
        "造好",
        "登录按钮",
        "checkbox",
        "CheckBox",
        "运营商",
        "认证服务",
    )

    candidates = []
    for t in discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=96):
        cy = t.y + t.h // 2
        if cy < y_lo or cy > y_hi:
            continue
        if t.w > max_w or t.h > max_h or t.w < 20 or t.h < 20:
            continue
        lbl = (t.label or "").strip()
        low = lbl.lower()
        if any(k in lbl or k in low for k in skip_fragments):
            continue
        if len(lbl) > 8 and re.search(r"[\u4e00-\u9fff]{3,}", lbl):
            continue
        candidates.append(t)

    if not candidates:
        return []

    centers_y = [t.y + t.h // 2 for t in candidates]
    centers_y.sort()
    row_y = centers_y[len(centers_y) // 2]
    band = max(36, int(screen_h * 0.035))
    row = [t for t in candidates if abs((t.y + t.h // 2) - row_y) <= band]
    row.sort(key=lambda t: t.x)
    return row


def _clip_search_params(label: str) -> Tuple[str, List[str], Optional[str]]:
    """
    从自然语言指令提炼 CLIP 检索词 / 别名 / 区域。
    一期：造好物登录链路走 clip_query_plan 表；未命中再走通用启发式。
    """
    raw = (label or "").strip()
    if not raw:
        return "", [], None

    try:
        from server.services.locate.clip_query_plan import clip_params_from_plan, lookup_clip_query_plan

        plan = lookup_clip_query_plan(raw)
        if plan:
            return clip_params_from_plan(plan, raw)
    except Exception:
        pass

    parsed_tab = parse_bottom_tab_label(raw)
    if parsed_tab:
        aliases = [raw] if raw != parsed_tab else []
        return parsed_tab, aliases, "bottom"

    if _is_segment_tab_query(raw):
        for name in _SEGMENT_TAB_NAMES:
            if name in raw:
                return name, [raw], "segment"

    if re.search(r"勾选|勾上|checkbox|单选|radio|复选", raw, re.I):
        from server.services.toggle_locate_service import parse_toggle_intent, toggle_clip_queries

        intent = parse_toggle_intent(raw)
        region = "bottom" if re.search(r"底部|底栏|协议", raw) else "full"
        if intent:
            q, extras = toggle_clip_queries(intent)
            return q, extras + ([raw] if raw != q else []), region
        q = "empty checkbox"
        return q, ["checkbox", "round checkbox", raw], region

    if re.search(r"下一步|继续|提交", raw):
        return (
            re.sub(r"^(点击|点一下)\s*", "", raw).strip() or "下一步",
            [raw],
            "full",
        )

    intent = _classify_login_method_intent(raw)
    if intent == "one_click":
        # 主按钮在屏中上部，与底栏图标行无关 → 全屏 CLIP
        return (
            "本机号码一键登录",
            ["一键登录", "one click login button", "本机号码", "登录按钮"],
            "full",
        )
    if intent and intent in _LOGIN_ICON_ALIAS_GROUPS:
        group = list(_LOGIN_ICON_ALIAS_GROUPS[intent])
        visual_primary = {
            "phone_sms": "smartphone mobile phone outline icon button",
            "wechat": "wechat green chat bubble icon",
            "email_password": "user account password login icon",
            "apple": "apple logo sign in icon",
        }.get(intent, group[0])
        extras = list(group[1:]) + [raw] if raw and raw not in group else list(group[1:])
        if intent == "phone_sms":
            extras.extend(
                [
                    "phone icon",
                    "mobile phone icon",
                    "手机图标",
                    "smartphone icon",
                ]
            )
        return visual_primary, extras, "login_row"

    q, pos_hint = _extract_ui_text_core(raw)
    if not q:
        q = raw

    region = pos_hint
    if region is None and re.search(r"底部|底栏", raw) and not re.search(r"协议|勾选", raw):
        region = "bottom"
    elif region is None and re.search(r"顶栏|分段", raw):
        region = "segment"

    aliases = [raw, q] if q != raw else [raw]
    return q, list(dict.fromkeys(aliases)), region


def _is_disagree_label(text: str) -> bool:
    t = (text or "").strip()
    return t == "不同意" or t.startswith("不同意")


def _extract_ui_text_core(label: str) -> Tuple[str, Optional[str]]:
    """从「登录页面点击右上角的访客浏览」提取核心文案与方位。"""
    raw = (label or "").strip()
    pos_hint: Optional[str] = None
    if re.search(r"右上角|右上", raw):
        pos_hint = "top_right"
    elif re.search(r"左上角|左上", raw):
        pos_hint = "top_left"
    elif re.search(r"底部|底栏", raw) and not re.search(r"协议|勾选", raw):
        pos_hint = "bottom"

    q = re.sub(r"^(点击|点一下|tap|click|进入|打开)\s*", "", raw, flags=re.I).strip()
    q = re.sub(r"^(登录页(面)?|页面)(上的|中的)?\s*", "", q)
    q = re.sub(r"^(右上角|左上角|右下角|左下角|底部|底栏|顶部)(的)?\s*", "", q)
    q = q.strip("「」『』【】\"' \t的")
    return q or raw, pos_hint


def _in_top_right_band(cx: int, cy: int, screen_w: int, screen_h: int) -> bool:
    return cy <= int(screen_h * 0.32) and cx >= int(screen_w * 0.48)


def _in_top_left_band(cx: int, cy: int, screen_w: int, screen_h: int) -> bool:
    return cy <= int(screen_h * 0.32) and cx <= int(screen_w * 0.52)


def _position_band_ok(
    pos_hint: Optional[str], cx: int, cy: int, screen_w: int, screen_h: int
) -> bool:
    if pos_hint == "top_right":
        return _in_top_right_band(cx, cy, screen_w, screen_h)
    if pos_hint == "top_left":
        return _in_top_left_band(cx, cy, screen_w, screen_h)
    if pos_hint == "bottom":
        return _in_bottom_bar_band(cy, screen_h)
    return True


def _should_try_text_locate_first(label: str) -> bool:
    if not label:
        return False
    if _classify_login_method_intent(label):
        return False
    if _is_toggle_intent(label) or _is_consent_action_label(label):
        return False
    if re.search(r"图标|icon", label, re.I):
        return False
    core, _ = _extract_ui_text_core(label)
    return bool(core and re.search(r"[\u4e00-\u9fff]", core) and len(core) <= 24)


def _resolve_text_click_target(
    engine,
    screen_w: int,
    screen_h: int,
    label: str,
) -> Tuple[Optional[Tuple[int, int]], str, str, Optional[Dict[str, Any]]]:
    """短中文文案优先 hierarchy/OCR（CLIP 不适合纯文字链接）。"""
    core, pos_hint = _extract_ui_text_core(label)
    query = core or label
    try:
        from driver.agent.Crawl.ui_discovery import (
            discover_clickables_from_hierarchy,
            discover_clickables_ocr,
        )

        hier_pool = list(
            discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=64)
        )
        hier_pool = [
            t
            for t in hier_pool
            if _position_band_ok(pos_hint, t.center[0], t.center[1], screen_w, screen_h)
        ]
        pick = _pick_best_text_clickable(query, hier_pool, screen_h=screen_h)
        if pick:
            cx, cy, txt, score, t = pick
            rect = _make_target_rect(t.x, t.y, t.w, t.h, label=txt)
            return (
                (cx, cy),
                "hierarchy",
                f"层级「{txt}」@({cx},{cy}) sim={score:.2f}",
                rect,
            )

        shot = engine.screenshot() if hasattr(engine, "screenshot") else None
        if shot is not None:
            ocr_pool = list(discover_clickables_ocr(shot, screen_w, screen_h, max_items=32))
            ocr_pool = [
                t
                for t in ocr_pool
                if _position_band_ok(pos_hint, t.center[0], t.center[1], screen_w, screen_h)
            ]
            pick = _pick_best_text_clickable(query, ocr_pool, screen_h=screen_h)
            if pick:
                cx, cy, txt, score, t = pick
                rect = _make_target_rect(t.x, t.y, t.w, t.h, label=txt)
                return (
                    (cx, cy),
                    "ocr",
                    f"OCR「{txt}」@({cx},{cy}) sim={score:.2f}",
                    rect,
                )
    except Exception as e:
        SLog.w(TAG, f"text locate failed label={label!r}: {e}")
    return None, "none", f"未找到文本「{query}」", None


def _label_similarity(query: str, candidate: str) -> float:
    """文本贴合度；成对按钮（同意/不同意）按语义区分，不依赖左右位置。"""
    from difflib import SequenceMatcher

    q = (query or "").strip()
    c = (candidate or "").strip()
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in ("同意", "同意并继续") and c in ("同意", "同意并继续"):
        return 0.96
    neg_prefixes = ("不", "勿", "未", "非")
    for neg in neg_prefixes:
        if c.startswith(neg) and len(c) > len(q) and c[len(neg) :] == q:
            return 0.05
        if q.startswith(neg) and len(q) > len(c) and q[len(neg) :] == c:
            return 0.05
    if q in ("同意", "同意并继续") and ("不同意" in c or c.startswith("不同意")):
        return 0.05
    if c in ("同意", "同意并继续") and ("不同意" in q or q.startswith("不同意")):
        return 0.05
    ratio = SequenceMatcher(None, q, c).ratio()
    if (q in c or c in q) and min(len(q), len(c)) <= 4:
        longer = max(len(q), len(c))
        shorter = min(len(q), len(c))
        if longer > shorter + 1:
            ratio = min(ratio, shorter / longer)
    return ratio


def _min_label_similarity(query: str) -> float:
    q = (query or "").strip()
    if len(q) <= 4:
        return 0.82
    if len(q) <= 8:
        return 0.72
    return 0.58


def _pick_consent_agree_clickable(
    query: str,
    candidates: list,
    *,
    screen_h: int = 0,
) -> Optional[Tuple[int, int, str, float, Any]]:
    """consent 弹窗「同意」：排除「不同意」，同分时取更靠右的按钮。"""
    floor = _min_label_similarity(query)
    matches: List[Tuple[float, int, int, str, Any]] = []
    y_lo = int(screen_h * 0.32) if screen_h else 0
    y_hi = int(screen_h * 0.82) if screen_h else 0
    for t in candidates or []:
        txt = (getattr(t, "label", None) or "").strip()
        if not txt or _is_disagree_label(txt) or _is_legal_bearing_target(txt):
            continue
        if txt not in ("同意", "同意并继续") and not _is_consent_action_label(txt):
            continue
        cx, cy = t.center
        if screen_h and not (y_lo <= cy <= y_hi):
            continue
        score = _label_similarity(query, txt)
        if score < floor:
            continue
        matches.append((score, cx, cy, txt, t))
    if not matches:
        return None
    matches.sort(key=lambda m: (-m[0], -m[1]))
    score, cx, cy, txt, t = matches[0]
    return cx, cy, txt, score, t


def _pick_best_text_clickable(
    query: str,
    candidates: list,
    *,
    screen_h: int = 0,
    bottom_band_only: bool = False,
    max_label_len: int = 0,
    consent_modal: bool = False,
) -> Optional[Tuple[int, int, str, float, Any]]:
    """从可点击候选中按文本贴合度选最高分。"""
    if query in ("同意", "同意并继续") or consent_modal:
        hit = _pick_consent_agree_clickable(query, candidates, screen_h=screen_h)
        if hit:
            return hit
    best: Optional[Tuple[int, int, str, float, Any]] = None
    best_score = 0.0
    floor = _min_label_similarity(query)
    for t in candidates or []:
        txt = (getattr(t, "label", None) or "").strip()
        if not txt or _is_legal_bearing_target(txt):
            continue
        if max_label_len and len(txt) > max_label_len:
            continue
        cx, cy = t.center
        if bottom_band_only and screen_h > 0 and cy < int(screen_h * 0.45):
            continue
        score = _label_similarity(query, txt)
        if score < floor:
            continue
        if score > best_score:
            best_score = score
            best = (cx, cy, txt, score, t)
    return best


def _match_target_label(label: str, target_label: str) -> bool:
    a = (label or "").strip()
    b = (target_label or "").strip()
    if not a or not b:
        return False
    if _is_legal_bearing_target(b) and _is_login_related_label(a):
        return False
    # 「同意」是「不同意」的子串，禁止子串误匹配左侧按钮
    if _is_consent_action_label(a) and _is_disagree_label(b):
        return False
    if _is_consent_action_label(a):
        return b in ("同意", "同意并继续")
    for va in _label_variants(a):
        for vb in _label_variants(b):
            if va == vb:
                return True
            if _is_legal_bearing_target(b) or _is_legal_bearing_target(vb):
                continue
            if _is_consent_action_label(a) and _is_disagree_label(vb):
                continue
            if va in vb or vb in va:
                if a in ("同意", "同意并继续") and "不同意" in b:
                    continue
                return True
    return False


def _icon_names_match_label(label: str, names: List[str]) -> bool:
    for name in names:
        if name and _match_target_label(label, name):
            return True
    intent = _classify_login_method_intent(label)
    if intent and intent != "one_click":
        group = _LOGIN_ICON_ALIAS_GROUPS.get(intent, ())
        for name in names:
            n = (name or "").strip()
            if not n:
                continue
            if any(g in n or n in g for g in group):
                return True
    return False


def _resolve_icon_target(
    label: str,
    icon_targets: Optional[List[Dict[str, Any]]],
    *,
    screen_w: int = 0,
    screen_h: int = 0,
) -> Optional[Tuple[int, int, str, Dict[str, Any]]]:
    from server.services.figma_icon_service import scale_icon_target_rect

    for t in icon_targets or []:
        names = [(t.get("name") or "").strip()]
        for a in t.get("aliases") or []:
            if a:
                names.append(str(a).strip())
        if not _icon_names_match_label(label, names):
            continue
        name = names[0]
        if screen_w > 0 and screen_h > 0:
            x, y, w, h = scale_icon_target_rect(t, screen_w, screen_h)
        else:
            x, y = int(t.get("x") or 0), int(t.get("y") or 0)
            w, h = int(t.get("w") or 0), int(t.get("h") or 0)
        if w <= 0 or h <= 0:
            continue
        cx = x + w // 2
        cy = y + h // 2
        if cx > 0 and cy > 0:
            rect = _make_target_rect(x, y, w or 48, h or 48, label=name)
            src = "Figma" if "figma_norm" in (t.get("note") or "") else "图标库"
            return cx, cy, f"{src}目标「{name}」@({cx},{cy})", rect
    return None


def _is_legal_link_target(label: str) -> bool:
    t = (label or "").strip()
    return any(k in t for k in ("用户协议", "隐私条款", "隐私政策", "服务协议"))


def _is_legal_bearing_target(label: str) -> bool:
    """含协议链接或 consent 正文的长文案，不能作为「同意」等短指令的点击目标。"""
    t = (label or "").strip()
    if not t:
        return False
    if _is_legal_link_target(t):
        return True
    if len(t) > 10 and any(k in t for k in ("《", "》", "阅读", "点击", "条款", "协议", "隐私")):
        return True
    return False


def _is_consent_action_label(label: str) -> bool:
    t = (label or "").strip()
    return t in ("同意", "同意并继续", "接受", "我知道了", "继续")


def _is_toggle_intent(label: str) -> bool:
    from server.services.toggle_locate_service import is_toggle_intent

    return is_toggle_intent(label)


def _is_checkbox_intent(label: str) -> bool:
    return _is_toggle_intent(label)


def _dedupe_consecutive_toggle_steps(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """合并连续重复的勾选步骤（如误拆成「底部协议勾选框」+「框勾选框」）。"""
    out: List[Dict[str, Any]] = []
    for st in steps or []:
        if not out:
            out.append(st)
            continue
        prev = out[-1]
        if st.get("kind") != "click" or prev.get("kind") != "click":
            out.append(st)
            continue
        pl = (prev.get("label") or prev.get("summary") or "").strip()
        sl = (st.get("label") or st.get("summary") or "").strip()
        if not (_is_toggle_intent(pl) and _is_toggle_intent(sl)):
            out.append(st)
            continue
        if sl in pl or pl in sl or (sl.endswith("框") and len(sl) <= 6 and "协议" in pl):
            continue
        out.append(st)
    return out


def _try_clip_resolve(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    label: str,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    region: Optional[str] = None,
) -> Optional[Tuple[Optional[Tuple[int, int]], str, str, Optional[Dict[str, Any]]]]:
    if not label:
        return None
    try:
        from server.core.vision.clip_service import clip_enabled
        from server.services.clip_locate_service import infer_region_hint, try_clip_locate

        if not clip_enabled():
            SLog.i(TAG, f"CLIP skip label={label!r} (CLIP_ENABLED=0)")
            return None

        query, aliases, region_hint = _clip_search_params(label)
        region_key = region or region_hint or infer_region_hint(query)
        SLog.i(
            TAG,
            f"CLIP try label={label!r} query={query!r} region={region_key} "
            f"screen={screen_w}x{screen_h}",
        )
        clip_hit = try_clip_locate(
            engine,
            screen_w,
            screen_h,
            label=label,
            query=query,
            aliases=aliases,
            icon_targets=icon_targets,
            region=region_key,
        )
        if clip_hit[0] is not None:
            SLog.i(TAG, f"CLIP hit label={label!r} method={clip_hit[1]} {clip_hit[2]}")
            return clip_hit
        SLog.i(TAG, f"CLIP miss label={label!r} {clip_hit[2]}")
    except Exception as e:
        SLog.w(TAG, f"CLIP locate failed label={label!r}: {e}")
    return None


def _resolve_click_target(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    label: str = "",
    x: int = 0,
    y: int = 0,
    coords_explicit: bool = False,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    exact_label: bool = False,
    login_icon_order: Optional[Dict[str, int]] = None,
    prefer_text_locate: bool = False,
) -> Tuple[Optional[Tuple[int, int]], str, str, Optional[Dict[str, Any]]]:
    """
    解析点击目标，返回 (position|None, method, detail, target_rect)。
    优先级：显式坐标 → consent 安全定位 → toggle → CLIP(全屏/区域) → 图标库 → hierarchy/OCR → Tab → 无障碍 label
    method: clip_* | hierarchy | ocr | icon_target | coordinate | label
    """
    global _LAST_LOCATE_DEBUG

    if label:
        try:
            from server.services.overlay_guard_service import is_screen_blocked
            from server.services.page_navigation_service import is_overlay_dismiss_target_label

            if is_screen_blocked(engine) and not is_overlay_dismiss_target_label(label):
                from server.services.overlay_guard_service import (
                    blocked_overlay_message,
                    detect_blocking_overlay,
                )

                ov = detect_blocking_overlay(engine) or {}
                blocked_msg = blocked_overlay_message(engine) or "当前屏被阻塞弹窗占用"
                _LAST_LOCATE_DEBUG = {
                    "query": label,
                    "blocked_overlay": True,
                    "overlay_type": ov.get("type") or "",
                    "detail": blocked_msg,
                }
                return (
                    None,
                    "blocked_overlay",
                    blocked_msg,
                    None,
                )
        except Exception:
            pass

        intent = _classify_login_method_intent(label)
        if intent == "one_click":
            try:
                from server.services.page_navigation_service import (
                    _collect_ocr_text_only,
                    _screen_is_login_home,
                    _screen_is_login_surface,
                )

                blob = (_collect_ocr_text_only(engine) or "").strip()
                if blob and not _screen_is_login_surface(blob) and not _screen_is_login_home(blob):
                    _LAST_LOCATE_DEBUG = {
                        "query": label,
                        "wrong_page": True,
                        "detail": "不在登录页，跳过一键登录定位",
                    }
                    return None, "wrong_page", "当前不在登录页", None
            except Exception:
                pass

    if coords_explicit and x > 0 and y > 0:
        half = 24
        rect = _make_target_rect(x - half, y - half, half * 2, half * 2, label=label or f"({x},{y})")
        return (x, y), "coordinate", f"坐标({x},{y})", rect

    try:
        from server.services.locate.resolver import _locate_arbitrator_enabled, resolve_locate_target

        if label and _locate_arbitrator_enabled():
            lr = resolve_locate_target(
                engine,
                screen_w,
                screen_h,
                label=label,
                icon_targets=icon_targets,
            )
            _LAST_LOCATE_DEBUG = lr.debug
            if lr.ok:
                return lr.position, lr.method, lr.detail, lr.target_rect
    except Exception as e:
        SLog.w(TAG, f"locate arbitrator failed label={label!r}: {e}")

    intent = _classify_login_method_intent(label) if label else None
    if intent and intent != "one_click":
        row = _discover_login_icon_row(engine, screen_w, screen_h)
        if row:
            slot = (login_icon_order or {}).get(intent)
            if slot is None:
                slot = {"wechat": 1, "phone_sms": 2, "email_password": 3, "apple": 3}.get(intent)
            if slot is not None:
                idx = int(slot) - 1
                if 0 <= idx < len(row):
                    t = row[idx]
                    cx, cy = t.center
                    rect = _make_target_rect(t.x, t.y, t.w, t.h, label=label or intent)
                    detail = f"登录图标行第{slot}位 @({int(cx)},{int(cy)})"
                    _LAST_LOCATE_DEBUG = {
                        "query": label,
                        "profile": "login",
                        "kind": "icon",
                        "winner": {"channel": "icon_row", "method": "login_icon_row"},
                    }
                    return (int(cx), int(cy)), "login_icon_row", detail, rect

    if label and _should_try_text_locate_first(label):
        text_hit = _resolve_text_click_target(engine, screen_w, screen_h, label)
        if text_hit[0]:
            return text_hit

    clip_attempted = False
    if label and not _is_toggle_intent(label):
        clip_attempted = True
        clip_hit = _try_clip_resolve(
            engine, screen_w, screen_h, label=label, icon_targets=icon_targets
        )
        if clip_hit and clip_hit[0]:
            return clip_hit

    if label:
        icon_hit = _resolve_icon_target(
            label, icon_targets, screen_w=screen_w, screen_h=screen_h
        )
        if icon_hit:
            cx, cy, detail, rect = icon_hit
            return (cx, cy), "icon_target", detail, rect

    if label and _is_segment_tab_query(label):
        seg_hit = _resolve_segment_tab_target(
            label, engine, screen_w, screen_h, icon_targets=icon_targets
        )
        if seg_hit[0]:
            return seg_hit

    try:
        from driver.agent.Crawl.ui_discovery import (
            discover_clickables_from_hierarchy,
            discover_clickables_ocr,
        )

        shot = None
        tab_query = label and (
            _is_bottom_tab_intent(label) or _is_probable_bottom_tab_query(label)
        )
        if tab_query:
            tab_hit = _resolve_bottom_tab_target(
                label, engine, screen_w, screen_h, icon_targets=icon_targets
            )
            if tab_hit[0]:
                return tab_hit
            if _is_bottom_tab_intent(label):
                return tab_hit

        if label and not _is_consent_action_label(label):
            try:
                from server.core.vision.clip_service import clip_enabled

                login_visual = bool(_classify_login_method_intent(label))
                if clip_attempted and clip_enabled() and login_visual:
                    return (
                        None,
                        "none",
                        f"CLIP 未找到「{label}」，已跳过文本子串兜底以免误点",
                        None,
                    )
            except Exception:
                pass
            tab_only = _is_probable_bottom_tab_query(label)
            hier_pool = list(
                discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=48)
            )
            if tab_only:
                hier_pool = [
                    t
                    for t in hier_pool
                    if _in_bottom_bar_band(t.center[1], screen_h)
                    and _match_bottom_tab_label(label, t.label)
                ]
            pick = _pick_best_text_clickable(
                label,
                hier_pool,
                screen_h=screen_h,
                bottom_band_only=tab_only,
            )
            if pick:
                cx, cy, txt, score, t = pick
                rect = _make_target_rect(t.x, t.y, t.w, t.h, label=txt)
                return (
                    cx,
                    cy,
                ), "hierarchy", f"层级「{txt}」@({cx},{cy}) sim={score:.2f}", rect

            shot = None
            if hasattr(engine, "screenshot"):
                try:
                    shot = engine.screenshot()
                except Exception:
                    shot = None
            if shot is not None:
                ocr_pool = list(
                    discover_clickables_ocr(shot, screen_w, screen_h, max_items=24)
                )
                if tab_only:
                    ocr_pool = [
                        t
                        for t in ocr_pool
                        if _in_bottom_bar_band(t.center[1], screen_h)
                        and _match_bottom_tab_label(label, t.label)
                    ]
                pick = _pick_best_text_clickable(
                    label,
                    ocr_pool,
                    screen_h=screen_h,
                    bottom_band_only=tab_only,
                )
                if pick:
                    cx, cy, txt, score, t = pick
                    rect = _make_target_rect(t.x, t.y, t.w, t.h, label=txt)
                    return (
                        cx,
                        cy,
                    ), "ocr", f"OCR「{txt}」@({cx},{cy}) sim={score:.2f}", rect
            if tab_only:
                return _resolve_bottom_tab_target(
                    label, engine, screen_w, screen_h, icon_targets=icon_targets
                )
    except Exception as e:
        SLog.w(TAG, f"resolve click target failed: {e}")

    try:
        from server.services.locate.resolver import _locate_arbitrator_enabled

        arbitrator_on = _locate_arbitrator_enabled()
    except Exception:
        arbitrator_on = False

    if (
        label
        and hasattr(engine, "click_by_label")
        and not _is_toggle_intent(label)
        and not arbitrator_on
    ):
        use_exact = (
            exact_label
            or _is_probable_bottom_tab_query(label)
            or not any(k in label for k in ("用户协议", "隐私", "同意", "条款"))
        )
        for variant in _label_variants(label):
            fn = getattr(engine, "click_by_label")
            try:
                clicked = fn(variant, exact_only=use_exact)
            except TypeError:
                clicked = fn(variant)
            if clicked:
                if _is_legal_link_target(variant) and _is_login_related_label(label):
                    SLog.w(TAG, f"blocked legal link click variant={variant!r} for={label!r}")
                    continue
                rect = None
                try:
                    from driver.agent.Crawl.ui_discovery import discover_clickables_from_hierarchy

                    for t in discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=48):
                        if _match_target_label(label, t.label):
                            rect = _make_target_rect(t.x, t.y, t.w, t.h, label=t.label)
                            break
                except Exception:
                    pass
                return None, "label", f"无障碍文案「{variant}」", rect

    return None, "none", "未找到可点击目标", None


def _run_mobile_click(
    sn: str,
    x: int,
    y: int,
    *,
    label: str = "",
    platform: str = "android",
    coords_explicit: bool = False,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    exact_label: bool = False,
    skip_label_lookup: bool = False,
    consent_dismiss: bool = False,
    login_icon_order: Optional[Dict[str, int]] = None,
    skip_overlay_clear: bool = False,
) -> Dict[str, Any]:
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import (
            DeviceOfflineError,
            bootstrap_mobile_engine,
        )

        builtins.TARGET_DEVICE_SN = sn
        try:
            engine, (screen_w, screen_h) = bootstrap_mobile_engine(sn, platform)
        except DeviceOfflineError as e:
            return {"ok": False, "msg": str(e), "method": "device_offline"}

        if hasattr(engine, "ensure_screen_ready"):
            try:
                if not engine.ensure_screen_ready(node_sn=sn):
                    SLog.w(TAG, f"screen may still be off/locked sn={sn}")
            except Exception as e:
                SLog.w(TAG, f"pre-click ensure_screen_ready failed: {e}")

        consent_label = _is_consent_action_label(label)
        if consent_label:
            try:
                from server.services.page_navigation_service import _consent_dialog_gone

                if _consent_dialog_gone(engine):
                    _gesture_tick()
                    SLog.i(
                        TAG,
                        f"Click skip consent already cleared label={label!r}",
                    )
                    return _with_locate_debug(
                        {
                            "ok": True,
                            "msg": "隐私弹窗已关闭",
                            "method": "already_dismissed",
                            "screen_size": {"w": screen_w, "h": screen_h},
                            "target_label": label,
                        }
                    )
            except Exception as e:
                SLog.w(TAG, f"consent dismissed check failed: {e}")

        clear_locate_debug()

        pos, method, detail, target_rect = _resolve_click_target(
            engine,
            screen_w,
            screen_h,
            label=label,
            x=x,
            y=y,
            coords_explicit=coords_explicit,
            icon_targets=icon_targets,
            exact_label=exact_label,
            login_icon_order=login_icon_order,
        )

        login_related = bool(
            label
            and (
                _is_one_click_login_label(label)
                or _classify_login_method_intent(label) == "one_click"
            )
        )
        if pos is None and login_related and not skip_overlay_clear:
            try:
                from server.services.page_navigation_service import dismiss_blocking_on_engine

                SLog.i(TAG, f"login click retry after wake/overlay clear label={label!r}")
                if hasattr(engine, "ensure_screen_ready"):
                    engine.ensure_screen_ready(node_sn=sn)
                dismiss_blocking_on_engine(engine, screen_w, screen_h, max_rounds=4)
                clear_locate_debug()
                pos, method, detail, target_rect = _resolve_click_target(
                    engine,
                    screen_w,
                    screen_h,
                    label=label,
                    x=x,
                    y=y,
                    coords_explicit=coords_explicit,
                    icon_targets=icon_targets,
                    exact_label=exact_label,
                    login_icon_order=login_icon_order,
                )
            except Exception as e:
                SLog.w(TAG, f"login click retry failed: {e}")
        screen_size = {"w": screen_w, "h": screen_h}
        target_label = (target_rect or {}).get("label") or label
        clip_query, _, _ = _clip_search_params(label) if label else ("", [], None)

        clip_semantic_ok = (
            _is_toggle_intent(label)
            or _is_consent_action_label(label)
            or (method or "") == "already_checked"
            or (method or "").startswith(("toggle", "u2_toggle", "u2_checkbox"))
            or _match_target_label(label, target_label)
            or _icon_names_match_label(label, [target_label, clip_query])
            or (
                (method or "").startswith("clip")
                and clip_query
                and _match_target_label(label, clip_query)
            )
        )

        if (
            label
            and pos
            and (method or "").startswith("clip")
            and target_label
            and not clip_semantic_ok
            and not _is_toggle_intent(label)
            and not (
                _is_probable_bottom_tab_query(label)
                and _match_bottom_tab_label(label, target_label)
            )
        ):
            return {
                "ok": False,
                "msg": (
                    f"CLIP 命中「{target_label}」与指令「{label}」不符，已终止点击。"
                    f" {detail}"
                ),
                "method": method,
                "target_rect": target_rect,
                "screen_size": screen_size,
                "target_label": target_label,
            }

        if method == "already_checked":
            _gesture_tick()
            SLog.i(TAG, f"Click audit ok=True method=already_checked label={target_label!r}")
            return {
                "ok": True,
                "msg": detail or "协议已勾选",
                "method": method,
                "x": pos[0] if pos else None,
                "y": pos[1] if pos else None,
                "target_rect": target_rect,
                "screen_size": screen_size,
                "target_label": target_label or "协议勾选",
            }

        if pos is None:
            return _with_locate_debug(
                {
                    "ok": False,
                    "msg": detail or "未找到可点击目标，请指定坐标如：点击 1080,2450",
                    "method": method,
                    "target_rect": target_rect,
                    "screen_size": screen_size,
                }
            )

        cx, cy = pos
        if hasattr(engine, "click"):
            tap_label = target_label or label
            force_coord = skip_label_lookup or coords_explicit
            if _is_toggle_intent(label):
                tap_label = label or "协议勾选"
                force_coord = True
            try:
                clicked = engine.click(
                    None,
                    position=(cx, cy),
                    label=tap_label,
                    skip_label_lookup=force_coord,
                    exact_label=exact_label,
                    locate_method=method or "",
                )
            except TypeError:
                clicked = engine.click(None, position=(cx, cy), label=tap_label)
            if clicked is False:
                return _with_locate_debug(
                    {
                        "ok": False,
                        "msg": f"{detail} — 触控注入失败，请检查 USB 调试(安全设置) 与无障碍 ATX",
                        "method": method,
                        "x": cx,
                        "y": cy,
                        "target_rect": target_rect,
                        "screen_size": screen_size,
                        "target_label": target_label,
                    }
                )
            toggle_ok = True
            if _is_toggle_intent(label) and method != "already_checked":
                try:
                    from script.sleep import mSleep

                    from server.services.page_navigation_service import _login_checkbox_checked

                    mSleep(0.35)
                    toggle_ok = _login_checkbox_checked(engine)
                    if not toggle_ok:
                        try:
                            engine.click(
                                None,
                                position=(cx, cy),
                                label=tap_label,
                                skip_label_lookup=True,
                                locate_method=method or "",
                            )
                        except TypeError:
                            engine.click(None, position=(cx, cy), label=tap_label)
                        mSleep(0.35)
                        toggle_ok = _login_checkbox_checked(engine)
                except Exception as e:
                    SLog.w(TAG, f"toggle verify failed: {e}")
            if not toggle_ok:
                return _with_locate_debug(
                    {
                        "ok": False,
                        "msg": f"{detail} — 协议勾选未生效",
                        "method": method,
                        "x": cx,
                        "y": cy,
                        "target_rect": target_rect,
                        "screen_size": screen_size,
                        "target_label": target_label,
                    }
                )
            _gesture_tick()
            SLog.i(
                TAG,
                f"Click audit ok=True method={method} label={target_label!r} "
                f"pos=({cx},{cy}) detail={detail}",
            )
            return _with_locate_debug(
                {
                    "ok": True,
                    "msg": detail,
                    "method": method,
                    "x": cx,
                    "y": cy,
                    "target_rect": target_rect,
                    "screen_size": screen_size,
                    "target_label": target_label,
                }
            )
        return _with_locate_debug({"ok": False, "msg": "引擎不支持点击"})
    except Exception as e:
        from driver.agent.Crawl.device_bootstrap import DeviceOfflineError

        if isinstance(e, DeviceOfflineError):
            return {"ok": False, "msg": str(e), "method": "device_offline"}
        return {"ok": False, "msg": str(e)}


def _nav_route(text: str) -> Optional[Dict[str, str]]:
    t = text.lower()
    rules = [
        (("应用列表", "应用", "首页", "dashboard", "apps", "app list"), "AppList", "/report/apps"),
        (("设备", "device"), "DeviceManage", "/device"),
        (("定时", "schedule", "任务"), "Schedule", "/schedule"),
        (("时间线", "timeline", "日志"), "Timeline", "/timeline"),
        (("对话", "copilot", "dialogue", "助手"), "Dialogue", "/dialogue"),
        (("设置", "settings", "配置中心"), "SettingsHub", "/settings/hub"),
        (("资源", "resource"), "ResourceList", "/resources"),
    ]
    for keys, name, path in rules:
        if any(k in t for k in keys):
            return {"name": name, "path": path}
    return None


_PACKAGE_RE = re.compile(r"(com\.[a-zA-Z0-9_.]+)")


def _normalize_app_token(token: str) -> str:
    t = (token or "").strip()
    t = re.sub(r"(?:应用|软件|程序|app|APP|应用程序)$", "", t, flags=re.I).strip()
    return t


def _looks_like_package(token: str) -> bool:
    t = (token or "").strip()
    return bool(t) and ("." in t) and not re.search(r"[\u4e00-\u9fff]", t)


def _name_match_score(query: str, candidate: str) -> int:
    q = _normalize_app_token(query).lower()
    c = _normalize_app_token(candidate).lower()
    if not q or not c:
        return 0
    if q == c:
        return 100
    if q in c:
        return 80 + int(len(q) / max(len(c), 1) * 15)
    if c in q:
        return 70 + int(len(c) / max(len(q), 1) * 15)
    return 0


def _pkg_match_score(query: str, package: str) -> int:
    q = _normalize_app_token(query).lower().replace(" ", "")
    p = (package or "").lower()
    if not q or not p:
        return 0
    if q == p:
        return 95
    if q in p:
        return 55
    seg = p.rsplit(".", 1)[-1]
    if q == seg or q in seg or seg in q:
        return 65
    return 0


def _package_from_env(env_raw: Any) -> str:
    """从 app.env 或 project.env 解析 Android 包名。"""
    try:
        from server.services.project_env import normalize_project_env, profile_snapshot

        snap = profile_snapshot(normalize_project_env(env_raw or {}))
        return ((snap.get("android") or {}).get("package") or "").strip()
    except Exception:
        return ""


def _package_for_app_record(app) -> str:
    """应用包名：优先 app.env，否则继承所属项目的 project.env。"""
    pkg = _package_from_env(getattr(app, "env", None))
    if pkg:
        return pkg
    project = getattr(app, "project", None)
    if project:
        return _package_from_env(getattr(project, "env", None))
    return ""


def _resolve_app_from_db(name: str, *, app_id: Optional[str] = None) -> Optional[Tuple[str, str, str]]:
    """从 MiniOrange 项目/应用库解析 (package, source, display_name)。"""
    try:
        from server.core.database import SessionLocal
        from server.models.project import App, Project
        from sqlalchemy.orm import joinedload

        session = SessionLocal()
        try:
            best_pkg = ""
            best_name = ""
            best_score = 0

            app_query = session.query(App).options(joinedload(App.project))
            if app_id:
                app_query = app_query.filter(App.id == str(app_id))
            for app in app_query.all():
                pkg = _package_for_app_record(app)
                project_name = app.project.name if app.project else ""
                score = max(
                    _name_match_score(name, app.name),
                    _name_match_score(name, project_name),
                    _pkg_match_score(name, pkg),
                )
                if score > best_score:
                    best_score = score
                    best_pkg = pkg
                    best_name = app.name or project_name

            for project in session.query(Project).all():
                pkg = _package_from_env(project.env)
                score = max(_name_match_score(name, project.name), _pkg_match_score(name, pkg))
                if score > best_score:
                    best_score = score
                    best_pkg = pkg
                    best_name = project.name

            if best_score >= 55 and best_pkg:
                return best_pkg, "db", best_name
            if best_score >= 55 and not best_pkg:
                SLog.w(
                    TAG,
                    f"app name matched「{best_name}」but package empty; "
                    "check project env android.package",
                )
        finally:
            session.close()
    except Exception as e:
        SLog.w(TAG, f"resolve app from db failed: {e}")
    return None


def _resolve_app_from_device(sn: str, name: str) -> Optional[Tuple[str, str, str]]:
    """从手机已安装应用解析 (package, source, display_name)。"""
    if not sn:
        return None
    try:
        import uiautomator2 as u2

        d = u2.connect(str(sn))
        out = d.shell("pm list packages -3").output or ""
        pkgs = [
            line.replace("package:", "").strip()
            for line in out.splitlines()
            if line.strip().startswith("package:")
        ]
        best_pkg = ""
        best_label = ""
        best_score = 0
        for pkg in pkgs:
            label = ""
            try:
                info = d.app_info(pkg) or {}
                label = (info.get("label") or info.get("name") or "").strip()
            except Exception:
                label = ""
            score = max(_name_match_score(name, label), _pkg_match_score(name, pkg))
            if score > best_score:
                best_score = score
                best_pkg = pkg
                best_label = label or pkg
        if best_score >= 55 and best_pkg:
            return best_pkg, "device", best_label
    except Exception as e:
        SLog.w(TAG, f"resolve app from device failed: {e}")
    return None


def resolve_app_package(
    name: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
) -> Tuple[Optional[str], str, str]:
    """
    应用名/别名 → 包名。
    返回 (package|None, source, display_name)。
    """
    ctx = context or {}
    token = _normalize_app_token(name)
    if not token:
        return None, "", ""

    if _looks_like_package(token):
        return token, "package", token

    pkg_match = _PACKAGE_RE.search(token)
    if pkg_match:
        pkg = pkg_match.group(1)
        return pkg, "package", pkg

    ctx_pkg = (ctx.get("package") or ctx.get("android_package") or "").strip()
    ctx_name = (ctx.get("app_name") or ctx.get("appName") or "").strip()
    if ctx_pkg and (not ctx_name or _name_match_score(token, ctx_name) >= 55):
        return ctx_pkg, "context", ctx_name or ctx_pkg

    db_hit = _resolve_app_from_db(token, app_id=ctx.get("app_id") or ctx.get("appId"))
    if db_hit:
        return db_hit

    device_hit = _resolve_app_from_device(sn, token) if sn else None
    if device_hit:
        return device_hit

    return None, "", ""


def _extract_app_identifier(raw: str, operation: str) -> str:
    quoted = re.search(r"[「『\"']([^」』\"']+)[」』\"']", raw)
    if quoted:
        return _normalize_app_token(quoted.group(1))
    if operation == "open":
        m = re.search(r"(?:打开|启动|open|launch)\s*(.+)$", raw, re.I)
    else:
        m = re.search(r"(?:关闭|退出|关掉|关|close|kill|force[- ]?stop)\s*(.+)$", raw, re.I)
    if not m:
        return ""
    return _normalize_app_token(m.group(1))


def _plan_app_action(
    raw: str,
    operation: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
) -> Optional[Dict[str, Any]]:
    """operation: open | close"""
    pkg_match = _PACKAGE_RE.search(raw)
    if pkg_match:
        pkg = pkg_match.group(1)
        display = pkg
        source = "package"
    else:
        name = _extract_app_identifier(raw, operation)
        if not name:
            return None
        pkg, source, display = resolve_app_package(name, sn=sn, context=context)
        if not pkg:
            verb = "打开" if operation == "open" else "关闭"
            return {
                "error": (
                    f"未找到应用「{name}」对应的包名。"
                    f"请在项目「运行环境」中配置 Android 包名，或确认手机已安装该应用。"
                ),
                "reply_hint": f"{verb} {name}",
            }

    is_open = operation == "open"
    kind = "open_app" if is_open else "close_app"
    op = "start" if is_open else "close"
    verb = "启动" if is_open else "关闭"
    label = display or pkg
    src_hint = {"db": "项目环境", "device": "本机", "context": "上下文", "package": "包名"}.get(source, source)
    return {
        "step": {
            "kind": kind,
            "nodeCode": "public/window",
            "platform": "mobile",
            "data": {
                "operation": op,
                "target_mobile": pkg,
                "restart": is_open,
                "platform": "mobile",
            },
            "summary": f"{verb}应用 {label} ({pkg})",
            "app_name": label,
            "package": pkg,
            "resolve_source": source,
        },
        "reply": f"将{verb}「{label}」→ {pkg}（{src_hint}）",
    }


# 多指令拆分：标点 / 连接词 / 连续动词
_SPLIT_DELIM_RE = re.compile(
    r"(?:"
    r"然后|接着|之后|接下来|再然后|然后再|并且|并|"
    r"and then|then|after that|"
    r"[,，;；\n|]|"
    r"→|->"
    r")+",
    re.I,
)
_VERB_BOUNDARY_RE = re.compile(
    r"(?=(?:"
    r"打开|启动|关闭|退出|关掉|"
    r"输入|填写|填入|勾选|勾上|"
    r"点击|点一下|"
    r"上滑|下滑|左滑|右滑|滑动|滑一下|"
    r"截图|截屏|等待|返回|后退|"
    r"open|launch|close|kill|input|type|click|tap|swipe|screenshot|wait|back"
    r"))",
    re.I,
)
_INPUT_TEXT_RE = re.compile(
    r"(?:输入|填写|填入)\s*(?:手机号|手机|电话|号码|验证码)?\s*(\d{4,15})",
    re.I,
)
_NUMBERED_STEP_RE = re.compile(r"(?:^|\s)\d+[.、)\）]\s*")


def _normalize_segment(segment: str) -> str:
    seg = (segment or "").strip()
    seg = re.sub(r"^(?:再|然后|接着|并|并且|接下来)\s*", "", seg, flags=re.I).strip()
    return seg


_ORPHAN_TOGGLE_SEG_RE = re.compile(r"^(?:勾选框|框勾选框|选框)$")
_CLICK_ORPHAN_SEG_RE = re.compile(r"^(?:点击|点一下|tap|click)$", re.I)


def _coalesce_split_segments(parts: List[str]) -> List[str]:
    """合并被误拆的「勾选框」后缀，避免「勾选底部协议」+「勾选框」两步。"""
    out: List[str] = []
    for seg in parts:
        s = (seg or "").strip()
        if not s:
            continue
        if out and _CLICK_ORPHAN_SEG_RE.match(out[-1]) and not _CLICK_ORPHAN_SEG_RE.match(s):
            out[-1] = f"{out[-1]}{s}"
            continue
        if out and _ORPHAN_TOGGLE_SEG_RE.match(s):
            out[-1] = f"{out[-1]}{s}"
            continue
        if out and s.startswith("勾选框") and len(s) <= 8:
            out[-1] = f"{out[-1]}{s}"
            continue
        out.append(s)
    return out


def _split_commands(text: str) -> List[str]:
    """将一条用户输入拆成多个可独立规划的子指令。"""
    raw = (text or "").strip()
    if not raw:
        return []

    protected: Dict[str, str] = {}

    def _protect(match: re.Match) -> str:
        key = f"__MO_{len(protected)}__"
        protected[key] = match.group(0)
        return key

    shielded = _PACKAGE_RE.sub(_protect, raw)
    shielded = re.sub(r"\d{2,4}\s*[,，]\s*\d{2,4}", _protect, shielded)

    parts: List[str] = []
    if _NUMBERED_STEP_RE.search(shielded):
        chunks = _NUMBERED_STEP_RE.split(shielded)
        parts = [c.strip() for c in chunks if c and c.strip()]
    else:
        chunks = _SPLIT_DELIM_RE.split(shielded)
        parts = [c.strip() for c in chunks if c and c.strip()]

    if len(parts) <= 1:
        parts = [shielded]

    expanded: List[str] = []
    for part in parts:
        subs = [s.strip() for s in _VERB_BOUNDARY_RE.split(part) if s and s.strip()]
        expanded.extend(subs if subs else [part])

    restored: List[str] = []
    seen = set()
    for part in expanded:
        seg = part
        for key, val in protected.items():
            seg = seg.replace(key, val)
        seg = _normalize_segment(seg)
        if seg and seg not in seen:
            seen.add(seg)
            restored.append(seg)
    merged = _coalesce_split_segments(restored)
    return merged or [raw]


def _inject_step_waits(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """启动应用后若紧跟点击/滑动，自动插入短暂等待。"""
    if not steps:
        return steps
    out: List[Dict[str, Any]] = []
    for i, step in enumerate(steps):
        out.append(step)
        if step.get("kind") != "open_app" or i + 1 >= len(steps):
            continue
        nxt = steps[i + 1]
        if nxt.get("kind") not in ("click", "swipe"):
            continue
        if out and out[-1].get("kind") == "ability" and out[-1].get("nodeCode") == "cfs/sleep":
            continue
        out.append({
            "kind": "ability",
            "nodeCode": "cfs/sleep",
            "platform": "common",
            "data": {"seconds": 2},
            "summary": "等待应用就绪 2 秒",
        })
    return out


def _plan_segment(
    raw: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
) -> Dict[str, Any]:
    """单条子指令 → steps + reply_parts + errors。"""
    segment = _normalize_segment(raw)
    if not segment:
        return {"steps": [], "reply_parts": [], "errors": []}

    try:
        from server.services.copilot_semantic import semantic_split_segment

        expanded = semantic_split_segment(segment, sn=sn, context=context)
        if expanded is not None:
            if len(expanded) == 1 and _normalize_segment(expanded[0]) == segment:
                expanded = None
        if expanded is not None:
            steps: List[Dict[str, Any]] = []
            reply_parts: List[str] = []
            errors: List[str] = []
            if not expanded:
                errors.append(f"无法从当前界面解析 Tab 列表，已跳过口语步骤：{segment}")
                return {"steps": steps, "reply_parts": reply_parts, "errors": errors}
            for sub in expanded:
                sub_plan = _plan_segment(sub, sn=sn, context=context)
                steps.extend(sub_plan.get("steps") or [])
                reply_parts.extend(sub_plan.get("reply_parts") or [])
                errors.extend(sub_plan.get("errors") or [])
            if steps:
                reply_parts.insert(0, f"语义展开 {len(expanded)} 步")
            return {"steps": steps, "reply_parts": reply_parts, "errors": errors}
    except Exception as e:
        SLog.w(TAG, f"semantic split skipped: {e}")

    steps: List[Dict[str, Any]] = []
    reply_parts: List[str] = []
    errors: List[str] = []

    open_intent = bool(re.search(r"打开|启动|open|launch", segment, re.I))
    close_intent = bool(re.search(r"关闭|退出|close|kill|force[- ]?stop", segment, re.I))
    if open_intent and not close_intent:
        app_plan = _plan_app_action(segment, "open", sn=sn, context=context)
        if app_plan:
            if app_plan.get("error"):
                errors.append(app_plan["error"])
            else:
                steps.append(app_plan["step"])
                reply_parts.append(app_plan["reply"])
    elif close_intent and not open_intent:
        app_plan = _plan_app_action(segment, "close", sn=sn, context=context)
        if app_plan:
            if app_plan.get("error"):
                errors.append(app_plan["error"])
            else:
                steps.append(app_plan["step"])
                reply_parts.append(app_plan["reply"])

    input_m = _INPUT_TEXT_RE.search(segment)
    if input_m:
        value = input_m.group(1)
        field_hint = "手机号" if re.search(r"手机|电话|号码", segment) else ""
        steps.append({
            "kind": "input",
            "text": value,
            "field_hint": field_hint,
            "summary": f"输入{field_hint or '文本'} {value}",
        })
        reply_parts.append(steps[-1]["summary"])

    toggle_m = re.search(
        r"(勾选|勾上|选中|单选)\s*[「『\"']?([^」』\"'\n,，]+)[」』\"']?",
        segment,
        re.I,
    )
    if toggle_m:
        hint = toggle_m.group(2).strip() or "勾选框"
        if not re.search(r"勾选框|复选框|单选框|checkbox|radio", hint, re.I):
            hint = f"{hint}勾选框"
        steps.append({
            "kind": "click",
            "x": 0,
            "y": 0,
            "label": hint,
            "coords_explicit": False,
            "summary": f"勾选「{hint}」",
        })
        reply_parts.append(steps[-1]["summary"])
    elif re.search(r"勾选|勾上|勾上选|check", segment, re.I):
        hint = "勾选框"
        steps.append({
            "kind": "click",
            "x": 0,
            "y": 0,
            "label": hint,
            "coords_explicit": False,
            "summary": f"勾选「{hint}」",
        })
        reply_parts.append(steps[-1]["summary"])

    coord = re.search(r"(\d{2,4})\s*[,，]\s*(\d{2,4})", segment)
    label_m = re.search(r"[「『\"']([^」』\"']+)[」』\"']", segment) or re.search(
        r"点击\s*(.+?)(?:\s*[,，;；]|$)", segment
    )
    if re.search(r"点击|点一下|tap|click", segment, re.I):
        label = label_m.group(1).strip() if label_m else ""
        coords_explicit = bool(coord)
        if coord:
            x, y = int(coord.group(1)), int(coord.group(2))
        else:
            x, y = _DEFAULT_CLICK_XY
        if label or coords_explicit:
            steps.append({
                "kind": "click",
                "x": x,
                "y": y,
                "label": label,
                "coords_explicit": coords_explicit,
                "summary": f"点击「{label or (f'{x},{y}' if coords_explicit else '目标')}」",
            })
            reply_parts.append(steps[-1]["summary"])

    swipe_dir = None
    if re.search(r"上滑|向上滑|swipe\s*up", segment, re.I):
        swipe_dir = "up"
    elif re.search(r"下滑|向下滑|swipe\s*down", segment, re.I):
        swipe_dir = "down"
    elif re.search(r"左滑|向左", segment, re.I):
        swipe_dir = "left"
    elif re.search(r"右滑|向右", segment, re.I):
        swipe_dir = "right"
    elif re.search(r"滑动|滑一下|scroll|swipe", segment, re.I):
        swipe_dir = "up"
    if swipe_dir:
        steps.append({"kind": "swipe", "direction": swipe_dir, "summary": f"滑动 {swipe_dir}"})
        reply_parts.append(f"滑动 {swipe_dir}")

    if re.search(r"截图|截屏|screenshot", segment, re.I):
        steps.append({
            "kind": "ability",
            "nodeCode": "tools/screenshot",
            "platform": "mobile",
            "data": {"platform": "mobile"},
            "summary": "截图",
        })
        reply_parts.append("截图")

    if re.search(r"等待|wait|sleep", segment, re.I):
        sec_m = re.search(r"(\d+)\s*秒", segment)
        sec = int(sec_m.group(1)) if sec_m else 2
        steps.append({
            "kind": "ability",
            "nodeCode": "cfs/sleep",
            "platform": "common",
            "data": {"seconds": sec},
            "summary": f"等待 {sec} 秒",
        })
        reply_parts.append(f"等待 {sec}s")

    if re.search(r"返回|后退|back", segment, re.I) and not re.search(r"页面", segment):
        steps.append({"kind": "back", "summary": "返回（累计手势后执行）"})
        reply_parts.append("登记返回键")

    verify_text = re.sub(r"^验证预期[：:]\s*", "", segment).strip()
    if not steps and (
        segment.startswith("验证预期")
        or re.search(r"成功|失败|校验|校验通过|预期|确认", verify_text)
    ):
        frag = verify_text or segment
        steps.append(
            {
                "kind": "verify",
                "verify_text": frag,
                "summary": f"验证：{frag}",
            }
        )
        reply_parts.append(f"验证：{frag}")

    if not steps and not errors:
        errors.append(f"未识别子指令：{segment}")

    return {"steps": steps, "reply_parts": reply_parts, "errors": errors}


def plan_message(
    text: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
) -> Dict[str, Any]:
    """将用户输入拆解为步骤列表 + 回复文案。"""
    raw = (text or "").strip()
    if not raw:
        return {"reply": "请输入指令，例如：打开 造物相机 / 关闭 美团 / 点击 600,1200", "steps": [], "navigate": None}

    if raw.startswith("/"):
        cmd = raw[1:].strip().lower()
        nav = _nav_route(cmd)
        if nav:
            return {
                "reply": f"切换到：{nav['name']}",
                "steps": [],
                "navigate": nav,
            }

    nav = _nav_route(raw)
    if nav and len(raw) < 24 and len(_split_commands(raw)) <= 1:
        return {"reply": f"切换到页面：{nav['path']}", "steps": [], "navigate": nav}

    segments = _split_commands(raw)
    steps: List[Dict[str, Any]] = []
    reply_parts: List[str] = []
    segment_errors: List[str] = []
    knowledge_hints: List[str] = []
    page_hint = ""

    ctx = context or {}
    app_id = ctx.get("app_id") or ctx.get("appId")
    plat = (ctx.get("platform") or "android").lower()
    try:
        from server.services.system_settings_service import match_testing_knowledge

        for item in match_testing_knowledge(raw, app_id=app_id, limit=3):
            title = (item.get("title") or "").strip()
            content = (item.get("content") or "").strip()
            if title and content:
                knowledge_hints.append(f"「{title}」: {content[:120]}")
    except Exception:
        pass

    skip_page_hint = False
    try:
        from server.services.regression_run_context import get_ctx

        skip_page_hint = bool(get_ctx() and get_ctx().get("run_id"))
    except Exception:
        pass

    if sn and app_id and not skip_page_hint:
        try:
            import builtins
            from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
            from server.services.page_context_service import (
                format_page_hint,
                get_engine_screen_snapshot,
                identify_for_app,
            )

            builtins.TARGET_DEVICE_SN = str(sn)
            engine, _ = bootstrap_mobile_engine(str(sn), plat)
            snap = get_engine_screen_snapshot(engine)
            page_ctx = identify_for_app(
                str(app_id), engine, frame_count=1, screen_text=snap.get("blob") or ""
            )
            page_hint = format_page_hint(page_ctx)
        except Exception as e:
            SLog.w(TAG, f"page context hint failed: {e}")

    for seg in segments:
        planned = _plan_segment(seg, sn=sn, context=context)
        steps.extend(planned.get("steps") or [])
        reply_parts.extend(planned.get("reply_parts") or [])
        segment_errors.extend(planned.get("errors") or [])

    steps = _inject_step_waits(steps)
    steps = _dedupe_consecutive_toggle_steps(steps)

    if not steps and segment_errors:
        return {
            "reply": "\n".join(segment_errors),
            "steps": [],
            "navigate": None,
            "sn": sn,
            "auto_run": False,
        }

    if not steps:
        return {
            "reply": (
                "未识别指令。可尝试：\n"
                "· 打开 造物相机，点击我的，上滑\n"
                "· 打开 造物相机 / 关闭 美团\n"
                "· 打开 com.xxx.app（也支持包名）\n"
                "· 点击 600,1200 或 点击「我的」\n"
                "· 上滑 / 左滑\n"
                "· 去应用列表 / 设备管理\n"
                "· / 查看快捷命令"
            ),
            "steps": [],
            "navigate": None,
        }

    if not sn and any(s.get("kind") in ("click", "swipe", "open_app", "close_app", "ability") for s in steps):
        reply_parts.append("（未选设备：请先在顶部选择在线手机）")

    reply = " → ".join(reply_parts) if reply_parts else "好的"
    if len(steps) > 1:
        reply = f"共 {len(steps)} 步：{reply}"
    if segment_errors:
        reply += "\n⚠ " + "；".join(segment_errors)
    if page_hint:
        reply += f"\n📍 {page_hint}"

    return {
        "reply": reply,
        "display_reply": " → ".join(reply_parts) if reply_parts else reply,
        "steps": steps,
        "navigate": None,
        "sn": sn,
        "auto_run": True,
        "knowledge_hints": knowledge_hints,
        "page_hint": page_hint,
        "segment_errors": segment_errors,
        "plan_complete": len(segment_errors) == 0,
    }


def _attach_step_page_context(
    out: Dict[str, Any],
    *,
    sn: str,
    platform: str,
    app_id: str,
    wait_ms: int = 600,
    run_id: str = "",
) -> None:
    """执行后识别当前页，写入步骤结果供回放/断言参考。"""
    if not sn or not app_id:
        return
    if out.get("kind") not in ("click", "open_app", "close_app", "swipe"):
        return
    try:
        from server.services.regression_run_context import get_ctx

        if get_ctx() and get_ctx().get("run_id"):
            return
    except Exception:
        pass
    try:
        import builtins
        from script.sleep import mSleep
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
        from server.services.page_context_service import (
            get_engine_screen_snapshot,
            identify_for_app,
        )

        if wait_ms > 0:
            mSleep(min(wait_ms, 250) / 1000.0)

        builtins.TARGET_DEVICE_SN = str(sn)
        engine, (w, h) = bootstrap_mobile_engine(str(sn), platform)
        blob = get_engine_screen_snapshot(engine).get("blob") or ""
        from server.services.page_context_service import enrich_page_context_screenshot

        pc = identify_for_app(
            str(app_id), engine, frame_count=1, screen_text=blob
        )
        pc = enrich_page_context_screenshot(
            pc,
            sn=str(sn),
            platform=platform,
            run_id=str(run_id or ""),
            tag=f"step_page_{out.get('index', 0)}",
        )
        out["current_page"] = pc.get("label")
        out["current_page_score"] = pc.get("score")
        out["current_page_matched"] = pc.get("matched")
        out["current_page_id"] = pc.get("node_id")
        out["page_context"] = pc
    except Exception as e:
        SLog.w(TAG, f"step page context failed: {e}")


def execute_steps(
    steps: List[Dict[str, Any]],
    *,
    sn: Optional[str] = None,
    platform: str = "android",
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    run_id: Optional[str] = None,
    capture_screenshots: bool = True,
    app_id: Optional[str] = None,
    skip_overlay_clear: bool = False,
    enable_overlay_guard: bool = True,
    target_package: str = "",
    stop_on_failure: bool = True,
) -> List[Dict[str, Any]]:
    """逐步执行并返回每步结果（供前端展示判断循环）。"""
    import builtins

    runtime_icons: List[Dict[str, Any]] = list(icon_targets or [])
    learn_app_id = (app_id or "").strip()
    login_icon_order: Dict[str, int] = {}
    if learn_app_id:
        try:
            from server.core.database import SessionLocal
            from server.models.project import App
            from server.services.app_automation_service import get_login_icon_order

            session = SessionLocal()
            try:
                app = session.query(App).filter(App.id == learn_app_id).first()
                if app:
                    login_icon_order = get_login_icon_order(app)
            finally:
                session.close()
        except Exception as e:
            SLog.w(TAG, f"load login_icon_order failed: {e}")

    if sn:
        builtins.TARGET_DEVICE_SN = str(sn)
        try:
            from driver.agent.Memory import memory_manager
            memory_manager.short_term.set_global("run_device_sn", str(sn))
            memory_manager.short_term.set_global("platform", platform)
        except Exception:
            pass

    try:
        from server.services.regression_run_context import get_ctx

        if run_id and sn and not get_ctx():
            from server.services.regression_run_context import begin_run

            begin_run(
                run_id=str(run_id),
                sn=str(sn),
                platform=platform,
                capture_screenshots=capture_screenshots,
            )
    except Exception:
        pass

    results: List[Dict[str, Any]] = []
    guard_planned_all: List[Dict[str, Any]] = []
    pkg_guard = (target_package or "").strip()
    use_reactive_guard = bool(enable_overlay_guard)
    skip_click_overlay_dismiss = bool(skip_overlay_clear or use_reactive_guard)

    for i, step in enumerate(steps or []):
        t0 = time.time()
        kind = step.get("kind", "")
        summary = step.get("summary", kind)
        SLog.i(TAG, f"Step {i} start: {summary}")

        if sn and pkg_guard and kind not in ("open_app", "close_app", "verify", "ability"):
            try:
                from server.services.app_automation_service import guard_test_app_foreground

                fg = guard_test_app_foreground(sn, pkg_guard, platform)
                if not fg.get("ok"):
                    out_guard: Dict[str, Any] = {
                        "index": i,
                        "kind": kind,
                        "summary": summary,
                        "ok": False,
                        "msg": fg.get("msg") or "被测应用不在前台",
                        "method": "foreground_guard",
                        "started_at": datetime.fromtimestamp(t0).isoformat(
                            timespec="milliseconds"
                        ),
                        "duration_ms": 0,
                    }
                    results.append(out_guard)
                    SLog.w(TAG, f"Step {i} aborted: {out_guard['msg']}")
                    if stop_on_failure:
                        break
                    continue
            except Exception as e:
                SLog.w(TAG, f"foreground guard failed: {e}")
        out: Dict[str, Any] = {
            "index": i,
            "kind": kind,
            "summary": summary,
            "ok": False,
            "msg": "",
            "started_at": datetime.fromtimestamp(t0).isoformat(timespec="milliseconds"),
        }

        if sn:
            try:
                from driver.agent.Crawl.device_bootstrap import ensure_adb_device_online

                ensure_adb_device_online(str(sn), platform)
            except Exception as e:
                from driver.agent.Crawl.device_bootstrap import DeviceOfflineError

                if isinstance(e, DeviceOfflineError):
                    out["msg"] = str(e)
                    out["method"] = "device_offline"
                    results.append(out)
                    break
                raise

        if kind in ("open_app", "close_app"):
            if not sn:
                out["msg"] = "未选择设备"
            else:
                pkg = (
                    step.get("package")
                    or (step.get("data") or {}).get("target_mobile")
                    or ""
                ).strip()
                if kind == "open_app":
                    from server.services.app_automation_service import ensure_app_foreground

                    r = ensure_app_foreground(sn, pkg, platform)
                else:
                    r = _run_mobile_stop_app(sn, pkg, platform)
                out.update(r)
                if r.get("ok") and step.get("resolve_source"):
                    out["msg"] = (
                        f"{out.get('msg', '')} "
                        f"[{step.get('app_name', '')} → {step.get('package') or pkg} "
                        f"via {step.get('resolve_source')}]"
                    ).strip()

        elif kind == "click":
            if not sn:
                out["msg"] = "未选择设备"
            else:
                from server.services.regression_run_context import mark_step

                guard_round_i = 0
                max_guard_rounds = 3
                click_attempt = 0
                r: Dict[str, Any] = {"ok": False, "msg": ""}

                while True:
                    mark_step()
                    SLog.i(
                        TAG,
                        f"Step {i} click attempt={click_attempt} label={step.get('label')!r} "
                        f"coords=({step.get('x')},{step.get('y')})",
                    )
                    r = _run_mobile_click(
                        sn,
                        int(step.get("x", 0)),
                        int(step.get("y", 0)),
                        label=step.get("label", ""),
                        platform=platform,
                        coords_explicit=bool(step.get("coords_explicit")),
                        icon_targets=runtime_icons,
                        exact_label=bool(step.get("exact_label")),
                        skip_label_lookup=bool(step.get("skip_label_lookup")),
                        consent_dismiss=bool(step.get("consent_dismiss")),
                        login_icon_order=login_icon_order or None,
                        skip_overlay_clear=skip_click_overlay_dismiss,
                    )
                    if r.get("ok"):
                        out.update(r)
                        break

                    if not use_reactive_guard:
                        out.update(r)
                        break

                    try:
                        import builtins
                        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
                        from server.services.overlay_guard_service import (
                            apply_reactive_guard_round,
                            is_screen_blocked,
                        )

                        builtins.TARGET_DEVICE_SN = str(sn)
                        engine_g, (gw, gh) = bootstrap_mobile_engine(str(sn), platform)
                        if not is_screen_blocked(engine_g):
                            out.update(r)
                            break

                        SLog.i(
                            TAG,
                            f"Step {i}: reactive guard round={guard_round_i} "
                            f"after click miss {summary!r}",
                        )
                        try:
                            from server.services.regression_run_context import (
                                capture_trace_frame,
                                stamp_run_timing,
                            )

                            attempt_out = {
                                "index": i,
                                "kind": kind,
                                "summary": summary,
                                "ok": False,
                                "msg": r.get("msg") or "点击未命中",
                                "method": r.get("method") or "",
                                "phase": "plan_attempt",
                                "click_attempt": click_attempt,
                                "locate_debug": r.get("locate_debug"),
                                "screen_size": r.get("screen_size"),
                                "started_at": datetime.fromtimestamp(t0).isoformat(
                                    timespec="milliseconds"
                                ),
                                "duration_ms": int((time.time() - t0) * 1000),
                            }
                            try:
                                miss_shot = capture_trace_frame(
                                    f"plan_attempt_{i}_{click_attempt}",
                                    settle_ms=80,
                                )
                                if miss_shot:
                                    attempt_out["screenshot_before"] = miss_shot
                                    attempt_out["screenshot_after"] = miss_shot
                            except Exception:
                                pass
                            stamp_run_timing(attempt_out)
                            results.append(attempt_out)
                        except Exception:
                            pass
                        reactive = apply_reactive_guard_round(
                            engine_g,
                            gw,
                            gh,
                            before_step_index=i,
                            round_i=guard_round_i,
                            icon_targets=runtime_icons,
                            app_id=str(learn_app_id or ""),
                            step_summary=summary,
                            sn=str(sn),
                            platform=platform,
                        )
                        if reactive.get("planned_steps"):
                            guard_planned_all.extend(reactive["planned_steps"])
                        for gr in reactive.get("step_rows") or []:
                            gr["duration_ms"] = gr.get("duration_ms") or 0
                            results.append(gr)

                        if not reactive.get("attempted"):
                            out.update(r)
                            break
                        if not reactive.get("can_retry_click"):
                            last_action = (reactive.get("one") or {}).get("action") or {}
                            fail_out = {
                                "index": i,
                                "kind": kind,
                                "summary": summary,
                                "ok": False,
                                "msg": reactive.get("msg") or "阻塞弹窗守卫失败",
                                "method": "overlay_guard",
                                "started_at": datetime.fromtimestamp(t0).isoformat(
                                    timespec="milliseconds"
                                ),
                                "duration_ms": int((time.time() - t0) * 1000),
                                "overlay_guard": reactive,
                                "locate_debug": last_action.get("locate_debug"),
                            }
                            try:
                                from server.services.regression_run_context import stamp_run_timing

                                stamp_run_timing(fail_out)
                            except Exception:
                                pass
                            results.append(fail_out)
                            out.update(fail_out)
                            break

                        guard_round_i += 1
                        click_attempt += 1
                        if guard_round_i >= max_guard_rounds:
                            out.update(r)
                            break
                        continue
                    except Exception as e:
                        SLog.w(TAG, f"reactive guard after click miss failed: {e}")
                        out.update(r)
                        break

        elif kind == "swipe":
            if not sn:
                out["msg"] = "未选择设备"
            else:
                from server.services.regression_run_context import mark_step

                mark_step()
                r = _run_mobile_swipe(sn, step.get("direction", "up"), platform)
                out.update(r)

        elif kind == "input":
            if not sn:
                out["msg"] = "未选择设备"
            else:
                from server.services.regression_run_context import mark_step

                mark_step()
                SLog.i(
                    TAG,
                    f"Step {i} input begin text={step.get('text')!r} "
                    f"field={step.get('field_hint')!r}",
                )
                r = _run_mobile_input(
                    sn,
                    step.get("text") or "",
                    field_hint=step.get("field_hint") or "",
                    platform=platform,
                )
                out.update(r)

        elif kind == "back":
            if not sn:
                out["msg"] = "未选择设备"
            else:
                from server.services.regression_run_context import mark_step

                mark_step()
                r = _run_mobile_back(
                    sn,
                    platform,
                    immediate=bool(step.get("immediate", False)),
                )
                out.update(r)

        elif kind == "system_permission":
            if not sn:
                out["msg"] = "未选择设备"
            else:
                from server.services.page_navigation_service import (
                    ensure_system_permissions_cleared,
                )

                r = ensure_system_permissions_cleared(sn, platform, run_id=run_id or "")
                out.update(
                    {
                        "ok": bool(r.get("ok")),
                        "msg": r.get("reason") or "系统权限弹窗处理",
                        "gestures": r.get("gestures") or [],
                    }
                )

        elif kind == "verify":
            out.update(
                {
                    "ok": True,
                    "msg": step.get("verify_text") or summary,
                    "verify_only": True,
                }
            )

        elif kind == "ability":
            payload = _task_payload(
                step.get("nodeCode", "tools/screenshot"),
                platform=step.get("platform", "mobile"),
                data=step.get("data", {}),
            )
            r = _execute_ability(payload)
            out.update(r)

        else:
            out["msg"] = f"未知步骤类型 {kind}"

        out["duration_ms"] = int((time.time() - t0) * 1000)
        out["started_at"] = datetime.fromtimestamp(t0).isoformat(timespec="milliseconds")

        try:
            from server.services.regression_run_context import stamp_run_timing

            stamp_run_timing(out)
        except Exception:
            pass

        try:
            from server.services.regression_run_context import take_gestures_since_watermark

            step_gestures = take_gestures_since_watermark()
            returned_gestures = out.get("gestures") or []
            merged: List[Dict[str, Any]] = []
            seen_ids = set()
            for g in (step_gestures or []) + returned_gestures:
                gid = g.get("gesture_id") or ""
                dedupe_key = gid or f"{g.get('x')}-{g.get('y')}-{g.get('started_at')}-{g.get('summary')}"
                if dedupe_key in seen_ids:
                    continue
                seen_ids.add(dedupe_key)
                merged.append(g)
            if merged:
                out["gestures"] = merged
                out["screenshot_before"] = merged[0].get("screenshot_before") or ""
                out["screenshot_after"] = merged[-1].get("screenshot_after") or ""
        except Exception:
            pass

        if not out.get("screenshot_after") and capture_screenshots and sn and run_id:
            try:
                from server.services.regression_capture import capture_device_screenshot

                fallback_settle = 350 if step.get("consent_dismiss") else 450
                out["screenshot_after"] = capture_device_screenshot(
                    sn,
                    platform,
                    run_id=run_id,
                    tag=f"s{i}_{kind or 'step'}",
                    settle_ms=fallback_settle,
                )
            except Exception:
                out["screenshot_after"] = ""

        if learn_app_id and sn:
            _attach_step_page_context(
                out,
                sn=str(sn),
                platform=platform,
                app_id=learn_app_id,
                run_id=str(run_id or ""),
            )

        if learn_app_id and kind == "click" and out.get("ok"):
            try:
                from server.services import icon_target_service as its

                learned = its.auto_learn_from_click(learn_app_id, out)
                if learned:
                    out["icon_auto_learned"] = True
                    out["suggest_icon_library"] = True
                    runtime_icons.append(its.learned_icon_for_copilot(learned))
                    note = f"已自动入库图标库「{learned.get('name')}」"
                    out["msg"] = f"{out.get('msg') or ''} [{note}]".strip()
                    SLog.i(TAG, note)
            except Exception as e:
                SLog.w(TAG, f"auto learn icon failed: {e}")

        results.append(out)
        SLog.i(TAG, f"Step {i}: {summary} -> ok={out.get('ok')} {out.get('msg')} ({out.get('duration_ms')}ms)")

        if stop_on_failure and not out.get("ok") and kind not in ("verify",):
            SLog.w(TAG, f"Step {i} failed — stop remaining steps")
            break

    if guard_planned_all:
        try:
            from server.services.regression_run_context import get_ctx

            ctx = get_ctx()
            if ctx is not None:
                ctx["guard_planned_steps"] = guard_planned_all
        except Exception:
            pass

    return results
