# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""飞书用例：顺序执行 + 预期校验 + 服务端缓存与执行日志。"""
from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from script.log import SLog
from server.services import copilot_service as cs
from server.services import app_automation_service as aas
from server.services.feishu_service import load_cases_from_config, normalize_feishu_case
from server.services.case_precondition_service import (
    has_precondition_phase,
    precondition_cleared_app_cache,
    run_preconditions,
)
from server.services.page_navigation_service import (
    _should_attempt_page_recovery,
    ensure_page_ready_before_action,
    try_dismiss_blocking_overlay,
    try_recover_and_reverify,
)

TAG = "FeishuRegression"

_RUNS: Dict[str, Dict[str, Any]] = {}
_RUNS_MAX_ENTRIES = 10
_RUNS_TTL_SEC = 6 * 3600


def _slim_run_for_memory_cache(run_doc: Dict[str, Any]) -> Dict[str, Any]:
    """完整 run 已入库后，内存只保留摘要，避免 _RUNS 长期堆积 trace/gesture。"""
    keys = (
        "run_id",
        "app_id",
        "app_name",
        "sn",
        "platform",
        "env_profile",
        "package",
        "started_at",
        "finished_at",
        "status",
        "total",
        "passed",
        "failed",
        "skipped",
        "duration_ms",
        "executed",
        "pending_clarification",
    )
    slim: Dict[str, Any] = {k: run_doc[k] for k in keys if k in run_doc}
    slim_cases: List[Dict[str, Any]] = []
    for c in run_doc.get("cases") or []:
        slim_cases.append(
            {
                k: c.get(k)
                for k in (
                    "case_id",
                    "name",
                    "status",
                    "msg",
                    "duration_ms",
                    "command",
                )
                if c.get(k) is not None
            }
        )
    slim["cases"] = slim_cases
    return slim


def _prune_runs_cache() -> None:
    """淘汰已完成且过期的 run，限制内存中缓存条数。"""
    if not _RUNS:
        return
    now = time.time()
    finished: List[tuple] = []
    for rid, doc in list(_RUNS.items()):
        if doc.get("status") == "running":
            continue
        ts = doc.get("finished_at") or doc.get("started_at") or ""
        age = _RUNS_TTL_SEC + 1
        try:
            if ts:
                age = now - datetime.fromisoformat(str(ts)).timestamp()
        except Exception:
            pass
        if age > _RUNS_TTL_SEC:
            _RUNS.pop(rid, None)
            continue
        finished.append((rid, ts))
    while len(_RUNS) > _RUNS_MAX_ENTRIES and finished:
        finished.sort(key=lambda x: x[1] or "")
        rid = finished.pop(0)[0]
        if _RUNS.get(rid, {}).get("status") != "running":
            _RUNS.pop(rid, None)


def _get_app_feishu_config(app) -> Dict[str, Any]:
    env = app.env if isinstance(app.env, dict) else {}
    return env.get("feishu") or {}


def save_app_feishu_config(app, config: Dict[str, Any]) -> Dict[str, Any]:
    from server.services.feishu_service import parse_feishu_sheet_url
    from sqlalchemy.orm.attributes import flag_modified

    env = dict(app.env) if isinstance(app.env, dict) else {}
    url = (config.get("doc_url") or config.get("url") or "").strip()
    parsed = parse_feishu_sheet_url(url)
    feishu = {
        "doc_url": url,
        "spreadsheet_token": config.get("spreadsheet_token") or parsed.get("spreadsheet_token"),
        "sheet_id": config.get("sheet_id") or parsed.get("sheet_id"),
        "data_range": config.get("data_range") or "A1:O500",
        "enabled": bool(config.get("enabled", True)),
        "bot_id": (config.get("bot_id") or "").strip(),
        "env_profile": (config.get("env_profile") or "test").strip() or "test",
    }
    env["feishu"] = feishu
    app.env = env
    flag_modified(app, "env")
    return feishu


def fetch_cases_for_app(app, *, persist: bool = True) -> Dict[str, Any]:
    cfg = _get_app_feishu_config(app)
    if not cfg.get("doc_url") and not cfg.get("spreadsheet_token"):
        raise RuntimeError("请先在应用中配置飞书表格链接")
    payload = load_cases_from_config(cfg)
    payload["cases"] = [normalize_feishu_case(c) for c in (payload.get("cases") or [])]
    if persist:
        cache = aas.save_feishu_cases_cache(app, payload)
        payload["cached_at"] = cache.get("synced_at")
    return payload


def list_cases_for_app(app, *, refresh: bool = False) -> Dict[str, Any]:
    if refresh:
        return fetch_cases_for_app(app, persist=True)
    cache = aas.get_feishu_cases_cache(app)
    if cache and cache.get("cases"):
        cases = [normalize_feishu_case(c) for c in (cache.get("cases") or [])]
        return {
            "cases": cases,
            "total": cache.get("total") or len(cases),
            "synced_at": cache.get("synced_at"),
            "from_cache": True,
            "resolve_note": cache.get("resolve_note") or "",
        }
    return fetch_cases_for_app(app, persist=True)


def _normalize_step_line(line: str) -> str:
    line = re.sub(r"^\d+[.、．)\）]\s*", "", (line or "").strip())
    if not line:
        return ""
    if not re.search(r"点击|打开|关闭|滑|等待|返回|启动", line, re.I):
        return f"点击{line}"
    return line


def _steps_to_command(case: Dict[str, Any]) -> str:
    parts: List[str] = []
    for line in case.get("steps") or []:
        norm = _normalize_step_line(line)
        if norm:
            parts.append(norm)
    return "，".join(parts)


def _trace_precondition_phase(
    case: Dict[str, Any],
    *,
    phase: str,
    sn: str,
    platform: str,
    package: str,
    trace: List[Dict[str, Any]],
) -> Optional[str]:
    """执行前置条件；不满足时返回错误说明。"""
    raw = (case.get("precondition") or "").strip()
    if not raw:
        return None
    res = run_preconditions(
        raw,
        sn=sn,
        platform=platform,
        package=package,
        phase=phase,
    )
    if not res.get("ok"):
        return res.get("msg") or "前置条件不满足"
    return None


