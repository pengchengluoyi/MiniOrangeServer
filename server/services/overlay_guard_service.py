# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
阻塞弹窗守卫（Overlay Guard）

业务 Plan 点击失败且屏被阻塞时，插入一轮「守卫 · {类型}」+ 单次处置 Tap，
然后重试业务 Plan。不做 Detect/Recheck 展示节点，Assert 仅内部判定。
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from script.log import SLog

TAG = "OverlayGuard"

OVERLAY_TYPE_TITLES = {
    "consent": "隐私同意弹窗",
    "login_confirm": "登录确认弹窗",
    "system_permission": "系统权限弹窗",
    "agreement": "协议全文页",
    "generic_overlay": "应用弹层",
    "screen_not_ready": "屏幕未就绪",
}

GUARDABLE_STEP_KINDS = frozenset(
    {"click", "swipe", "input", "open_app", "ability", "back"}
)


_SUB_INDEX = {"act": 1, "assert": 2}


def _guard_plan_index(
    before_step_index: int,
    iteration: int,
    sub: str = "act",
) -> int:
    """正索引：排在对应业务步骤之后；iteration 越大序号越大（consent → permission）。"""
    sub_i = _SUB_INDEX.get(sub, 1)
    return (before_step_index + 1) * 1000 + (iteration + 1) * 10 + sub_i


def is_guard_plan_index(plan_index: int) -> bool:
    return int(plan_index) >= 1000


def detect_blocking_overlay(engine) -> Optional[Dict[str, Any]]:
    """检测当前屏幕是否被阻塞弹窗占用。无阻塞返回 None。"""
    from server.services.page_navigation_service import (
        _engine_screen_size,
        _hierarchy_has_app_consent_modal,
        _screen_is_overlay,
        _screen_is_system_permission_dialog,
        _screen_is_user_agreement_page,
        get_blocking_screen_state,
        is_blocking_consent_screen,
        is_blocking_login_confirm_screen,
    )

    screen_w, screen_h = _engine_screen_size(engine) if engine is not None else (1080, 1920)

    if engine is not None and _hierarchy_has_app_consent_modal(engine, screen_w, screen_h):
        state = get_blocking_screen_state(engine, force=True)
        ocr = (state.get("ocr_text") or "").strip()
        return {
            "type": "consent",
            "blocked": True,
            "reason": "hierarchy_consent_pair",
            "ocr_snip": ocr[:200],
            "state": state,
        }

    state = get_blocking_screen_state(engine, force=True)
    ocr = (state.get("ocr_text") or "").strip()

    if state.get("screen_not_ready"):
        return {
            "type": "screen_not_ready",
            "blocked": True,
            "reason": state.get("reason") or "screen_not_ready",
            "ocr_snip": ocr[:200],
            "state": state,
        }

    if is_blocking_consent_screen(screen_state=state, screen_text=ocr, engine=engine):
        return {
            "type": "consent",
            "blocked": True,
            "reason": state.get("reason") or "consent",
            "ocr_snip": ocr[:200],
            "state": state,
        }

    if is_blocking_login_confirm_screen(screen_state=state, screen_text=ocr, engine=engine):
        return {
            "type": "login_confirm",
            "blocked": True,
            "reason": state.get("reason") or "login_confirm_sheet",
            "ocr_snip": ocr[:200],
            "state": state,
        }

    if _screen_is_system_permission_dialog(ocr, engine=engine):
        return {
            "type": "system_permission",
            "blocked": True,
            "reason": "system_permission",
            "ocr_snip": ocr[:200],
            "state": state,
        }

    if state.get("agreement") or _screen_is_user_agreement_page(ocr):
        return {
            "type": "agreement",
            "blocked": True,
            "reason": state.get("reason") or "agreement",
            "ocr_snip": ocr[:200],
            "state": state,
        }

    if _screen_is_overlay(ocr) and not state.get("reason") in (
        "login_home",
        "phone_login_or_sms",
    ):
        return {
            "type": "generic_overlay",
            "blocked": True,
            "reason": state.get("reason") or "overlay",
            "ocr_snip": ocr[:200],
            "state": state,
        }

    return None


