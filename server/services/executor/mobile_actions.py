# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""移动端手势/输入/点击执行（坐标注入 + 本地定位分支）。"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from script.log import SLog

from server.services.executor.gesture_state import _gesture_tick
from server.services.executor.locate_debug import clear_locate_debug, _with_locate_debug

TAG = "CopilotExecutor"

def _run_mobile_back(sn: str, platform: str = "android", *, immediate: bool = True) -> Dict[str, Any]:
    return _run_mobile_key(sn, "back", platform=platform)


def _run_mobile_key(sn: str, key: str, platform: str = "android") -> Dict[str, Any]:
    key_name = (key or "").strip().lower()
    if key_name not in {"back", "home", "menu", "power"}:
        return {"ok": False, "msg": f"不支持的系统按键：{key}", "method": "press_key"}
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
        from script.sleep import mSleep

        builtins.TARGET_DEVICE_SN = sn
        engine, _ = bootstrap_mobile_engine(sn, platform)
        if hasattr(engine, "press_key"):
            engine.press_key(key_name)
            mSleep(0.6)
            SLog.i(TAG, f"Key audit key={key_name} sn={sn}")
            label = {"back": "返回", "home": "Home", "menu": "菜单", "power": "电源"}.get(key_name, key_name)
            return {"ok": True, "msg": f"已执行{label}键", "method": "press_key", "key": key_name}
        return {"ok": False, "msg": "引擎不支持系统按键", "method": "press_key"}
    except Exception as e:
        return {"ok": False, "msg": str(e), "method": "press_key"}


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


def _run_mobile_swipe_coords(
    sn: str,
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    *,
    duration_ms: int = 350,
    platform: str = "android",
) -> Dict[str, Any]:
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

        builtins.TARGET_DEVICE_SN = sn
        engine, (w, h) = bootstrap_mobile_engine(sn, platform)
        if min(start_x, start_y, end_x, end_y) <= 0:
            return {"ok": False, "msg": "AI 滑动缺少有效起止坐标", "method": "ai_coordinate"}
        if start_x > w or end_x > w or start_y > h or end_y > h:
            return {
                "ok": False,
                "msg": f"AI 滑动坐标超出屏幕范围：({start_x},{start_y})->({end_x},{end_y}) screen={w}x{h}",
                "method": "ai_coordinate",
                "screen_size": {"w": w, "h": h},
            }
        if not hasattr(engine, "swipe_norm"):
            return {"ok": False, "msg": "设备引擎不支持滑动", "method": "ai_coordinate"}
        engine.swipe_norm(
            start_x / float(w),
            start_y / float(h),
            end_x / float(w),
            end_y / float(h),
            max(0.1, duration_ms / 1000.0),
        )
        _gesture_tick()
        return {
            "ok": True,
            "msg": f"AI 坐标滑动 ({start_x},{start_y})->({end_x},{end_y})",
            "method": "ai_coordinate",
            "start_x": start_x,
            "start_y": start_y,
            "end_x": end_x,
            "end_y": end_y,
            "screen_size": {"w": w, "h": h},
        }
    except Exception as e:
        return {"ok": False, "msg": str(e), "method": "ai_coordinate"}