def _probe_screen_before_prep(sn: str, platform: str) -> Tuple[bool, bool]:
    """ADB 轻量探测锁屏（不解锁、不 bootstrap）。"""
    if platform != "android" or not sn:
        return False, False
    try:
        import subprocess

        from driver.agent.Crawl.device_bootstrap import resolve_mobile_serial

        mobile_sn = resolve_mobile_serial(sn, platform)
        proc = subprocess.run(
            ["adb", "-s", mobile_sn, "shell", "dumpsys", "window"],
            capture_output=True,
            text=True,
            timeout=12,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        locked = any(
            m in out
            for m in (
                "mDreamingLockscreen=true",
                "mShowingLockscreen=true",
                "mKeyguardShowing=true",
                "isStatusBarKeyguard=true",
                "KeyguardShowing=true",
            )
        )
        return locked, False
    except Exception as e:
        SLog.w(TAG, f"screen probe failed: {e}")
        return False, False


def _append_foreground_trace(
    trace: List[Dict[str, Any]],
    fg: Dict[str, Any],
    *,
    run_id: str,
    sn: str,
    platform: str,
) -> None:
    """拉起被测应用写入时间轴（填补前置条件与业务步骤之间的空档）。"""
    from server.services.regression_run_context import stamp_run_timing

    row = stamp_run_timing(
        {
            "index": 0,
            "kind": "launch",
            "summary": "拉起被测应用",
            "ok": bool(fg.get("ok")),
            "msg": fg.get("msg") or "",
            "package": fg.get("package") or "",
            "foreground_before": fg.get("foreground_before") or "",
            "foreground_after": fg.get("foreground_after") or "",
        }
    )
    shot = _capture_step_screenshot(sn, platform, run_id=run_id, tag="foreground_after")
    if shot:
        row["screenshot_after"] = shot
    planned = [
        {
            "type": "planned_step",
            "index": 0,
            "kind": "launch",
            "summary": "拉起被测应用",
        }
    ]
    exec_log = aas.build_execute_log([row])
    trace.append(
        {
            "phase": "foreground",
            "title": "拉起被测应用",
            "subtitle": fg.get("package") or "",
            "ok": bool(fg.get("ok")),
            "entries": [
                {
                    "text": fg.get("msg") or "拉起被测应用",
                    "ok": bool(fg.get("ok")),
                    "msg": fg.get("msg") or "",
                    "kind": "launch",
                }
            ],
            "execute_log": exec_log,
            "operation": aas.build_operation_plan_tree(
                planned,
                exec_log,
                reply=fg.get("msg") or "拉起被测应用到前台",
            ),
        }
    )


def _append_device_prep_trace(
    *,
    sn: str,
    platform: str,
    run_id: str,
    trace: List[Dict[str, Any]],
) -> None:
    """记录设备唤醒/解锁步骤，便于回放排查黑屏、锁屏等问题。"""
    if not sn:
        return
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

        from server.services.regression_run_context import stamp_run_timing

        locked_before, blank_before = _probe_screen_before_prep(sn, platform)
        shot_lock = ""
        if run_id and locked_before:
            try:
                from server.services.regression_capture import capture_adb_raw_screenshot

                shot_lock = capture_adb_raw_screenshot(
                    sn,
                    platform,
                    run_id=run_id,
                    tag="device_prep_lock",
                    wake_first=True,
                )
            except Exception:
                shot_lock = ""

        entries: List[Dict[str, Any]] = []
        if locked_before:
            entries.append(
                stamp_run_timing(
                    {"text": "检测到锁屏", "ok": True, "msg": "准备解锁", "kind": "device_check"}
                )
            )
        if blank_before:
            entries.append(
                stamp_run_timing(
                    {
                        "text": "检测到黑屏/未点亮",
                        "ok": True,
                        "msg": "准备唤醒",
                        "kind": "device_check",
                    }
                )
            )

        builtins.TARGET_DEVICE_SN = sn
        engine, (screen_w, screen_h) = bootstrap_mobile_engine(sn, platform)

        shot_unlocked = ""
        if run_id:
            try:
                from server.services.regression_capture import capture_engine_screenshot

                shot_unlocked = capture_engine_screenshot(
                    engine,
                    run_id=run_id,
                    tag="device_prep_unlocked",
                    settle_ms=150,
                )
            except Exception:
                shot_unlocked = ""

        unlock_ok = not bool(getattr(engine, "_is_keyguard_showing", lambda: False)())
        if locked_before or blank_before:
            entries.append(
                stamp_run_timing(
                    {
                        "text": "唤醒并解锁屏幕",
                        "ok": unlock_ok,
                        "msg": "解锁成功" if unlock_ok else "解锁失败，请检查设备锁屏密码配置",
                        "kind": "device_unlock",
                    }
                )
            )
        elif not locked_before and not blank_before:
            entries.append(
                stamp_run_timing(
                    {
                        "text": "屏幕已点亮且未锁屏",
                        "ok": True,
                        "msg": "无需解锁",
                        "kind": "device_check",
                        "skipped": True,
                    }
                )
            )

        shot_after = shot_unlocked or ""
        if run_id and not shot_after:
            shot_after = _capture_step_screenshot(
                sn, platform, run_id=run_id, tag="device_prep_after"
            )

        def _prep_shots(entry: Dict[str, Any], idx: int) -> Tuple[str, str]:
            text = entry.get("text") or ""
            kind = entry.get("kind") or ""
            if text == "检测到锁屏":
                return shot_lock, shot_lock
            if kind == "device_unlock":
                return shot_lock, shot_unlocked or shot_after
            if text == "检测到黑屏/未点亮":
                return shot_lock, shot_lock
            if idx == 0:
                return shot_lock or shot_after, shot_after if len(entries) == 1 else ""
            if idx == len(entries) - 1:
                return "", shot_unlocked or shot_after
            return "", ""

        planned = [
            {
                "type": "planned_step",
                "index": i,
                "kind": e.get("kind") or "verify",
                "summary": e.get("text") or "设备准备",
            }
            for i, e in enumerate(entries)
        ]

        results = []
        for i, e in enumerate(entries):
            before_shot, after_shot = _prep_shots(e, i)
            results.append(
                {
                    "index": i,
                    "kind": "verify",
                    "summary": e.get("text") or "",
                    "ok": e.get("ok"),
                    "msg": e.get("msg") or "",
                    "screenshot_before": before_shot,
                    "screenshot_after": after_shot,
                    "screen_size": {"w": screen_w, "h": screen_h},
                    "run_elapsed_ms": e.get("run_elapsed_ms"),
                    "run_elapsed": e.get("run_elapsed") or "",
                }
            )
        exec_log = aas.build_execute_log(results)
        trace.append(
            {
                "phase": "device_prep",
                "title": "设备准备",
                "subtitle": "唤醒 / 解锁屏幕",
                "ok": unlock_ok,
                "entries": entries,
                "execute_log": exec_log,
                "operation": aas.build_operation_plan_tree(
                    planned,
                    exec_log,
                    reply="执行前检查设备屏幕状态，必要时自动唤醒并解锁",
                ),
            }
        )
    except Exception as e:
        SLog.w(TAG, f"device prep trace failed: {e}")
        trace.append(
            {
                "phase": "device_prep",
                "title": "设备准备",
                "ok": False,
                "entries": [{"text": "设备准备", "ok": False, "msg": str(e)}],
            }
        )


def _append_precondition_trace(
    case: Dict[str, Any],
    *,
    before_items: List[Dict[str, Any]],
    after_items: List[Dict[str, Any]],
    ok: bool,
    trace: List[Dict[str, Any]],
) -> None:
    """合并启动前/后前置条件为一条 trace。"""
    raw = (case.get("precondition") or "").strip()
    if not raw and not before_items and not after_items:
        return
    from server.services.regression_run_context import stamp_run_timing

    entries = []
    for i in before_items + after_items:
        entries.append(
            {
                "text": i.get("text"),
                "kind": i.get("kind"),
                "msg": i.get("msg"),
                "ok": i.get("ok"),
                "skipped": i.get("skipped"),
                "operator": i.get("operator"),
                "phone_number": i.get("phone_number"),
                "sim_state": i.get("sim_state"),
            }
        )
    planned = [
        {
            "type": "planned_step",
            "index": idx,
            "kind": e.get("kind") or "verify",
            "summary": e.get("text") or "前置条件",
        }
        for idx, e in enumerate(entries)
    ]
    merged_items = list(before_items) + list(after_items)
    results = []
    for idx, e in enumerate(entries):
        row = {
            "index": idx,
            "kind": "verify",
            "summary": e.get("text") or "",
            "ok": e.get("ok"),
            "msg": e.get("msg") or "",
        }
        src = merged_items[idx] if idx < len(merged_items) else {}
        if src.get("run_elapsed_ms") is not None:
            row["run_elapsed_ms"] = src["run_elapsed_ms"]
            row["run_elapsed"] = src.get("run_elapsed") or ""
        else:
            stamp_run_timing(row)
        results.append(row)
    exec_log = aas.build_execute_log(results)
    trace.append(
        {
            "phase": "precondition",
            "title": "前置条件",
            "subtitle": raw,
            "ok": ok,
            "entries": entries,
            "execute_log": exec_log,
            "operation": aas.build_operation_plan_tree(
                planned,
                exec_log,
                reply=raw or "前置条件校验",
            ),
        }
    )


def _skills_to_command(lines: List[str]) -> str:
    parts = []
    for line in lines or []:
        s = (line or "").strip()
        if not s:
            continue
        if not re.search(r"点击|打开|关闭|滑|等待|返回|启动", s, re.I):
            parts.append(f"点击{s}")
        else:
            parts.append(s)
    return "，".join(parts)


def _load_icon_targets(db: Optional[Session], app_id: str) -> List[Dict[str, Any]]:
    if db is None:
        return []
    try:
        from server.services.icon_target_service import list_for_copilot

        return list_for_copilot(db, app_id)
    except Exception:
        return []


def _run_command_block(
    command: str,
    *,
    sn: str,
    platform: str,
    context: Dict[str, Any],
    icon_targets: List[Dict[str, Any]],
    phase: str,
    run_id: str = "",
) -> Dict[str, Any]:
    if not command:
        return {
            "phase": phase,
            "command": "",
            "plan_log": [],
            "execute_log": [],
            "step_results": [],
            "ok": True,
        }
    plan = cs.plan_message(command, sn=sn, context=context)
    plan_log = aas.build_plan_log(command, plan)
    if plan.get("error") or not plan.get("steps"):
        return {
            "phase": phase,
            "command": command,
            "plan_log": plan_log,
            "execute_log": [],
            "step_results": [],
            "ok": False,
            "msg": plan.get("reply") or plan.get("error") or "规划失败",
        }
    results = cs.execute_steps(
        plan.get("steps") or [],
        sn=sn,
        platform=platform,
        icon_targets=icon_targets,
        run_id=run_id,
        capture_screenshots=bool(run_id),
        app_id=str(context.get("app_id") or context.get("appId") or ""),
        skip_overlay_clear=bool(context.get("skip_overlay_clear")),
        enable_overlay_guard=not bool(context.get("skip_overlay_guard")),
        target_package=str(context.get("package") or ""),
        stop_on_failure=True,
    )
    try:
        from server.services.regression_run_context import get_ctx
        from server.services.overlay_guard_service import merge_guard_plan_log

        gctx = get_ctx()
        guard_planned = (gctx or {}).get("guard_planned_steps") or []
        if guard_planned:
            plan_log = merge_guard_plan_log(plan_log, guard_planned)
            if gctx is not None:
                gctx["guard_planned_steps"] = []
    except Exception:
        pass
    segment_errors = list(plan.get("segment_errors") or [])
    ok = aas.business_step_results_ok(results)
    if results:
        guard_fail = any(
            not r.get("ok")
            for r in results
            if (r.get("phase") or "") == "overlay_guard"
            and (r.get("kind") or "") in ("overlay_guard", "click")
        )
        if guard_fail:
            ok = False
    if segment_errors:
        ok = False
    fail_msgs = [r.get("msg") or "" for r in results if not r.get("ok")]
    if segment_errors:
        fail_msgs = segment_errors + fail_msgs
    return {
        "phase": phase,
        "command": command,
        "plan_log": plan_log,
        "execute_log": aas.build_execute_log(results),
        "step_results": results,
        "reply": plan.get("reply") or "",
        "ok": ok,
        "msg": "；".join(m for m in fail_msgs if m)[:400],
        "segment_errors": segment_errors,
        "plan_complete": plan.get("plan_complete", len(segment_errors) == 0),
        "knowledge_hints": list(plan.get("knowledge_hints") or []),
        "page_hint": plan.get("page_hint") or "",
        "thought_meta": {
            "reply": plan.get("display_reply") or plan.get("reply") or "",
            "plan_reply": plan.get("display_reply") or plan.get("reply") or "",
            "knowledge_hints": list(plan.get("knowledge_hints") or []),
            "page_hint": plan.get("page_hint") or "",
            "segment_errors": list(segment_errors),
            "plan_log": plan_log,
        },
    }


def _stamp_case_duration(item: Dict[str, Any], case_started: float) -> None:
    """确保早停/continue 的用例也有耗时。"""
    if item.get("duration_ms") is not None:
        return
    finished = time.time()
    item.setdefault("started_at", datetime.fromtimestamp(case_started).isoformat())
    item["finished_at"] = datetime.fromtimestamp(finished).isoformat()
    item["duration_ms"] = max(0, int((finished - case_started) * 1000))


def _collect_screen_text(
    engine, screen_w: int, screen_h: int, *, force: bool = False
) -> str:
    try:
        from server.services.page_context_service import _collect_full_screen_text

        return _collect_full_screen_text(engine, force=force)
    except Exception as e:
        SLog.w(TAG, f"collect screen text failed: {e}")
        return ""


def _prepare_screen_for_verify(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    sn: str = "",
    platform: str = "android",
) -> List[Dict[str, Any]]:
    """校验前运行阻塞弹窗守卫，避免挡住页面识别。"""
    from script.sleep import mSleep
    from server.services.overlay_guard_service import run_overlay_guard_until_clear

    gestures: List[Dict[str, Any]] = []
    try:
        guard_out = run_overlay_guard_until_clear(
            engine, screen_w, screen_h, max_rounds=5
        )
        for it in guard_out.get("iterations") or []:
            g = (it.get("action") or {}).get("gesture")
            if g and g not in gestures:
                gestures.append(g)
        mSleep(0.4)
    except Exception as e:
        SLog.w(TAG, f"prepare screen for verify failed: {e}")
    return gestures


def _navigation_expectation_conflict(exp: str, screen_text: str) -> Optional[str]:
    """检测「进入某页」类预期与当前界面是否矛盾（如在首页却断言已进入详情页）。"""
    blob = screen_text or ""
    if not blob or not exp:
        return None
    exp_l = exp.lower()

    if "详情" in exp:
        home_markers = ["首页", "推荐", "发现", "关注", "AI创意", "造物", "社区"]
        detail_markers = ["商品详情", "作品详情", "详情页", "立即购买", "加入购物车", "规格参数", "评论"]
        home_hits = sum(1 for w in home_markers if w in blob)
        detail_hits = sum(1 for w in detail_markers if w in blob)
        # 底栏仍在首页且缺少详情页特征 → 视为未进入详情
        if "首页" in blob and detail_hits < 1 and home_hits >= 1:
            return "界面仍为首页/feed，未进入详情页"
        if home_hits >= 2 and detail_hits < 1 and "详情" in exp_l:
            return "界面仍为列表/首页态，未进入详情页"

    if "首页" in exp and "首页" not in blob:
        try:
            from server.services.expectation_semantic_service import normalize_page_intent

            if normalize_page_intent(exp) == "首页":
                return None
        except Exception:
            pass
        if any(w in blob for w in ("详情", "我的", "消息", "设置")):
            return "当前不在首页"

    return None


def _check_expected(
    expected_lines: List[str],
    screen_text: str,
    step_results: List[Dict[str, Any]],
    *,
    page_context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    checks: List[Dict[str, Any]] = []
    all_steps_ok = aas.business_step_results_ok(step_results)
    blob = screen_text or ""

    try:
        from server.services.page_context_service import enrich_check_with_page
    except Exception:
        enrich_check_with_page = None

    for line in expected_lines or []:
        exp = re.sub(r"^\d+[.、．)\）]\s*", "", (line or "").strip())
        if not exp:
            continue
        ok = False
        reason = ""

        if not all_steps_ok:
            ok = False
            reason = "前置操作未成功，界面校验无效"
            if page_context and (page_context.get("label") or page_context.get("figma_best")):
                cur = page_context.get("label") or page_context.get("figma_best")
                src = "Figma" if page_context.get("method") == "figma_text" else "图谱"
                score = page_context.get("score")
                score_txt = f" {float(score):.0%}" if score is not None else ""
                reason += f"（当前页「{cur}」· {src}{score_txt}）"
        else:
            page_verdict = None
            try:
                from server.services.expectation_semantic_service import (
                    evaluate_dynamic_expectation,
                )

                page_verdict = evaluate_dynamic_expectation(
                    exp, blob, step_results=step_results
                )
            except Exception:
                pass
            if page_verdict is None and enrich_check_with_page:
                page_verdict = enrich_check_with_page(
                    exp,
                    page_context or {},
                    steps_ok=all_steps_ok,
                    screen_text=blob,
                )
            if page_verdict is not None:
                ok = bool(page_verdict.get("ok"))
                reason = page_verdict.get("reason") or ""
            else:
                nav_conflict = _navigation_expectation_conflict(exp, blob)
                if nav_conflict:
                    ok = False
                    reason = nav_conflict
                elif exp in blob:
                    ok = True
                    reason = "界面文案匹配"
                else:
                    fragments = [x.strip() for x in re.split(r"[、,，;；]", exp) if len(x.strip()) >= 2]
                    hit = sum(1 for f in fragments if f in blob)
                    if fragments and hit >= max(1, len(fragments) // 2):
                        if "进入" in exp or "页" in exp:
                            ok = exp in blob
                            reason = "界面文案匹配" if ok else "页面状态与预期不符（部分关键词不足）"
                        else:
                            ok = True
                            reason = f"部分关键词匹配 {hit}/{len(fragments)}"
                    else:
                        reason = "未在界面中找到预期文案"
        checks.append({"text": exp, "ok": ok, "reason": reason})
    return checks


def _verify_case(
    case: Dict[str, Any],
    step_results: List[Dict[str, Any]],
    *,
    sn: str,
    platform: str = "android",
    package: str = "",
    app_id: str = "",
) -> Dict[str, Any]:
    page_context: Dict[str, Any] = {}
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

        if package and not _case_allows_background(case):
            aas.ensure_app_foreground(sn, package, platform)

        builtins.TARGET_DEVICE_SN = sn
        engine, (w, h) = bootstrap_mobile_engine(sn, platform)
        _prepare_screen_for_verify(engine, w, h, sn=sn, platform=platform)
        screen_text = _collect_screen_text(engine, w, h)
        if app_id:
            from server.services.page_context_service import identify_for_app

            page_context = identify_for_app(
                app_id, engine, frame_count=1, screen_text=screen_text
            )
            exp_join = " ".join(case.get("expected") or [])
            if exp_join:
                page_context = _enrich_page_context_meta(page_context, exp_join, app_id)
    except Exception as e:
        screen_text = ""
        SLog.w(TAG, f"verify screenshot failed: {e}")

    checks = _check_expected(
        case.get("expected") or [],
        screen_text,
        step_results,
        page_context=page_context,
    )
    steps_ok = aas.business_step_results_ok(step_results)
    checks_ok = all(c.get("ok") for c in checks) if checks else steps_ok

    if steps_ok and checks_ok:
        status = "pass"
        msg = "执行与预期校验通过"
    elif steps_ok and not checks:
        status = "pass"
        msg = "步骤执行成功（无预期条目）"
    elif steps_ok:
        status = "fail"
        failed = [c["text"] for c in checks if not c.get("ok")]
        msg = f"步骤成功但预期未达标: {', '.join(failed[:3])}"
    else:
        status = "fail"
        msg = "存在失败的执行步骤"

    return {
        "status": status,
        "msg": msg,
        "checks": checks,
        "steps_ok": steps_ok,
        "screen_text_preview": (screen_text or "")[:500],
        "page_context": page_context,
    }


def _capture_step_screenshot(
    sn: str,
    platform: str,
    *,
    run_id: str = "",
    tag: str = "",
) -> str:
    if not run_id:
        return ""
    try:
        from server.services.regression_capture import capture_device_screenshot

        return capture_device_screenshot(sn, platform, run_id=run_id, tag=tag) or ""
    except Exception:
        return ""


def _analyze_expected_plans(
    expected_text: str,
    *,
    sn: str = "",
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """预期动作语义拆解 → Plan 列表（与操作 plan_message 对称）。"""
    exp = re.sub(r"^\d+[.、．)\）]\s*", "", (expected_text or "").strip())
    if not exp:
        return {"reply": "", "plan_log": [], "planned": []}

    plan_cmd = f"验证预期：{exp}"
    plan = cs.plan_message(plan_cmd, sn=sn, context=context or {})
    plan_log = aas.build_plan_log(plan_cmd, plan)

    fragments = aas._split_expected_fragments(expected_text)
    planned = [
        {
            "type": "planned_step",
            "index": i,
            "summary": frag,
            "kind": "verify",
        }
        for i, frag in enumerate(fragments)
    ]

    return {
        "reply": plan.get("reply") or f"验证预期：{exp}",
        "plan_log": plan_log,
        "planned": planned,
    }


def _enrich_page_context_meta(
    page_context: Dict[str, Any],
    expected: str,
    app_id: str,
) -> Dict[str, Any]:
    if not app_id:
        return page_context
    try:
        from server.core.database import SessionLocal
        from server.models.project import App
        from server.services.figma_logic_service import load_figma_logic_for_app
        from server.services.page_context_service import load_app_graph_by_app_id
        from server.services.page_navigation_service import resolve_target_page_from_expected

        session = SessionLocal()
        try:
            app_graph = load_app_graph_by_app_id(session, app_id)
            app = session.query(App).filter(App.id == str(app_id)).first()
            figma_logic = load_figma_logic_for_app(app) if app else None
            target = resolve_target_page_from_expected(expected, app_graph, figma_logic)
            out = dict(page_context or {})
            cur = out.get("label") or out.get("figma_best")
            out["current_page_label"] = cur
            out["target_page"] = target
            if not out.get("matched") and out.get("figma_best"):
                out["label"] = out.get("figma_best")
            return out
        finally:
            session.close()
    except Exception as e:
        SLog.w(TAG, f"enrich page context failed: {e}")
        return page_context


def _verify_step_expected(
    expected_text: str,
    step_results: List[Dict[str, Any]],
    *,
    sn: str,
    platform: str = "android",
    package: str = "",
    app_id: str = "",
    case: Optional[Dict[str, Any]] = None,
    run_id: str = "",
    step_index: int = 0,
    case_id: str = "",
    icon_targets: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """单条预期校验（对应用例表「步骤 N」→「预期 N」）。"""
    exp = re.sub(r"^\d+[.、．)\）]\s*", "", (expected_text or "").strip())
    if not exp:
        return {
            "ok": True,
            "msg": "无预期条目",
            "checks": [],
            "screenshot": "",
            "plan_tree": {"thought": "", "plans": []},
            "plan_log": [],
            "page_context": {},
        }

    screen_text = ""
    page_context: Dict[str, Any] = {}
    device_lost = False
    try:
        import builtins
        from script.sleep import mSleep
        from driver.agent.Crawl.device_bootstrap import (
            DeviceOfflineError,
            bootstrap_mobile_engine,
            is_adb_device_online,
        )
        from server.services.page_context_service import identify_page_for_trace

        if not is_adb_device_online(sn, platform):
            device_lost = True
            raise DeviceOfflineError(f"设备 {sn} 已离线，跳过页面校验")

        if package and case and not _case_allows_background(case):
            aas.guard_test_app_foreground(sn, package, platform)

        builtins.TARGET_DEVICE_SN = sn
        engine, (w, h) = bootstrap_mobile_engine(sn, platform)
        _prepare_screen_for_verify(engine, w, h, sn=sn, platform=platform)

        max_rounds = 2
        for attempt in range(max_rounds):
            if attempt > 0:
                if not is_adb_device_online(sn, platform):
                    device_lost = True
                    SLog.w(TAG, f"verify aborted: device offline sn={sn}")
                    break
                mSleep(0.45)
                engine, (w, h) = bootstrap_mobile_engine(sn, platform)
            screen_text = _collect_screen_text(engine, w, h, force=attempt > 0)
            if not app_id:
                break
            page_context = identify_page_for_trace(
                app_id,
                engine,
                frame_count=1,
                screen_text=screen_text,
                sn=sn,
                platform=platform,
                run_id=run_id,
                tag=f"verify_s{step_index}",
            )
            page_context = _enrich_page_context_meta(page_context, exp, app_id)
            trial_checks = _check_expected(
                [exp], screen_text, step_results, page_context=page_context
            )
            if trial_checks and trial_checks[0].get("ok"):
                break
            if attempt < max_rounds - 1 and not device_lost:
                SLog.i(
                    TAG,
                    f"verify retry {attempt + 1}/{max_rounds - 1} exp={exp[:24]!r} "
                    f"page={page_context.get('label')}",
                )
    except Exception as e:
        from driver.agent.Crawl.device_bootstrap import DeviceOfflineError

        if isinstance(e, DeviceOfflineError):
            device_lost = True
            SLog.w(TAG, f"verify step device offline: {e}")
        else:
            SLog.w(TAG, f"verify step screenshot failed: {e}")

    steps_ok = aas.business_step_results_ok(step_results)
    checks = _check_expected(
        [exp], screen_text, step_results, page_context=page_context
    )
    checks_ok = all(c.get("ok") for c in checks) if checks else False
    recovery: Optional[Dict[str, Any]] = None

    if device_lost:
        return {
            "ok": False,
            "msg": f"设备 {sn} 已离线，预期校验中止",
            "checks": [
                {
                    "text": exp,
                    "ok": False,
                    "reason": "设备离线",
                }
            ],
            "plan_tree": {"thought": "", "plans": []},
            "plan_log": [],
            "screen_text_preview": "",
            "screenshot": "",
            "page_context": page_context,
            "page_recovery": None,
            "device_offline": True,
        }

    if _should_attempt_page_recovery(
        exp,
        checks_ok=checks_ok,
        checks=checks,
        page_context=page_context,
        screen_text=screen_text,
        app_id=app_id,
        step_results=step_results,
    ):
        try:
            from server.core.database import SessionLocal

            db = SessionLocal()
            try:
                recovery = try_recover_and_reverify(
                    exp,
                    sn=sn,
                    platform=platform,
                    app_id=app_id,
                    session=db,
                    page_context=page_context,
                    screen_text=screen_text,
                    step_results=step_results,
                    icon_targets=icon_targets,
                    run_id=run_id,
                )
            finally:
                db.close()
            if recovery and recovery.get("attempted"):
                page_context = recovery.get("current_page_after") or page_context
                page_context = _enrich_page_context_meta(page_context, exp, app_id)
                screen_text = recovery.get("screen_text_after") or screen_text
                checks = _check_expected(
                    [exp], screen_text, step_results, page_context=page_context
                )
                checks_ok = all(c.get("ok") for c in checks) if checks else False
        except Exception as e:
            SLog.w(TAG, f"page recovery failed: {e}")
    action_shot = ""
    for r in reversed(step_results or []):
        action_shot = r.get("screenshot_after") or r.get("screenshot_before") or ""
        if action_shot:
            break
    screenshot = (
        page_context.get("screenshot")
        or action_shot
        or _capture_step_screenshot(
            sn,
            platform,
            run_id=run_id,
            tag=f"verify_{case_id}_{step_index}",
        )
    )

    expected_analysis = _analyze_expected_plans(
        expected_text,
        sn=sn,
        context={"app_id": app_id or (case or {}).get("case_id"), "platform": platform},
    )
    plan_tree = aas.build_expected_plan_tree(
        expected_text,
        checks,
        reply=(expected_analysis.get("reply") or "")
        + (
            f"\n📍 当前页：{page_context.get('current_page_label') or page_context.get('label') or page_context.get('figma_best') or '未知'}"
            + (
                f"（Figma {float(page_context.get('figma_score') or page_context.get('score') or 0):.0%}）"
                if page_context.get("figma_best") or page_context.get("method") == "figma_text"
                else ""
            )
            if page_context
            else ""
        )
        + (
            f"\n🎯 目标页：{(page_context.get('target_page') or {}).get('label') or '-'}"
            if page_context.get("target_page")
            else ""
        ),
    )

    # 若语义拆解出多个 Plan，按片段分别校验并回填
    if len(plan_tree.get("plans") or []) > 1:
        enriched_plans = []
        for p in plan_tree["plans"]:
            frag = p.get("verify_text") or p.get("summary") or ""
            frag_checks = _check_expected(
                [frag], screen_text, step_results, page_context=page_context
            )
            enriched_plans.append(
                {
                    **p,
                    "checks": frag_checks,
                    "ok": all(c.get("ok") for c in frag_checks),
                }
            )
        plan_tree["plans"] = enriched_plans
        checks_ok = all(p.get("ok") for p in enriched_plans)

    if not steps_ok:
        invalidated = []
        for c in checks:
            row = dict(c)
            if row.get("ok"):
                row["ok"] = False
                row["reason"] = f"前置操作失败，断言无效（原：{row.get('reason') or '匹配'}）"
            invalidated.append(row)
        checks = invalidated
        checks_ok = False
        plan_tree = aas.build_expected_plan_tree(
            expected_text,
            checks,
            reply=expected_analysis.get("reply") or "",
        )
        return {
            "ok": False,
            "msg": "操作未成功，预期校验无效",
            "checks": checks,
            "plan_tree": plan_tree,
            "plan_log": expected_analysis.get("plan_log") or [],
            "screen_text_preview": (screen_text or "")[:500],
            "screenshot": screenshot,
            "page_context": page_context,
            "page_recovery": recovery,
        }
    if checks_ok:
        return {
            "ok": True,
            "msg": "预期达成",
            "checks": checks,
            "plan_tree": plan_tree,
            "plan_log": expected_analysis.get("plan_log") or [],
            "screen_text_preview": (screen_text or "")[:500],
            "screenshot": screenshot,
            "page_context": page_context,
            "page_recovery": recovery,
        }
    failed = [c["text"] for c in checks if not c.get("ok")]
    msg = f"预期未达成: {', '.join(failed[:2])}"
    if recovery and recovery.get("attempted"):
        nav_n = len((recovery.get("plan") or {}).get("steps") or [])
        msg += f"（已尝试页面恢复 {nav_n} 步）"
    return {
        "ok": False,
        "msg": msg,
        "checks": checks,
        "plan_tree": plan_tree,
        "plan_log": expected_analysis.get("plan_log") or [],
        "screen_text_preview": (screen_text or "")[:500],
        "screenshot": screenshot,
        "page_context": page_context,
        "page_recovery": recovery,
    }


def _last_screenshot_from_execute_log(execute_log: List[Dict[str, Any]]) -> str:
    for entry in reversed(execute_log or []):
        shot = entry.get("screenshot_after") or entry.get("screenshot_before")
        if shot:
            return shot
    return ""


def _last_locate_meta_from_execute_log(
    execute_log: List[Dict[str, Any]],
) -> Dict[str, Any]:
    for entry in reversed(execute_log or []):
        if entry.get("locate_debug"):
            return {
                "locate_debug": entry.get("locate_debug"),
                "screen_size": entry.get("screen_size") or {},
            }
    return {}


def _run_case_steps_sequential(
    case: Dict[str, Any],
    *,
    sn: str,
    platform: str,
    context: Dict[str, Any],
    icon_targets: List[Dict[str, Any]],
    package: str,
    run_id: str,
    start_step_index: int = 0,
    initial_trace: Optional[List[Dict[str, Any]]] = None,
    initial_all_results: Optional[List[Dict[str, Any]]] = None,
    pause_on_clarification: bool = True,
) -> Dict[str, Any]:
    """按飞书用例编号逐步执行：每步操作 → 对应预期校验。"""
    step_lines = list(case.get("steps") or [])
    expected_lines = list(case.get("expected") or [])
    step_nums = list(case.get("step_nums") or [])
    case = normalize_feishu_case(case)
    expected_by_step: Dict[int, str] = dict(case.get("expected_by_step") or {})
    trace: List[Dict[str, Any]] = list(initial_trace or [])
    all_results: List[Dict[str, Any]] = list(initial_all_results or [])
    overall_ok = True
    fail_msg = ""

    # 拉起应用由 run_cases 在本用例步骤开始前统一执行一次。

    for i, step_line in enumerate(step_lines):
        if i < start_step_index:
            continue
        try:
            from driver.agent.Crawl.device_bootstrap import is_adb_device_online

            if not is_adb_device_online(sn, platform):
                overall_ok = False
                fail_msg = f"设备 {sn} 已离线，用例中止于步骤 {i + 1}"
                SLog.w(TAG, fail_msg)
                break
        except Exception:
            pass

        step_no = step_nums[i] if i < len(step_nums) else (i + 1)
        expected_text = (expected_by_step.get(step_no) or "").strip()
        step_block: Dict[str, Any] = {
            "phase": "case_step",
            "step_index": i,
            "step_no": i + 1,
            "action_text": step_line,
            "expected_text": expected_text,
        }

        cmd = _normalize_step_line(step_line)
        app_id_str = str(context.get("app_id") or context.get("appId") or "")
        pre_action_recovery: Optional[Dict[str, Any]] = None
        if app_id_str and not context.get("skip_pre_action_recovery"):
            try:
                from server.core.database import SessionLocal

                db = SessionLocal()
                try:
                    pre_action_recovery = ensure_page_ready_before_action(
                        sn=sn,
                        platform=platform,
                        app_id=app_id_str,
                        session=db,
                        step_text=step_line,
                        icon_targets=icon_targets,
                        run_id=run_id,
                        target_package=package,
                    )
                finally:
                    db.close()
            except Exception as e:
                SLog.w(TAG, f"pre-action page ready failed: {e}")

        step_ctx = dict(context)
        if pre_action_recovery and pre_action_recovery.get("attempted"):
            if not pre_action_recovery.get("overlay_guard_delegated"):
                step_ctx["skip_overlay_clear"] = True

        first_block = _run_command_block(
            cmd,
            sn=sn,
            platform=platform,
            context=step_ctx,
            icon_targets=icon_targets,
            phase="action",
            run_id=run_id,
        )
        action_block = first_block
        exec_log = list(action_block.get("execute_log") or [])
        plan_log = action_block.get("plan_log") or []
        step_results_merged = [
            {**r, "attempt": 1} for r in (action_block.get("step_results") or [])
        ]
        action_ok = bool(action_block.get("ok"))
        post_recovery: Optional[Dict[str, Any]] = None
        if (
            not action_ok
            and app_id_str
            and not run_id
            and not (
                pre_action_recovery
                and (
                    pre_action_recovery.get("attempted")
                    or pre_action_recovery.get("overlay_guard_delegated")
                )
            )
        ):
            fail_msg_text = action_block.get("msg") or ""
            overlay_blocked = any(
                k in fail_msg_text
                for k in ("consent", "弹层遮挡", "权限弹", "协议弹窗", "启动弹层")
            )
            try:
                from server.core.database import SessionLocal

                db = SessionLocal()
                try:
                    post_recovery = try_dismiss_blocking_overlay(
                        sn=sn,
                        platform=platform,
                        app_id=app_id_str,
                        session=db,
                        icon_targets=icon_targets,
                        run_id=run_id,
                        target_package=package,
                    )
                finally:
                    db.close()
                if (
                    overlay_blocked
                    and post_recovery
                    and post_recovery.get("attempted")
                    and post_recovery.get("ok")
                ):
                    retry_block = _run_command_block(
                        cmd,
                        sn=sn,
                        platform=platform,
                        context=context,
                        icon_targets=icon_targets,
                        phase="action_retry",
                        run_id=run_id,
                    )
                    for r in retry_block.get("step_results") or []:
                        step_results_merged.append({**r, "attempt": 2})
                    exec_log.extend(retry_block.get("execute_log") or [])
                    action_block = {
                        **retry_block,
                        "step_results": step_results_merged,
                        "execute_log": exec_log,
                        "first_attempt": first_block,
                    }
                    action_ok = bool(retry_block.get("ok"))
            except Exception as e:
                SLog.w(TAG, f"post-action overlay recovery failed: {e}")

        delegated_guard = bool(
            (pre_action_recovery or {}).get("overlay_guard_delegated")
        )
        pre_rec = (
            pre_action_recovery
            if pre_action_recovery
            and pre_action_recovery.get("attempted")
            and not delegated_guard
            else None
        )
        page_recovery = post_recovery or pre_rec

        recovery_exec: List[Dict[str, Any]] = []
        if (
            not delegated_guard
            and page_recovery
            and page_recovery.get("attempted")
            and page_recovery.get("nav_results")
        ):
            recovery_exec = aas.build_execute_log(page_recovery.get("nav_results") or [])
            for e in recovery_exec:
                e["phase"] = "recovery"
        exec_log_ordered = recovery_exec + exec_log

        op_tree = aas.build_operation_plan_tree(
            plan_log,
            exec_log_ordered,
            reply=action_block.get("reply") or "",
        )
        display_recovery = None if delegated_guard else (dict(page_recovery) if page_recovery else None)
        if display_recovery and recovery_exec:
            display_recovery = {**display_recovery, "nav_results": []}

        step_block["operation"] = {
            "text": step_line,
            "command": cmd,
            "thought": cmd,
            "thought_meta": {
                **(action_block.get("thought_meta") or {}),
                "plan_reply": (
                    action_block.get("thought_meta", {}).get("plan_reply")
                    or action_block.get("reply")
                    or ""
                ),
                "command": cmd,
            },
            "knowledge_hints": action_block.get("knowledge_hints") or [],
            "plans": op_tree.get("plans") or [],
            "flat_items": op_tree.get("flat_items") or [],
            "plan_log": plan_log,
            "execute_log": exec_log_ordered,
            "ok": action_ok,
            "action_ok": action_ok,
            "msg": action_block.get("msg"),
            "screenshot": _last_screenshot_from_execute_log(exec_log_ordered),
            "page_recovery": display_recovery,
            "page_context": {}
            if delegated_guard
            else (
                (pre_action_recovery or {}).get("current_page_before")
                or (page_recovery or {}).get("current_page_after")
                or {}
            ),
        }
        # 兼容旧字段
        step_block["action"] = step_block["operation"]
        all_results.extend(step_results_merged or action_block.get("step_results") or [])

        op_shot = _last_screenshot_from_execute_log(exec_log_ordered)
        locate_meta = _last_locate_meta_from_execute_log(exec_log_ordered)

        expected_part: Dict[str, Any] = {"text": expected_text, "ok": True, "checks": [], "screenshot": ""}
        if not expected_text:
            expected_part = {
                "text": "",
                "ok": True,
                "skipped": True,
                "checks": [],
                "msg": "本步无预期，已跳过校验",
                "screenshot": op_shot,
                "page_context": {},
                "plans": [],
            }
        elif expected_text and not action_ok:
            expected_part = {
                "text": expected_text,
                "ok": True,
                "skipped": True,
                "checks": [],
                "msg": "前置操作未成功，已跳过预期校验",
                "screenshot": op_shot,
                "page_context": {},
                **locate_meta,
            }
        elif expected_text:
            verify_one = _verify_step_expected(
                expected_text,
                action_block.get("step_results") or [],
                sn=sn,
                platform=platform,
                package=package,
                app_id=str(context.get("app_id") or context.get("appId") or ""),
                case=case,
                run_id=run_id,
                step_index=i,
                case_id=str(case.get("case_id") or i),
                icon_targets=icon_targets,
            )
            expected_part = {
                "text": expected_text,
                "thought": (verify_one.get("plan_tree") or {}).get("thought") or "",
                "plans": (verify_one.get("plan_tree") or {}).get("plans") or [],
                "plan_log": verify_one.get("plan_log") or [],
                "ok": verify_one.get("ok"),
                "checks": verify_one.get("checks"),
                "msg": verify_one.get("msg"),
                "screen_preview": verify_one.get("screen_text_preview"),
                "screenshot": verify_one.get("screenshot"),
                "page_context": verify_one.get("page_context") or {},
                "page_recovery": verify_one.get("page_recovery"),
            }

        step_block["expected_action"] = expected_part
        step_block["expected"] = expected_part
        step_block["action_ok"] = action_ok
        step_block["expected_ok"] = (
            True if expected_part.get("skipped") else expected_part.get("ok", True)
        )
        step_block["ok"] = action_ok and step_block["expected_ok"]
        trace.append(step_block)

        expected_verify_failed = (
            bool(expected_text)
            and action_ok
            and not expected_part.get("skipped")
            and not expected_part.get("ok", True)
        )

        if not action_ok:
            part = f"步骤{i + 1}操作失败: {action_block.get('msg') or ''}"
            fail_msg = f"{fail_msg}；{part}" if fail_msg else part
            overall_ok = False
            break

        if expected_verify_failed:
            part = f"步骤{i + 1}预期未达成: {expected_part.get('msg') or ''}"
            fail_msg = f"{fail_msg}；{part}" if fail_msg else part
            overall_ok = False

        if pause_on_clarification and not action_ok:
            try:
                from server.services.execution_clarification_service import (
                    build_login_icon_clarification,
                    needs_clarification_for_step,
                )

                if needs_clarification_for_step(step_line, action_block):
                    clarification = build_login_icon_clarification(
                        sn=sn,
                        platform=platform,
                        app_id=str(context.get("app_id") or context.get("appId") or ""),
                        step_text=step_line,
                        action_block=action_block,
                        run_id=run_id,
                        case_name=str(case.get("name") or case.get("case_id") or ""),
                    )
                    if clarification:
                        return {
                            "trace": trace,
                            "step_results": all_results,
                            "status": "awaiting_clarification",
                            "msg": fail_msg or "等待人工确认",
                            "overall_ok": False,
                            "clarification": clarification,
                            "resume_state": {"step_index": i},
                        }
            except Exception as e:
                SLog.w(TAG, f"build clarification failed: {e}")

        # 仅「有预期且校验失败」时中断；无预期 / 跳过预期校验时继续后续步骤
        if expected_verify_failed:
            break

    if overall_ok:
        status = "pass"
        msg = "全部步骤与预期校验通过"
    else:
        status = "fail"
        msg = fail_msg or "存在失败步骤"

    return {
        "trace": trace,
        "step_results": all_results,
        "status": status,
        "msg": msg,
        "overall_ok": overall_ok,
    }


def _case_matches_platform(case: Dict[str, Any], platform: str) -> bool:
    p = (case.get("platform") or "").strip().lower()
    if not p or "双端" in p or "all" in p:
        return True
    if platform == "android":
        return "android" in p or "安卓" in p
    if platform == "ios":
        return "ios" in p or "苹果" in p
    return True


def _case_allows_background(case: Dict[str, Any]) -> bool:
    return aas._case_allows_background(case)


def run_cases(
    app,
    *,
    sn: str,
    platform: str = "android",
    case_ids: Optional[List[str]] = None,
    start_index: int = 0,
    db: Optional[Session] = None,
    use_cache: bool = True,
    async_exec: bool = True,
) -> Dict[str, Any]:
    if use_cache:
        payload = list_cases_for_app(app, refresh=False)
    else:
        payload = fetch_cases_for_app(app, persist=True)
    all_cases = payload.get("cases") or []
    if case_ids:
        by_id = {c.get("case_id"): c for c in all_cases if c.get("case_id")}
        cases = [by_id[cid] for cid in case_ids if cid in by_id]
        missing = [cid for cid in case_ids if cid not in by_id]
        if missing:
            SLog.w(TAG, f"run_cases: {len(missing)} case_id not found: {missing[:5]}")
    else:
        # 全量执行：包含 iOS 专用用例等，平台不匹配时由前置条件 skip 并展示在列表中
        cases = list(all_cases)
    if start_index > 0:
        cases = cases[start_index:]

    if db is not None:
        try:
            from server.services.execution_clarification_service import ensure_default_login_icon_templates

            ensure_default_login_icon_templates(db, app.id)
        except Exception as e:
            SLog.w(TAG, f"seed login icon templates failed: {e}")

    icon_targets = _load_icon_targets(db, app.id) or aas.get_icon_targets(app)
    device_skills = aas.get_skills_for_device(app, sn)
    env_profile = aas.resolve_env_profile(app)
    package = aas.package_for_app(app, env_profile)
    if not package:
        SLog.w(TAG, f"run_cases: app={app.id} name={app.name} profile={env_profile} 未解析到 Android 包名")
    else:
        SLog.i(TAG, f"run_cases: app={app.id} sn={sn} package={package} profile={env_profile}")

    if platform == "android" and sn:
        try:
            from driver.agent.Crawl.device_bootstrap import wait_for_adb_device

            wait_for_adb_device(sn, platform, timeout=15.0)
        except Exception as e:
            SLog.w(TAG, f"run_cases: device not ready sn={sn}: {e}")
            fail_id = uuid.uuid4().hex[:12]
            return {
                "run_id": fail_id,
                "app_id": app.id,
                "app_name": app.name,
                "sn": sn,
                "platform": platform,
                "env_profile": env_profile,
                "package": package,
                "started_at": datetime.now().isoformat(),
                "finished_at": datetime.now().isoformat(),
                "status": "failed",
                "total": len(cases),
                "passed": 0,
                "failed": len(cases),
                "skipped": 0,
                "cases": [],
                "error": str(e),
            }

    context = {
        "app_id": app.id,
        "app_name": app.name,
        "env_profile": env_profile,
        "package": package,
        "platform": platform,
    }

    run_id = uuid.uuid4().hex[:12]
    try:
        from server.services.regression_run_context import begin_run

        begin_run(
            run_id=run_id,
            sn=sn,
            platform=platform,
            capture_screenshots=True,
        )
    except Exception as e:
        SLog.w(TAG, f"gesture audit context init failed: {e}")
    run_doc: Dict[str, Any] = {
        "run_id": run_id,
        "app_id": app.id,
        "app_name": app.name,
        "sn": sn,
        "platform": platform,
        "env_profile": env_profile,
        "package": package,
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "status": "running",
        "total": len(cases),
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "cases": [],
    }
    _RUNS[run_id] = run_doc
    if db is not None:
        aas.persist_run_start(
            db,
            run_id=run_id,
            app_id=app.id,
            sn=sn,
            platform=platform,
            total=len(cases),
        )

    if async_exec:
        _spawn_background_run(
            app_id=app.id,
            run_id=run_id,
            cases=cases,
            sn=sn,
            platform=platform,
            context=context,
            icon_targets=icon_targets,
            device_skills=device_skills,
            package=package,
            env_profile=env_profile,
        )
        return _client_run_snapshot(run_doc)

    _execute_cases_batch(
        app,
        cases,
        run_doc,
        sn=sn,
        platform=platform,
        context=context,
        icon_targets=icon_targets,
        device_skills=device_skills,
        package=package,
        env_profile=env_profile,
        db=db,
        run_id=run_id,
    )

    return _finalize_run_doc(run_doc, db)


def _client_run_snapshot(run_doc: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "run_id": run_doc.get("run_id"),
        "app_id": run_doc.get("app_id"),
        "app_name": run_doc.get("app_name"),
        "sn": run_doc.get("sn"),
        "platform": run_doc.get("platform"),
        "status": run_doc.get("status") or "running",
        "total": run_doc.get("total") or 0,
        "passed": run_doc.get("passed") or 0,
        "failed": run_doc.get("failed") or 0,
        "skipped": run_doc.get("skipped") or 0,
        "started_at": run_doc.get("started_at"),
        "finished_at": run_doc.get("finished_at"),
        "cases": list(run_doc.get("cases") or []),
    }


def _spawn_background_run(
    *,
    app_id: str,
    run_id: str,
    cases: List[Dict[str, Any]],
    sn: str,
    platform: str,
    context: Dict[str, Any],
    icon_targets: List[Dict[str, Any]],
    device_skills: Dict[str, Any],
    package: str,
    env_profile: str,
) -> None:
    t = threading.Thread(
        target=_background_run_worker,
        kwargs={
            "app_id": app_id,
            "run_id": run_id,
            "cases": cases,
            "sn": sn,
            "platform": platform,
            "context": context,
            "icon_targets": icon_targets,
            "device_skills": device_skills,
            "package": package,
            "env_profile": env_profile,
        },
        name=f"feishu-run-{run_id}",
        daemon=True,
    )
    t.start()


def _background_run_worker(
    *,
    app_id: str,
    run_id: str,
    cases: List[Dict[str, Any]],
    sn: str,
    platform: str,
    context: Dict[str, Any],
    icon_targets: List[Dict[str, Any]],
    device_skills: Dict[str, Any],
    package: str,
    env_profile: str,
) -> None:
    from server.core.database import SessionLocal
    from server.models.project import App

    db = SessionLocal()
    run_doc = _RUNS.get(run_id)
    try:
        from server.services.regression_run_context import begin_run, get_ctx

        if not get_ctx():
            begin_run(
                run_id=run_id,
                sn=sn,
                platform=platform,
                capture_screenshots=True,
            )
        app = db.query(App).filter(App.id == app_id).first()
        if not app or not run_doc:
            SLog.e(TAG, f"background run aborted run_id={run_id} app={app_id}")
            return
        _execute_cases_batch(
            app,
            cases,
            run_doc,
            sn=sn,
            platform=platform,
            context=context,
            icon_targets=icon_targets,
            device_skills=device_skills,
            package=package,
            env_profile=env_profile,
            db=db,
            run_id=run_id,
        )
        _finalize_run_doc(run_doc, db)
    except Exception as e:
        SLog.e(TAG, f"background run failed run_id={run_id}: {e}")
        if run_doc:
            run_doc["status"] = "failed"
            run_doc["error"] = str(e)
            run_doc["finished_at"] = datetime.now().isoformat()
            try:
                aas.persist_run_finish(db, run_doc)
            except Exception:
                pass
            _RUNS[run_id] = run_doc
    finally:
        db.close()


def _finalize_run_doc(run_doc: Dict[str, Any], db: Optional[Session] = None) -> Dict[str, Any]:
    try:
        from server.services.regression_run_context import end_run

        run_doc["gesture_log"] = end_run()
    except Exception:
        run_doc["gesture_log"] = []
    run_doc["finished_at"] = datetime.now().isoformat()
    run_doc["executed"] = len(run_doc.get("cases") or [])
    try:
        started = run_doc.get("started_at") or ""
        if started:
            t0 = datetime.fromisoformat(started)
            t1 = datetime.fromisoformat(run_doc["finished_at"])
            run_doc["duration_ms"] = max(0, int((t1 - t0).total_seconds() * 1000))
    except Exception:
        pass
    try:
        from driver.agent.Crawl.device_bootstrap import clear_engine_cache

        clear_engine_cache(run_doc.get("sn") or "", run_doc.get("platform") or "android")
    except Exception:
        pass
    if run_doc.get("status") == "running":
        run_doc["status"] = "done"
    if db is not None:
        aas.persist_run_finish(db, run_doc)
    rid = run_doc.get("run_id")
    if rid:
        if db is not None and run_doc.get("status") not in (
            "running",
            "awaiting_clarification",
        ):
            _RUNS[rid] = _slim_run_for_memory_cache(run_doc)
        else:
            _RUNS[rid] = run_doc
        _prune_runs_cache()
    return run_doc


def _execute_cases_batch(
    app,
    cases: List[Dict[str, Any]],
    run_doc: Dict[str, Any],
    *,
    sn: str,
    platform: str,
    context: Dict[str, Any],
    icon_targets: List[Dict[str, Any]],
    device_skills: Dict[str, Any],
    package: str,
    env_profile: str,
    db: Optional[Session],
    run_id: str,
) -> bool:
    """执行一批用例；若因人工确认暂停则返回 True。"""
    import time as _time

    for raw_case in cases:
        case = normalize_feishu_case(raw_case)
        case_started = _time.time()
        try:
            from server.services.regression_run_context import begin_case

            begin_case()
        except Exception:
            pass
        item: Dict[str, Any] = {
            "case_id": case.get("case_id"),
            "name": case.get("name"),
            "module": case.get("module"),
            "row_index": case.get("row_index"),
            "status": "running",
            "command": "",
            "steps_raw": case.get("steps_raw") or "",
            "precondition_raw": case.get("precondition") or "",
            "expected_raw": case.get("expected_raw") or "",
            "step_lines": list(case.get("steps") or []),
            "steps": list(case.get("steps") or []),
            "step_nums": list(case.get("step_nums") or []),
            "expected_lines": list(case.get("expected") or []),
            "expected": list(case.get("expected") or []),
            "expected_nums": list(case.get("expected_nums") or []),
            "expected_by_step": dict(case.get("expected_by_step") or {}),
            "expected_by_step": dict(case.get("expected_by_step") or {}),
            "step_results": [],
            "execution_trace": [],
            "verify": {},
            "msg": "",
        }
        run_doc["cases"].append(item)

        trace: List[Dict[str, Any]] = []
        all_results: List[Dict[str, Any]] = []

        pre_cmd = _skills_to_command(device_skills.get("pre") or [])
        if pre_cmd:
            pre_block = _run_command_block(
                pre_cmd,
                sn=sn,
                platform=platform,
                context=context,
                icon_targets=icon_targets,
                phase="skill_pre",
                run_id=run_id,
            )
            trace.append(
                {
                    "phase": "skill_pre",
                    "title": "前置 Skills",
                    "command": pre_cmd,
                    "plan_log": pre_block.get("plan_log"),
                    "execute_log": pre_block.get("execute_log"),
                    "ok": pre_block.get("ok"),
                }
            )
            all_results.extend(pre_block.get("step_results") or [])
            if not pre_block.get("ok"):
                item["status"] = "fail"
                item["msg"] = f"前置 Skills 失败: {pre_block.get('msg')}"
                item["execution_trace"] = trace
                run_doc["failed"] += 1
                _stamp_case_duration(item, case_started)
                continue

        step_lines = list(case.get("steps") or [])
        item["command"] = _steps_to_command(case)
        context["platform"] = platform
        context.pop("startup_overlay_recovery", None)
        context.pop("skip_overlay_clear", None)
        context.pop("system_permission_recovery", None)

        # 设备准备优先于前置校验，避免重复 bootstrap 且侧栏时间戳反映真实顺序
        _append_device_prep_trace(sn=sn, platform=platform, run_id=run_id, trace=trace)

        pre_before_items: List[Dict[str, Any]] = []
        raw_pre = (case.get("precondition") or "").strip()
        if raw_pre:
            before_res = run_preconditions(
                raw_pre,
                sn=sn,
                platform=platform,
                package=package,
                phase="before_launch",
            )
            pre_before_items = list(before_res.get("items") or [])
            if not before_res.get("ok"):
                _append_precondition_trace(
                    case,
                    before_items=pre_before_items,
                    after_items=[],
                    ok=False,
                    trace=trace,
                )
                item["status"] = "skip"
                item["msg"] = before_res.get("msg") or "前置条件不满足"
                if not _case_matches_platform(case, platform):
                    item["msg"] = (
                        f"当前设备为 {platform}，本用例需要 "
                        f"{case.get('platform') or '其他平台'}："
                        f"{before_res.get('msg') or '前置条件不满足'}"
                    )
                item["execution_trace"] = trace
                run_doc["skipped"] = int(run_doc.get("skipped") or 0) + 1
                _stamp_case_duration(item, case_started)
                continue
            # 前置条件 trace 统一在拉起应用后合并写入，避免 before_launch 与 after_launch 各写一条导致回放重复

        app_cache_cleared = precondition_cleared_app_cache(pre_before_items)
        context["app_cache_cleared"] = app_cache_cleared

        if not step_lines and not item["command"]:
            item["status"] = "skip"
            item["msg"] = "无有效步骤"
            item["execution_trace"] = trace
            _stamp_case_duration(item, case_started)
            continue

        # 每条用例仅拉起一次：放在 pre skills 之后、步骤执行之前
        if not package:
            trace.append(
                {
                    "phase": "foreground",
                    "title": "拉起被测应用",
                    "entries": [
                        {
                            "type": "error",
                            "text": f"未配置 Android 包名（环境 profile={env_profile}），请在应用配置 → 执行环境 中填写",
                            "ok": False,
                        }
                    ],
                }
            )
            item["status"] = "fail"
            item["msg"] = "未配置 Android 包名，无法拉起被测应用"
            item["execution_trace"] = trace
            run_doc["failed"] += 1
            _stamp_case_duration(item, case_started)
            continue

        startup_recovery: Optional[Dict[str, Any]] = None
        if not _case_allows_background(case):
            fg = aas.ensure_app_foreground(sn, package, platform)
            _append_foreground_trace(
                trace,
                fg,
                run_id=run_id,
                sn=sn,
                platform=platform,
            )
            if not fg.get("ok"):
                item["status"] = "fail"
                item["msg"] = fg.get("msg") or "拉起被测应用失败"
                item["execution_trace"] = trace
                run_doc["failed"] += 1
                _stamp_case_duration(item, case_started)
                continue

            if app_cache_cleared:
                context["requires_fresh_startup"] = True
                SLog.i(
                    TAG,
                    "fresh startup after cache clear — overlay guard runs inside each Plan step",
                )

        needs_after_launch = has_precondition_phase(case.get("precondition") or "", "after_launch")
        if needs_after_launch and package and platform == "android":
            if _case_allows_background(case):
                fg2 = aas.ensure_app_foreground(sn, package, platform)
                trace.append(
                    {
                        "phase": "foreground",
                        "title": "拉起被测应用（前置校验）",
                        "entries": [{"type": "info", "text": fg2.get("msg"), "ok": fg2.get("ok")}],
                    }
                )
                if not fg2.get("ok"):
                    item["status"] = "fail"
                    item["msg"] = fg2.get("msg") or "拉起被测应用失败"
                    item["execution_trace"] = trace
                    run_doc["failed"] += 1
                    _stamp_case_duration(item, case_started)
                    continue
        pre_after_items: List[Dict[str, Any]] = []
        if raw_pre:
            after_res = run_preconditions(
                raw_pre,
                sn=sn,
                platform=platform,
                package=package,
                phase="after_launch",
            )
            pre_after_items = list(after_res.get("items") or [])
            _append_precondition_trace(
                case,
                before_items=pre_before_items,
                after_items=pre_after_items,
                ok=bool(after_res.get("ok")),
                trace=trace,
            )
            if not after_res.get("ok"):
                item["status"] = "skip"
                item["msg"] = after_res.get("msg") or "前置条件不满足"
                item["execution_trace"] = trace
                run_doc["skipped"] = int(run_doc.get("skipped") or 0) + 1
                _stamp_case_duration(item, case_started)
                continue

        if step_lines:
            seq = _run_case_steps_sequential(
                case,
                sn=sn,
                platform=platform,
                context=context,
                icon_targets=icon_targets,
                package=package,
                run_id=run_id,
                pause_on_clarification=False,
            )
            trace.extend(seq.get("trace") or [])
            all_results.extend(seq.get("step_results") or [])
            item["step_results"] = all_results
            item["verify"] = {
                "status": seq.get("status"),
                "msg": seq.get("msg"),
                "checks": [
                    {
                        "text": b.get("expected_text") or (b.get("expected") or {}).get("text"),
                        "ok": (b.get("expected") or {}).get("ok"),
                        "reason": (b.get("expected") or {}).get("msg"),
                    }
                    for b in (seq.get("trace") or [])
                    if b.get("phase") == "case_step" and (b.get("expected") or {}).get("text")
                ],
            }
            item["status"] = seq.get("status", "fail")
            item["msg"] = seq.get("msg", "")

            if seq.get("status") == "awaiting_clarification":
                item["execution_trace"] = trace
                item["clarification"] = seq.get("clarification") or {}
                item["msg"] = seq.get("msg") or "步骤失败，需人工确认图标/知识库"
                item["status"] = "fail"
                _stamp_case_duration(item, case_started)
                run_doc["failed"] += 1
                SLog.w(
                    TAG,
                    f"case {case.get('case_id')} needs clarification; continue batch",
                )
                continue
        else:
            command = item["command"]
            plan = cs.plan_message(command, sn=sn, context=context)
            plan_log = aas.build_plan_log(command, plan)
            trace.append(
                {
                    "phase": "plan",
                    "title": "步骤拆解",
                    "command": command,
                    "plan_log": plan_log,
                    "reply": plan.get("reply"),
                }
            )

            if plan.get("error") or not plan.get("steps"):
                item["status"] = "fail"
                item["msg"] = plan.get("reply") or plan.get("error") or "规划失败"
                item["execution_trace"] = trace
                run_doc["failed"] += 1
                _stamp_case_duration(item, case_started)
                continue

            shot_before = _capture_step_screenshot(
                sn, platform, run_id=run_id, tag=f"case_{case.get('case_id')}_before"
            )
            results = cs.execute_steps(
                plan.get("steps") or [],
                sn=sn,
                platform=platform,
                icon_targets=icon_targets,
                run_id=run_id,
                capture_screenshots=True,
                app_id=app.id,
            )
            all_results.extend(results)
            item["step_results"] = results
            trace.append(
                {
                    "phase": "execute",
                    "title": "执行动作",
                    "screenshot_before": shot_before,
                    "execute_log": aas.build_execute_log(results),
                }
            )

            verify = _verify_case(
                case,
                all_results,
                sn=sn,
                platform=platform,
                package=package,
                app_id=str(app.id),
            )
            item["verify"] = verify
            trace.append(
                {
                    "phase": "verify",
                    "title": "预期校验",
                    "entries": verify.get("checks") or [],
                    "screen_preview": verify.get("screen_text_preview"),
                }
            )
            item["status"] = verify.get("status", "fail")
            item["msg"] = verify.get("msg", "")

        post_cmd = _skills_to_command(device_skills.get("post") or [])
        if post_cmd:
            post_block = _run_command_block(
                post_cmd,
                sn=sn,
                platform=platform,
                context=context,
                icon_targets=icon_targets,
                phase="skill_post",
                run_id=run_id,
            )
            trace.append(
                {
                    "phase": "skill_post",
                    "title": "后置 Skills",
                    "command": post_cmd,
                    "plan_log": post_block.get("plan_log"),
                    "execute_log": post_block.get("execute_log"),
                    "ok": post_block.get("ok"),
                }
            )
            all_results.extend(post_block.get("step_results") or [])

        item["execution_trace"] = trace
        _stamp_case_duration(item, case_started)

        if item["status"] == "pass":
            run_doc["passed"] += 1
        elif item["status"] not in ("awaiting_clarification", "skip"):
            run_doc["failed"] += 1

        if db is not None:
            try:
                aas.persist_run_progress(db, run_doc)
            except Exception as e:
                SLog.w(TAG, f"persist_run_progress failed run={run_id}: {e}")

    return False


def clarify_and_resume_run(
    run_id: str,
    answer: Dict[str, Any],
    db: Session,
) -> Dict[str, Any]:
    """用户确认澄清后写入图标/知识库，并从失败步骤继续执行。"""
    from server.models.project import App
    from server.services.execution_clarification_service import apply_clarification_answer

    run_doc = get_run(run_id, db)
    if not run_doc:
        raise ValueError("执行记录不存在")
    if run_doc.get("status") != "awaiting_clarification":
        raise ValueError("当前执行不在等待确认状态")

    pending = run_doc.get("pending_clarification") or {}
    resume = run_doc.get("resume_state") or {}
    app_id = run_doc.get("app_id") or ""
    apply_clarification_answer(db, app_id, pending, answer)

    app = db.query(App).filter(App.id == app_id).first()
    if not app:
        raise ValueError("应用不存在")

    icon_targets = _load_icon_targets(db, app.id) or aas.get_icon_targets(app)
    sn = run_doc["sn"]
    platform = run_doc["platform"]
    env_profile = run_doc.get("env_profile") or "test"
    package = run_doc.get("package") or aas.package_for_app(app, env_profile)
    context = dict(resume.get("context") or {})
    device_skills = resume.get("device_skills") or aas.get_skills_for_device(app, sn)

    case = resume.get("case") or {}
    item_idx = int(resume.get("case_item_index") or 0)
    item = run_doc["cases"][item_idx]
    step_index = int(resume.get("step_index") or 0)
    partial_trace = list(resume.get("trace") or [])
    retry_trace = partial_trace[:-1] if partial_trace else []

    run_doc["status"] = "running"
    run_doc.pop("pending_clarification", None)

    seq = _run_case_steps_sequential(
        case,
        sn=sn,
        platform=platform,
        context=context,
        icon_targets=icon_targets,
        package=package or "",
        run_id=run_id,
        start_step_index=step_index,
        initial_trace=retry_trace,
        initial_all_results=resume.get("step_results") or [],
    )

    item["step_results"] = seq.get("step_results") or []
    item["verify"] = {
        "status": seq.get("status"),
        "msg": seq.get("msg"),
        "checks": [
            {
                "text": b.get("expected_text") or (b.get("expected") or {}).get("text"),
                "ok": (b.get("expected") or {}).get("ok"),
                "reason": (b.get("expected") or {}).get("msg"),
            }
            for b in (seq.get("trace") or [])
            if b.get("phase") == "case_step" and (b.get("expected") or {}).get("text")
        ],
    }
    item["status"] = seq.get("status", "fail")
    item["msg"] = seq.get("msg", "")
    item["execution_trace"] = seq.get("trace") or retry_trace

    if seq.get("status") == "awaiting_clarification":
        run_doc["status"] = "awaiting_clarification"
        run_doc["pending_clarification"] = seq.get("clarification") or {}
        run_doc["resume_state"] = {
            **resume,
            "step_index": (seq.get("resume_state") or {}).get("step_index", step_index),
            "trace": list(item["execution_trace"] or []),
            "step_results": list(item["step_results"] or []),
            "context": dict(context),
            "device_skills": device_skills,
        }
        aas.persist_run_pause(db, run_doc)
        _RUNS[run_id] = run_doc
        return run_doc

    if item["status"] == "pass":
        run_doc["passed"] += 1
    else:
        run_doc["failed"] += 1

    run_doc.pop("resume_state", None)
    remaining = resume.get("remaining_cases") or []
    if remaining:
        if _execute_cases_batch(
            app,
            remaining,
            run_doc,
            sn=sn,
            platform=platform,
            context=context,
            icon_targets=icon_targets,
            device_skills=device_skills,
            package=package or "",
            env_profile=env_profile,
            db=db,
            run_id=run_id,
        ):
            _RUNS[run_id] = run_doc
            return run_doc

    return _finalize_run_doc(run_doc, db)


def get_run(run_id: str, db: Optional[Session] = None) -> Optional[Dict[str, Any]]:
    mem = _RUNS.get(run_id)
    if mem and mem.get("status") in ("running", "awaiting_clarification"):
        return mem
    if db is not None:
        row = aas.get_run_from_db(db, run_id)
        if row:
            return row
    return mem