def is_screen_blocked(engine) -> bool:
    return detect_blocking_overlay(engine) is not None


def blocked_overlay_message(engine) -> str:
    """阻塞屏说明文案，含弹窗类型（隐私同意 / 系统权限等）。"""
    ov = detect_blocking_overlay(engine)
    if not ov:
        return ""
    otype = (ov.get("type") or "").strip()
    title = OVERLAY_TYPE_TITLES.get(otype, "阻塞弹窗")
    return f"当前屏被{title}占用"


def assert_overlay_cleared(engine, overlay_type: str) -> Dict[str, Any]:
    """断言指定类型弹窗已消除（允许出现下一层阻塞弹窗）。"""
    from server.services.page_navigation_service import classify_blocking_screen

    after = detect_blocking_overlay(engine)
    cleared = after is None or after.get("type") != overlay_type
    screen_after = classify_blocking_screen(engine)
    return {
        "ok": cleared,
        "cleared": cleared,
        "overlay_type": overlay_type,
        "still_blocked": after,
        "screen_after": {
            "reason": screen_after.get("reason"),
            "consent": screen_after.get("consent"),
            "agreement": screen_after.get("agreement"),
        },
    }


def _plan_for_overlay(detect: Dict[str, Any]) -> Dict[str, Any]:
    """守卫 Plan 只描述当前屏类型，不预置「Tap · 同意」等固定点击文案。"""
    otype = detect.get("type") or ""
    title = OVERLAY_TYPE_TITLES.get(otype, otype)
    return {
        "kind": "overlay_guard",
        "overlay_type": otype,
        "summary": f"守卫 · {title}",
        "title": title,
    }


def _action_summary_for_overlay(otype: str, *, action_label: str = "") -> str:
    title = OVERLAY_TYPE_TITLES.get(otype, otype)
    if action_label:
        return f"处置 · {title}（{action_label}）"
    if otype == "agreement":
        return f"处置 · {title}（返回）"
    return f"处置 · {title}"