def _run_mobile_input(
    sn: str,
    text: str,
    *,
    field_hint: str = "",
    platform: str = "android",
    focus_rect: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """向当前页输入框填入文本（优先绑定上一步点击区域，再 u2 EditText，兜底 adb）。"""
    value = (text or "").strip()
    if not value:
        return {"ok": False, "msg": "输入内容为空", "method": "input"}
    gesture = None
    try:
        from server.services.shared.run_context.regression_run_context import finish_gesture, record_gesture

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

        hints = [field_hint, "手机号", "手机", "请输入", "验证码", "账号", "密码", "邮箱"]
        if field_hint:
            hints.extend([
                field_hint.replace("输入框", ""),
                f"{field_hint}输入框",
            ])
        hints = [h for h in hints if h]
        seen_hint = set()
        deduped_hints = []
        for h in hints:
            key = h.strip()
            if key and key not in seen_hint:
                seen_hint.add(key)
                deduped_hints.append(key)
        hints = deduped_hints
        typed = False
        method = "input"

        d = engine._ensure_u2() if hasattr(engine, "_ensure_u2") else None

        def _try_focus_rect_input() -> bool:
            if not d or not focus_rect:
                return False
            center = focus_rect.get("center") or []
            if not isinstance(center, (list, tuple)) or len(center) < 2:
                return False
            try:
                cx, cy = int(center[0]), int(center[1])
            except (TypeError, ValueError):
                return False
            if cy > int(screen_h * 0.72):
                return False
            try:
                d.click(cx, cy)
                time.sleep(0.35)
                focused = d(focused=True)
                if focused.exists(timeout=1.0):
                    cls = (focused.info or {}).get("className") or ""
                    if "Edit" in cls:
                        try:
                            focused.clear_text()
                        except Exception:
                            pass
                        focused.set_text(value)
                        return True
                for spec in (
                    {"className": "android.widget.EditText", "focused": True},
                    {"className": "android.widget.EditText"},
                ):
                    sel = d(**spec)
                    if not sel.exists(timeout=0.5):
                        continue
                    try:
                        sel.click()
                        time.sleep(0.2)
                        sel.clear_text()
                    except Exception:
                        pass
                    sel.set_text(value)
                    return True
            except Exception as e:
                SLog.w(TAG, f"focus_rect input failed: {e}")
            return False

        if _try_focus_rect_input():
            typed = True
            method = "u2_focus_input"

        if not typed and d:
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


def _run_mobile_input_coords(
    sn: str,
    x: int,
    y: int,
    text: str,
    *,
    label: str = "",
    platform: str = "android",
) -> Dict[str, Any]:
    value = (text or "").strip()
    if not value:
        return {"ok": False, "msg": "输入内容为空", "method": "ai_coordinate_input"}
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
        from script.sleep import mSleep

        builtins.TARGET_DEVICE_SN = sn
        engine, (screen_w, screen_h) = bootstrap_mobile_engine(sn, platform)
        if x <= 0 or y <= 0:
            return {"ok": False, "msg": "AI 输入缺少有效 x/y 坐标", "method": "ai_coordinate_input"}
        if x > screen_w or y > screen_h:
            return {
                "ok": False,
                "msg": f"AI 输入坐标超出屏幕范围：({x},{y}) screen={screen_w}x{screen_h}",
                "method": "ai_coordinate_input",
                "screen_size": {"w": screen_w, "h": screen_h},
            }
        if hasattr(engine, "ensure_screen_ready"):
            try:
                engine.ensure_screen_ready(node_sn=sn)
            except Exception:
                pass
        if hasattr(engine, "click"):
            try:
                clicked = engine.click(
                    None,
                    position=(x, y),
                    label=label or "AI input target",
                    skip_label_lookup=True,
                    locate_method="ai_coordinate_input",
                )
            except TypeError:
                clicked = engine.click(None, position=(x, y), label=label or "AI input target")
            if clicked is False:
                return {"ok": False, "msg": "AI 输入坐标点击失败", "method": "ai_coordinate_input"}
        mSleep(0.25)
        if hasattr(engine, "send_keys"):
            engine.send_keys(None, value)
            _gesture_tick()
            return {
                "ok": True,
                "msg": f"AI 坐标输入「{value}」",
                "method": "ai_coordinate_input",
                "kind": "input",
                "x": x,
                "y": y,
                "text": value,
                "screen_size": {"w": screen_w, "h": screen_h},
                "target_label": label,
            }
        return {"ok": False, "msg": "设备引擎不支持文本输入", "method": "ai_coordinate_input"}
    except Exception as e:
        return {"ok": False, "msg": str(e), "method": "ai_coordinate_input"}


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
    from server.services.copilot_service import (
        _classify_login_method_intent,
        _clip_search_params,
        _icon_names_match_label,
        _is_consent_action_label,
        _is_one_click_login_label,
        _is_probable_bottom_tab_query,
        _is_toggle_intent,
        _make_target_rect,
        _match_bottom_tab_label,
        _match_target_label,
        _resolve_click_target,
    )

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

        from server.services.shared.run_context.regression_run_context import is_ai_execution

        ai_mode = is_ai_execution()

        consent_label = _is_consent_action_label(label)
        if consent_label and "同意并继续" not in (label or "") and not ai_mode:
            try:
                from server.services.local.navigation.page_navigation_service import (
                    _overlay_dismiss_target_cleared,
                )

                if _overlay_dismiss_target_cleared(engine, label):
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

        if ai_mode:
            if x <= 0 or y <= 0:
                return {
                    "ok": False,
                    "msg": "AI 模式 click 必须返回有效 x/y 坐标，不能只返回 label。",
                    "method": "ai_coordinate",
                    "screen_size": {"w": screen_w, "h": screen_h},
                    "target_label": label,
                }
            if x > screen_w or y > screen_h:
                return {
                    "ok": False,
                    "msg": f"AI 坐标超出屏幕范围：({x},{y}) screen={screen_w}x{screen_h}",
                    "method": "ai_coordinate",
                    "screen_size": {"w": screen_w, "h": screen_h},
                    "target_label": label,
                }
            if hasattr(engine, "click"):
                try:
                    clicked = engine.click(
                        None,
                        position=(x, y),
                        label=label or f"AI({x},{y})",
                        skip_label_lookup=True,
                        locate_method="ai_coordinate",
                    )
                except TypeError:
                    clicked = engine.click(None, position=(x, y), label=label or f"AI({x},{y})")
                if clicked is False:
                    return {
                        "ok": False,
                        "msg": "AI 坐标点击触控注入失败",
                        "method": "ai_coordinate",
                        "x": x,
                        "y": y,
                        "screen_size": {"w": screen_w, "h": screen_h},
                        "target_label": label,
                    }
                _gesture_tick()
                SLog.i(TAG, f"AI coordinate click x={x} y={y} label={label!r} skip local locate")
                return {
                    "ok": True,
                    "msg": f"AI 坐标点击 ({x},{y})",
                    "method": "ai_coordinate",
                    "x": x,
                    "y": y,
                    "target_rect": _make_target_rect(x - 24, y - 24, 48, 48, label=label or f"({x},{y})"),
                    "screen_size": {"w": screen_w, "h": screen_h},
                    "target_label": label,
                }
            return {"ok": False, "msg": "引擎不支持点击", "method": "ai_coordinate"}

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
                from server.services.local.navigation.page_navigation_service import dismiss_blocking_on_engine

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
                tap_hint = (
                    "触控注入失败，请确认 ClawNode 无障碍已开启且设备 WebSocket 在线"
                    if str(sn).startswith("claw-")
                    else "触控注入失败，请检查 USB 调试(安全设置) 与无障碍 ATX"
                )
                return _with_locate_debug(
                    {
                        "ok": False,
                        "msg": f"{detail} — {tap_hint}",
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

                    from server.services.local.navigation.page_navigation_service import _login_checkbox_checked

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
