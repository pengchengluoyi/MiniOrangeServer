# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""逐步执行 Copilot/用例步骤并返回每步结果。"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from script.log import SLog

from server.services.executor.ability import _execute_ability, _task_payload
from server.services.executor.mobile_actions import (
    _run_mobile_back,
    _run_mobile_click,
    _run_mobile_input,
    _run_mobile_input_coords,
    _run_mobile_key,
    _run_mobile_stop_app,
    _run_mobile_swipe,
    _run_mobile_swipe_coords,
)
from server.services.executor.plan_attempt import _record_plan_attempt_miss
from server.services.executor.step_context import _attach_step_page_context

TAG = "CopilotExecutor"

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
        from server.services.shared.run_context.regression_run_context import get_ctx

        if run_id and sn and not get_ctx():
            from server.services.shared.run_context.regression_run_context import begin_run

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
    last_click_target: Optional[Dict[str, Any]] = None

    for i, step in enumerate(steps or []):
        t0 = time.time()
        t_click = t0
        kind = step.get("kind", "")
        summary = step.get("summary", kind)
        SLog.i(TAG, f"Step {i} start: {summary}")

        foreground_note = ""
        if sn and pkg_guard and kind not in ("open_app", "close_app", "verify", "ability"):
            try:
                from server.services.app_automation_service import guard_test_app_foreground

                fg = guard_test_app_foreground(
                    sn, pkg_guard, platform, phase=f"step_{i}_{kind}"
                )
                if fg.get("drift") and fg.get("msg"):
                    foreground_note = str(fg.get("msg"))
            except Exception as e:
                SLog.w(TAG, f"foreground observe failed: {e}")
        out: Dict[str, Any] = {
            "index": i,
            "kind": kind,
            "summary": summary,
            "ok": False,
            "msg": "",
            "started_at": datetime.fromtimestamp(t0).isoformat(timespec="milliseconds"),
        }

        if capture_screenshots and sn and run_id and kind in ("open_app", "close_app", "back", "system_key"):
            try:
                from server.services.shared.screenshot.regression_capture import capture_device_screenshot

                out["screenshot_before"] = capture_device_screenshot(
                    sn,
                    platform,
                    run_id=run_id,
                    tag=f"s{i}_{kind or 'step'}_before",
                    settle_ms=80,
                )
            except Exception:
                out["screenshot_before"] = ""

        if sn:
            try:
                from driver.agent.Crawl.device_bootstrap import ensure_adb_device_online

                prep_t0 = time.time()
                ensure_adb_device_online(str(sn), platform)
                prep_ms = int((time.time() - prep_t0) * 1000)
                if prep_ms >= 1200:
                    out.setdefault("pre_events", []).append(
                        {
                            "type": "device_prepare",
                            "label": "设备准备",
                            "summary": "执行器在正式步骤前完成了设备唤醒/解锁/连接准备",
                            "method": "ensure_device_online",
                            "duration_ms": prep_ms,
                            "visible": True,
                        }
                    )
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
                    or step.get("package_name")
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
                from server.services.shared.run_context.regression_run_context import mark_step

                guard_round_i = 0
                max_guard_rounds = 3
                click_attempt = 0
                r: Dict[str, Any] = {"ok": False, "msg": ""}

                while True:
                    mark_step()

                    if step.get("ai_coordinate_only"):
                        t_click = time.time()
                        SLog.i(
                            TAG,
                            f"Step {i} AI coordinate click x={step.get('x')} y={step.get('y')} "
                            f"label={step.get('label')!r}",
                        )
                        r = _run_mobile_click(
                            sn,
                            int(step.get("x", 0)),
                            int(step.get("y", 0)),
                            label=step.get("label", ""),
                            platform=platform,
                            coords_explicit=True,
                            skip_label_lookup=True,
                            ai_coordinate_only=True,
                        )
                        out.update(r)
                        out["click_attempt"] = click_attempt
                        break

                    if (
                        use_reactive_guard
                        and click_attempt == 0
                        and guard_round_i < max_guard_rounds
                    ):
                        try:
                            import builtins
                            from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
                            from server.services.local.overlay.overlay_guard_service import (
                                apply_reactive_guard_round,
                                is_screen_blocked,
                            )
                            from server.services.local.navigation.page_navigation_service import (
                                is_overlay_dismiss_target_label,
                            )

                            step_label = str(step.get("label") or "")
                            from server.services.local.navigation.page_navigation_service import (
                                is_planned_overlay_step_label,
                            )

                            skip_guard = (
                                is_planned_overlay_step_label(step_label)
                                or is_planned_overlay_step_label(summary)
                                or (
                                    step_label
                                    and is_overlay_dismiss_target_label(step_label)
                                )
                            )
                            if step_label and not skip_guard:
                                builtins.TARGET_DEVICE_SN = str(sn)
                                engine_pg, (pgw, pgh) = bootstrap_mobile_engine(
                                    str(sn), platform
                                )
                                if is_screen_blocked(engine_pg):
                                    SLog.i(
                                        TAG,
                                        f"Step {i}: proactive guard round={guard_round_i} "
                                        f"before click {summary!r}",
                                    )
                                    proactive = apply_reactive_guard_round(
                                        engine_pg,
                                        pgw,
                                        pgh,
                                        before_step_index=i,
                                        round_i=guard_round_i,
                                        icon_targets=runtime_icons,
                                        app_id=str(learn_app_id or ""),
                                        step_summary=summary,
                                        sn=str(sn),
                                        platform=platform,
                                    )
                                    if proactive.get("planned_steps"):
                                        guard_planned_all.extend(
                                            proactive["planned_steps"]
                                        )
                                    for gr in proactive.get("step_rows") or []:
                                        gr["duration_ms"] = gr.get("duration_ms") or 0
                                        results.append(gr)
                                    if not proactive.get("attempted"):
                                        pass
                                    elif not proactive.get("can_retry_click"):
                                        last_action = (proactive.get("one") or {}).get(
                                            "action"
                                        ) or {}
                                        fail_out = {
                                            "index": i,
                                            "kind": kind,
                                            "summary": summary,
                                            "ok": False,
                                            "msg": proactive.get("msg")
                                            or "阻塞弹窗守卫失败",
                                            "method": "overlay_guard",
                                            "started_at": datetime.fromtimestamp(
                                                t0
                                            ).isoformat(timespec="milliseconds"),
                                            "duration_ms": int(
                                                (time.time() - t0) * 1000
                                            ),
                                            "overlay_guard": proactive,
                                            "locate_debug": last_action.get(
                                                "locate_debug"
                                            ),
                                        }
                                        try:
                                            from server.services.shared.run_context.regression_run_context import (
                                                stamp_run_timing,
                                            )

                                            stamp_run_timing(fail_out)
                                        except Exception:
                                            pass
                                        results.append(fail_out)
                                        out.update(fail_out)
                                        break
                                    else:
                                        guard_round_i += 1
                                        continue
                        except Exception as e:
                            SLog.w(TAG, f"proactive guard before click failed: {e}")

                    t_click = time.time()
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
                        out["click_attempt"] = click_attempt
                        break

                    if not use_reactive_guard:
                        out.update(r)
                        break

                    try:
                        import builtins
                        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
                        from server.services.local.overlay.overlay_guard_service import (
                            apply_reactive_guard_round,
                            is_screen_blocked,
                        )

                        builtins.TARGET_DEVICE_SN = str(sn)
                        engine_g, (gw, gh) = bootstrap_mobile_engine(str(sn), platform)
                        if not is_screen_blocked(engine_g):
                            out.update(r)
                            out["phase"] = "plan_attempt"
                            out["click_attempt"] = click_attempt
                            try:
                                from server.services.shared.run_context.regression_run_context import (
                                    capture_trace_frame,
                                )

                                miss_shot = capture_trace_frame(
                                    f"plan_attempt_{i}_{click_attempt}",
                                    settle_ms=80,
                                )
                                if miss_shot:
                                    out["screenshot_before"] = miss_shot
                                    out["screenshot_after"] = miss_shot
                            except Exception:
                                pass
                            break

                        step_label = str(step.get("label") or "")
                        try:
                            from server.services.local.navigation.page_navigation_service import (
                                is_planned_overlay_step_label,
                            )

                            if is_planned_overlay_step_label(
                                step_label
                            ) or is_planned_overlay_step_label(summary):
                                out.update(r)
                                out["phase"] = "plan_attempt"
                                out["click_attempt"] = click_attempt
                                break
                        except Exception:
                            pass

                        SLog.i(
                            TAG,
                            f"Step {i}: reactive guard round={guard_round_i} "
                            f"after click miss {summary!r}",
                        )
                        _record_plan_attempt_miss(
                            results,
                            step_index=i,
                            kind=kind,
                            summary=summary,
                            click_attempt=click_attempt,
                            r=r,
                            t0=t0,
                        )
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
                                from server.services.shared.run_context.regression_run_context import stamp_run_timing

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

                if out.get("ok") and out.get("target_rect"):
                    last_click_target = out.get("target_rect")
                else:
                    last_click_target = None

        elif kind == "swipe":
            if not sn:
                out["msg"] = "未选择设备"
            else:
                from server.services.shared.run_context.regression_run_context import mark_step

                mark_step()
                if step.get("ai_coordinate_only"):
                    r = _run_mobile_swipe_coords(
                        sn,
                        int(step.get("start_x") or 0),
                        int(step.get("start_y") or 0),
                        int(step.get("end_x") or 0),
                        int(step.get("end_y") or 0),
                        duration_ms=int(step.get("duration_ms") or 350),
                        platform=platform,
                    )
                else:
                    r = _run_mobile_swipe(sn, step.get("direction", "up"), platform)
                out.update(r)

        elif kind == "input":
            if not sn:
                out["msg"] = "未选择设备"
            else:
                from server.services.shared.run_context.regression_run_context import mark_step

                mark_step()
                SLog.i(
                    TAG,
                    f"Step {i} input begin text={step.get('text')!r} "
                    f"field={step.get('field_hint')!r}",
                )
                if step.get("ai_coordinate_only"):
                    r = _run_mobile_input_coords(
                        sn,
                        int(step.get("x") or 0),
                        int(step.get("y") or 0),
                        step.get("text") or "",
                        label=step.get("label") or step.get("field_hint") or "",
                        platform=platform,
                    )
                else:
                    r = _run_mobile_input(
                        sn,
                        step.get("text") or "",
                        field_hint=step.get("field_hint") or "",
                        platform=platform,
                        focus_rect=last_click_target
                        if step.get("bind_last_click")
                        else None,
                    )
                out.update(r)

        elif kind == "back":
            if not sn:
                out["msg"] = "未选择设备"
            else:
                from server.services.shared.run_context.regression_run_context import mark_step

                mark_step()
                r = _run_mobile_back(
                    sn,
                    platform,
                    immediate=True,
                )
                out.update(r)

        elif kind == "system_key":
            if not sn:
                out["msg"] = "未选择设备"
            else:
                from server.services.shared.run_context.regression_run_context import mark_step

                mark_step()
                out.update(_run_mobile_key(sn, step.get("key") or "", platform=platform))

        elif kind == "system_permission":
            if not sn:
                out["msg"] = "未选择设备"
            else:
                from server.services.local.navigation.page_navigation_service import (
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

        if foreground_note:
            out["foreground_drift"] = True
            out["foreground_note"] = foreground_note
            base_msg = (out.get("msg") or "").strip()
            out["msg"] = f"{base_msg}；{foreground_note}" if base_msg else foreground_note

        dur_t0 = t_click if kind == "click" else t0
        out["duration_ms"] = int((time.time() - dur_t0) * 1000)
        out["started_at"] = datetime.fromtimestamp(t0).isoformat(timespec="milliseconds")

        try:
            from server.services.shared.run_context.regression_run_context import stamp_run_timing

            stamp_run_timing(out)
        except Exception:
            pass

        try:
            from server.services.shared.run_context.regression_run_context import take_gestures_since_watermark

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
                from server.services.shared.screenshot.regression_capture import capture_device_screenshot

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
                from server.services.shared import icon_target_service as its

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
            from server.services.shared.run_context.regression_run_context import get_ctx

            ctx = get_ctx()
            if ctx is not None:
                ctx["guard_planned_steps"] = guard_planned_all
        except Exception:
            pass

    return results