def _execute_guard_action(
    engine,
    screen_w: int,
    screen_h: int,
    detect: Dict[str, Any],
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    from script.sleep import mSleep

    otype = detect.get("type") or ""
    plan = _plan_for_overlay(detect)
    t0 = time.time()

    if otype == "screen_not_ready":
        ok = False
        if hasattr(engine, "ensure_screen_ready"):
            try:
                ok = bool(engine.ensure_screen_ready())
            except Exception as e:
                SLog.w(TAG, f"ensure_screen_ready failed: {e}")
        return {
            "ok": ok,
            "method": "screen_ready",
            "msg": "屏幕已点亮" if ok else "屏幕未就绪",
            "plan": plan,
            "summary": _action_summary_for_overlay(otype),
            "duration_ms": int((time.time() - t0) * 1000),
        }

    if otype == "agreement":
        ok = False
        try:
            if hasattr(engine, "back"):
                engine.back()
                ok = True
            elif hasattr(engine, "shell"):
                engine.shell("input keyevent 4")
                ok = True
        except Exception as e:
            SLog.w(TAG, f"agreement back failed: {e}")
        mSleep(0.6)
        return {
            "ok": ok,
            "method": "back",
            "msg": "从协议页返回" if ok else "返回失败",
            "plan": plan,
            "summary": _action_summary_for_overlay(otype),
            "duration_ms": int((time.time() - t0) * 1000),
        }

    if otype in ("consent", "login_confirm"):
        from server.services.page_navigation_service import tap_consent_agree_on_engine

        agree_label = "同意并继续" if otype == "login_confirm" else "同意"
        hit = tap_consent_agree_on_engine(
            engine,
            screen_w,
            screen_h,
            icon_targets=icon_targets,
            phase="overlay_guard",
            source="overlay_guard",
            exact_label=True,
            single_tap=True,
            agree_label=agree_label,
        )
        return {
            "ok": bool(hit.get("ok")),
            "method": hit.get("method") or "consent_agree",
            "msg": hit.get("msg")
            or (f"点击「{agree_label}」" if hit.get("ok") else "未找到可点击目标"),
            "plan": plan,
            "summary": _action_summary_for_overlay(
                otype, action_label=agree_label if hit.get("ok") else ""
            ),
            "x": hit.get("x"),
            "y": hit.get("y"),
            "gesture": hit.get("gesture"),
            "target_rect": hit.get("target_rect"),
            "locate_debug": hit.get("locate_debug"),
            "duration_ms": int((time.time() - t0) * 1000),
        }

    if otype == "system_permission":
        from server.services.page_navigation_service import (
            _hierarchy_has_app_consent_modal,
            tap_consent_agree_on_engine,
            tap_system_permission_on_engine,
        )

        hit = tap_system_permission_on_engine(
            engine,
            screen_w,
            screen_h,
            icon_targets=icon_targets,
            phase="overlay_guard",
            source="overlay_guard",
        )
        guard_type = otype
        if not hit.get("ok") and _hierarchy_has_app_consent_modal(engine, screen_w, screen_h):
            SLog.w(
                TAG,
                "system_permission guard missed; hierarchy shows consent pair — fallback",
            )
            plan = _plan_for_overlay({"type": "consent"})
            guard_type = "consent"
            hit = tap_consent_agree_on_engine(
                engine,
                screen_w,
                screen_h,
                icon_targets=icon_targets,
                phase="overlay_guard",
                source="overlay_guard",
                exact_label=True,
                single_tap=True,
                agree_label="同意",
            )
        act_label = ""
        if hit.get("ok") and hit.get("msg"):
            for prefer in ("仅在使用中允许", "始终允许", "允许", "同意", "同意并继续"):
                if prefer in str(hit.get("msg")):
                    act_label = prefer
                    break
        return {
            "ok": bool(hit.get("ok")),
            "method": hit.get("method") or "permission_allow",
            "msg": hit.get("msg") or "系统权限弹窗未命中允许按钮",
            "plan": plan,
            "summary": _action_summary_for_overlay(
                guard_type,
                action_label=act_label
                or ("允许" if guard_type == "system_permission" and hit.get("ok") else "")
                or ("同意" if guard_type == "consent" and hit.get("ok") else ""),
            ),
            "x": hit.get("x"),
            "y": hit.get("y"),
            "gesture": hit.get("gesture"),
            "target_rect": hit.get("target_rect"),
            "locate_debug": hit.get("locate_debug"),
            "duration_ms": int((time.time() - t0) * 1000),
        }

    return {
        "ok": False,
        "method": "unknown",
        "msg": f"未支持的阻塞类型: {otype}",
        "plan": plan,
        "summary": _action_summary_for_overlay(otype),
        "duration_ms": int((time.time() - t0) * 1000),
    }


def run_one_guard_round(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    detect: Optional[Dict[str, Any]] = None,
    round_i: int = 0,
    before_step_index: int = 0,
) -> Dict[str, Any]:
    """单次守卫：识别当前阻塞屏 → 处置一次 → Assert。由调用方在仍阻塞时再次调用。"""
    from script.sleep import mSleep
    from server.services.shared.run_context.regression_run_context import capture_trace_frame

    from server.services.shared.run_context.regression_run_context import run_elapsed_ms

    detect = detect or detect_blocking_overlay(engine)
    if not detect or not detect.get("blocked"):
        return {
            "attempted": False,
            "ok": True,
            "cleared": True,
            "msg": "无阻塞弹窗",
        }

    otype = detect.get("type") or ""
    type_title = OVERLAY_TYPE_TITLES.get(otype, otype)

    action_ms = run_elapsed_ms()
    action = _execute_guard_action(
        engine,
        screen_w,
        screen_h,
        detect,
        icon_targets=icon_targets,
    )
    mSleep(0.25)
    assert_ms = run_elapsed_ms()
    assert_result = assert_overlay_cleared(engine, otype)
    cleared = bool(assert_result.get("cleared"))
    assert_shot = capture_trace_frame(
        f"guard_s{before_step_index}_r{round_i}_after",
        settle_ms=120,
    )

    SLog.i(
        TAG,
        f"guard round={round_i} type={otype} action_ok={action.get('ok')} "
        f"cleared={cleared} reason={detect.get('reason')}",
    )

    return {
        "attempted": True,
        "ok": bool(action.get("ok")),
        "cleared": cleared,
        "detect": detect,
        "plan": action.get("plan") or _plan_for_overlay(detect),
        "action": action,
        "assert": assert_result,
        "type_title": type_title,
        "assert_screenshot": assert_shot,
        "msg": action.get("msg") or "",
        "action_elapsed_ms": action_ms,
        "assert_elapsed_ms": assert_ms,
    }


def run_guard_loop_before_step(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    before_step_index: int = 0,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    max_rounds: int = 6,
    app_id: str = "",
    step_summary: str = "",
    sn: str = "",
    platform: str = "android",
) -> Dict[str, Any]:
    """按当前屏逐步守卫：每轮只 detect → 处置一次 → assert，再判断是否仍阻塞。"""
    iterations: List[Dict[str, Any]] = []
    guard_round = 0

    while guard_round < max_rounds and is_screen_blocked(engine):
        one = run_one_guard_round(
            engine,
            screen_w,
            screen_h,
            icon_targets=icon_targets,
            round_i=guard_round,
            before_step_index=before_step_index,
        )
        if not one.get("attempted"):
            break

        iterations.append(
            {
                "round": guard_round,
                "detect": one.get("detect"),
                "plan": one.get("plan"),
                "action": one.get("action"),
                "assert": one.get("assert"),
                "type_title": one.get("type_title"),
                "assert_screenshot": one.get("assert_screenshot"),
                "action_elapsed_ms": one.get("action_elapsed_ms"),
                "assert_elapsed_ms": one.get("assert_elapsed_ms"),
            }
        )

        if not one.get("ok"):
            return {
                "attempted": True,
                "ok": False,
                "iterations": iterations,
                "msg": one.get("msg") or f"{one.get('type_title')}处置失败",
                "rounds": len(iterations),
            }

        guard_round += 1
        if not is_screen_blocked(engine):
            break

    if iterations and is_screen_blocked(engine):
        return {
            "attempted": True,
            "ok": False,
            "iterations": iterations,
            "msg": "阻塞弹窗未在限定轮次内消除",
            "rounds": len(iterations),
        }

    guard_out: Dict[str, Any] = {
        "attempted": bool(iterations),
        "ok": True,
        "iterations": iterations,
        "msg": "无阻塞弹窗" if not iterations else "阻塞弹窗已清除",
        "rounds": len(iterations),
    }

    return guard_out


def run_overlay_guard_until_clear(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    max_rounds: int = 6,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """循环调用 run_one_guard_round，直到无阻塞或失败。"""
    iterations: List[Dict[str, Any]] = []
    for round_i in range(max_rounds):
        if not is_screen_blocked(engine):
            return {
                "attempted": bool(iterations),
                "ok": True,
                "iterations": iterations,
                "msg": "无阻塞弹窗" if not iterations else "阻塞弹窗已清除",
                "rounds": len(iterations),
            }

        one = run_one_guard_round(
            engine,
            screen_w,
            screen_h,
            icon_targets=icon_targets,
            round_i=round_i,
        )
        if not one.get("attempted"):
            return {
                "attempted": bool(iterations),
                "ok": True,
                "iterations": iterations,
                "msg": "无阻塞弹窗" if not iterations else "阻塞弹窗已清除",
                "rounds": len(iterations),
            }

        iteration = {
            "round": round_i,
            "detect": one.get("detect"),
            "plan": one.get("plan"),
            "action": one.get("action"),
            "assert": one.get("assert"),
            "type_title": one.get("type_title"),
            "assert_screenshot": one.get("assert_screenshot"),
        }
        iterations.append(iteration)

        if not one.get("ok"):
            return {
                "attempted": True,
                "ok": False,
                "iterations": iterations,
                "msg": one.get("msg") or f"{one.get('type_title')}处置失败",
                "rounds": len(iterations),
            }

        if not one.get("cleared"):
            SLog.w(
                TAG,
                f"guard round={round_i} action ok but assert not cleared "
                f"type={one.get('detect', {}).get('type')}",
            )

    return {
        "attempted": True,
        "ok": False,
        "iterations": iterations,
        "msg": "阻塞弹窗未在限定轮次内消除",
        "rounds": len(iterations),
    }


def run_overlay_guard_on_device(
    sn: str,
    platform: str = "android",
    *,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    max_rounds: int = 6,
) -> Dict[str, Any]:
    import builtins
    from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

    builtins.TARGET_DEVICE_SN = sn
    engine, (screen_w, screen_h) = bootstrap_mobile_engine(sn, platform)
    return run_overlay_guard_until_clear(
        engine,
        screen_w,
        screen_h,
        max_rounds=max_rounds,
        icon_targets=icon_targets,
    )


def apply_reactive_guard_round(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    before_step_index: int,
    round_i: int,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    app_id: str = "",
    step_summary: str = "",
    sn: str = "",
    platform: str = "android",
) -> Dict[str, Any]:
    """
    反应式守卫：业务点击失败后，若屏仍被阻塞则只执行一轮处置 Tap。
    返回 step_rows / planned_steps / can_retry_click（处置成功即可重试业务 Plan）。
    """
    if not is_screen_blocked(engine):
        return {
            "attempted": False,
            "ok": True,
            "can_retry_click": False,
            "step_rows": [],
            "planned_steps": [],
        }

    try:
        from server.services.page_navigation_service import is_planned_overlay_step_label

        if is_planned_overlay_step_label(step_summary):
            return {
                "attempted": False,
                "ok": True,
                "can_retry_click": False,
                "step_rows": [],
                "planned_steps": [],
                "msg": "业务步骤自行处理同意/不同意，跳过守卫",
            }
    except Exception:
        pass

    one = run_one_guard_round(
        engine,
        screen_w,
        screen_h,
        icon_targets=icon_targets,
        round_i=round_i,
        before_step_index=before_step_index,
    )
    if not one.get("attempted"):
        return {
            "attempted": False,
            "ok": True,
            "can_retry_click": False,
            "step_rows": [],
            "planned_steps": [],
        }

    step_rows = guard_round_to_step_results(
        one, before_step_index=before_step_index, round_i=round_i
    )

    otype = (one.get("detect") or {}).get("type") or ""
    type_title = one.get("type_title") or OVERLAY_TYPE_TITLES.get(otype, "阻塞弹窗")
    planned_steps = guard_planned_steps_for_round(
        before_step_index=before_step_index,
        round_i=round_i,
        type_title=type_title,
        otype=otype,
    )

    guard_ok = bool(one.get("ok"))
    can_retry = guard_ok

    return {
        "attempted": True,
        "ok": guard_ok,
        "can_retry_click": can_retry,
        "one": one,
        "step_rows": step_rows,
        "planned_steps": planned_steps,
        "msg": one.get("msg") or "",
    }


def guard_planned_steps_for_round(
    *,
    before_step_index: int,
    round_i: int,
    type_title: str,
    otype: str,
) -> List[Dict[str, Any]]:
    """每轮守卫仅一个 Plan 节点 + 单次处置 Tap。"""
    pi = _guard_plan_index(before_step_index, round_i, "act")
    return [
        {
            "type": "planned_step",
            "index": pi,
            "kind": "overlay_guard",
            "summary": f"守卫 · {type_title}",
            "detail": {"before_step": before_step_index, "overlay_type": otype, "round": round_i},
        }
    ]


def guard_round_to_step_results(
    one: Dict[str, Any],
    *,
    before_step_index: int,
    round_i: int,
) -> List[Dict[str, Any]]:
    """将单次 run_one_guard_round 结果转为 execute 行（仅处置 Tap）。"""
    from server.services.shared.run_context.regression_run_context import apply_run_timing

    if not one.get("attempted"):
        return []

    pi = _guard_plan_index(before_step_index, round_i, "act")
    detect = one.get("detect") or {}
    action = one.get("action") or {}
    gesture = action.get("gesture")
    act_summary = action.get("summary") or _action_summary_for_overlay(
        detect.get("type") or ""
    )
    act_t0 = datetime.now().isoformat(timespec="milliseconds")
    tap_row = apply_run_timing(
        {
            "index": pi,
            "kind": "click" if action.get("ok") else "overlay_guard",
            "summary": act_summary,
            "ok": bool(action.get("ok")),
            "msg": action.get("msg") or "",
            "method": action.get("method") or "",
            "x": action.get("x"),
            "y": action.get("y"),
            "target_rect": action.get("target_rect"),
            "locate_debug": action.get("locate_debug"),
            "gestures": [gesture] if gesture else [],
            "screenshot_after": one.get("assert_screenshot") or "",
            "started_at": act_t0,
            "duration_ms": action.get("duration_ms") or 0,
            "phase": "overlay_guard",
            "guard_before_step": before_step_index,
            "guard_round": round_i,
            "plan": one.get("plan") or _plan_for_overlay(detect),
            "overlay_cleared": bool((one.get("assert") or {}).get("cleared")),
        },
        int(one.get("action_elapsed_ms") or gesture.get("run_elapsed_ms") or 0)
        if gesture
        else int(one.get("action_elapsed_ms") or 0),
    )
    if gesture:
        tap_row["screenshot_before"] = gesture.get("screenshot_before") or ""
        tap_row["screenshot_after"] = (
            gesture.get("screenshot_after") or tap_row.get("screenshot_after") or ""
        )
    return [tap_row]


def guard_iterations_to_step_results(
    guard_out: Dict[str, Any],
    *,
    before_step_index: int,
) -> List[Dict[str, Any]]:
    """将多轮守卫结果转为 execute_steps 行（兼容 run_overlay_guard_until_clear）。"""
    results: List[Dict[str, Any]] = []
    if not guard_out.get("attempted"):
        return results

    for it_i, it in enumerate(guard_out.get("iterations") or []):
        one = {
            "attempted": True,
            "detect": it.get("detect"),
            "action": it.get("action"),
            "assert": it.get("assert"),
            "plan": it.get("plan"),
            "type_title": it.get("type_title"),
                "assert_screenshot": it.get("assert_screenshot"),
                "action_elapsed_ms": it.get("action_elapsed_ms"),
            "assert_elapsed_ms": it.get("assert_elapsed_ms"),
        }
        results.extend(
            guard_round_to_step_results(
                one, before_step_index=before_step_index, round_i=it_i
            )
        )

    if guard_out.get("attempted") and not guard_out.get("ok"):
        results.append(
            {
                "index": _guard_plan_index(before_step_index, 99, "act"),
                "kind": "overlay_guard",
                "summary": "Plan · 阻塞弹窗守卫",
                "ok": False,
                "msg": guard_out.get("msg") or "守卫失败",
                "phase": "overlay_guard",
                "guard_before_step": before_step_index,
            }
        )

    return results


def guard_planned_steps(guard_out: Dict[str, Any], *, before_step_index: int) -> List[Dict[str, Any]]:
    """每轮守卫仅一个 Plan 节点；Detect/处置/Assert 作为同级 execute 行挂在同一 index。"""
    planned: List[Dict[str, Any]] = []
    for it_i, it in enumerate(guard_out.get("iterations") or []):
        type_title = it.get("type_title") or "阻塞弹窗"
        otype = (it.get("detect") or {}).get("type") or ""
        planned.extend(
            guard_planned_steps_for_round(
                before_step_index=before_step_index,
                round_i=it_i,
                type_title=type_title,
                otype=otype,
            )
        )
    return planned


def merge_guard_plan_log(
    plan_log: List[Dict[str, Any]],
    guard_planned: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """守卫 planned_step 与业务 planned_step 按 index 升序合并（业务 0..n，守卫 1xxx）。"""
    base = list(plan_log or [])
    steps = [e for e in base if e.get("type") == "planned_step"]
    rest = [e for e in base if e.get("type") != "planned_step"]
    merged_steps = sorted(steps + list(guard_planned or []), key=lambda x: int(x.get("index") or 0))
    out: List[Dict[str, Any]] = []
    if rest and rest[0].get("type") == "command":
        out.append(rest[0])
        rest = rest[1:]
    out.extend(merged_steps)
    out.extend(rest)
    return out
