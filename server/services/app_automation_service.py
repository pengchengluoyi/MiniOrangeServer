# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""应用自动化配置：Skills、无字图标目标、用例缓存、执行记录。"""
from __future__ import annotations

import copy
import re
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from script.log import SLog
from server.models.app_regression_run import AppRegressionRun
from server.models.project import App
from server.services.project_env import load_project_env, profile_snapshot, resolve_profile_name

TAG = "AppAutomation"

DEFAULT_AUTOMATION = {
    "env_profile": "test",
    "execution_env": {"mode": "fixed", "profile": "test"},
    "skills": {
        "default": {"pre": [], "post": []},
        "devices": {},
    },
    "icon_targets": [],
}


def _app_env(app) -> dict:
    return dict(app.env) if isinstance(app.env, dict) else {}


def get_automation_config(app) -> Dict[str, Any]:
    env = _app_env(app)
    raw = env.get("automation") if isinstance(env.get("automation"), dict) else {}
    out = copy.deepcopy(DEFAULT_AUTOMATION)
    if raw.get("env_profile"):
        out["env_profile"] = raw["env_profile"]
    ex = raw.get("execution_env")
    if isinstance(ex, dict):
        out["execution_env"] = {
            "mode": ex.get("mode") or "fixed",
            "profile": ex.get("profile") or out["env_profile"],
        }
    skills = raw.get("skills") if isinstance(raw.get("skills"), dict) else {}
    if isinstance(skills.get("default"), dict):
        out["skills"]["default"] = {
            "pre": list(skills["default"].get("pre") or []),
            "post": list(skills["default"].get("post") or []),
        }
    devices = skills.get("devices") if isinstance(skills.get("devices"), dict) else {}
    out["skills"]["devices"] = {
        k: {
            "pre": list((v or {}).get("pre") or []),
            "post": list((v or {}).get("post") or []),
        }
        for k, v in devices.items()
    }
    icons = raw.get("icon_targets")
    if isinstance(icons, list):
        out["icon_targets"] = [x for x in icons if isinstance(x, dict)]
    figma = raw.get("figma")
    if isinstance(figma, dict):
        out["figma"] = {
            "file_url": (figma.get("file_url") or "").strip(),
            "file_key": (figma.get("file_key") or "").strip(),
            "last_sync_at": figma.get("last_sync_at") or "",
            "pages_summary": figma.get("pages_summary") or [],
            "logic": figma.get("logic") if isinstance(figma.get("logic"), dict) else {},
            "logic_applied_at": figma.get("logic_applied_at") or "",
            "login_frame": figma.get("login_frame") if isinstance(figma.get("login_frame"), dict) else {},
            "login_reference": figma.get("login_reference") if isinstance(figma.get("login_reference"), dict) else {},
        }
    return out


def save_automation_config(app, config: Dict[str, Any]) -> Dict[str, Any]:
    env = _app_env(app)
    current = get_automation_config(app)
    if config.get("env_profile"):
        current["env_profile"] = str(config["env_profile"]).strip() or "test"
    if "execution_env" in config and isinstance(config["execution_env"], dict):
        current["execution_env"] = config["execution_env"]
        if config["execution_env"].get("profile"):
            current["env_profile"] = config["execution_env"]["profile"]
    if "skills" in config and isinstance(config["skills"], dict):
        current["skills"] = config["skills"]
    if "icon_targets" in config and isinstance(config["icon_targets"], list):
        normalized = []
        for item in config["icon_targets"]:
            if not isinstance(item, dict):
                continue
            name = (item.get("name") or "").strip()
            if not name:
                continue
            normalized.append(
                {
                    "id": item.get("id") or uuid.uuid4().hex[:10],
                    "name": name,
                    "x": int(item.get("x") or 0),
                    "y": int(item.get("y") or 0),
                    "w": int(item.get("w") or 0),
                    "h": int(item.get("h") or 0),
                    "note": (item.get("note") or "").strip(),
                }
            )
        current["icon_targets"] = normalized
    if "figma" in config and isinstance(config["figma"], dict):
        prev_figma = current.get("figma") or {}
        incoming = config["figma"]
        current["figma"] = {
            "file_url": (incoming.get("file_url") or "").strip(),
            "file_key": (incoming.get("file_key") or "").strip(),
            "last_sync_at": incoming.get("last_sync_at") or prev_figma.get("last_sync_at") or "",
            "pages_summary": incoming.get("pages_summary") or prev_figma.get("pages_summary") or [],
            "logic": incoming.get("logic") if isinstance(incoming.get("logic"), dict) else (prev_figma.get("logic") or {}),
            "logic_applied_at": incoming.get("logic_applied_at") or prev_figma.get("logic_applied_at") or "",
            "login_frame": incoming.get("login_frame")
            if isinstance(incoming.get("login_frame"), dict)
            else (prev_figma.get("login_frame") or {}),
            "login_reference": incoming.get("login_reference")
            if isinstance(incoming.get("login_reference"), dict)
            else (prev_figma.get("login_reference") or {}),
        }
    env["automation"] = current
    app.env = env
    flag_modified(app, "env")
    return current


def get_skills_for_device(app, sn: str) -> Dict[str, List[str]]:
    cfg = get_automation_config(app)
    skills = cfg.get("skills") or {}
    default = skills.get("default") or {"pre": [], "post": []}
    dev_map = skills.get("devices") or {}
    dev = dev_map.get(sn) or {}
    pre = list(default.get("pre") or []) + list(dev.get("pre") or [])
    post = list(dev.get("post") or []) + list(default.get("post") or [])
    return {"pre": pre, "post": post}


def get_icon_targets(app) -> List[Dict[str, Any]]:
    return list(get_automation_config(app).get("icon_targets") or [])


def get_login_icon_order(app) -> Dict[str, int]:
    """
    登录页底部图标行槽位（左→右，兜底用）。
    未安装微信等导致图标缺失时，应优先靠无障碍语义匹配；仅当四入口齐全或你明确校准过槽位时才可靠。
    """
    raw = get_automation_config(app).get("login_icon_order")
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, int] = {}
    for key in ("wechat", "phone_sms", "email_password", "apple"):
        if key in raw:
            try:
                out[key] = int(raw[key])
            except (TypeError, ValueError):
                pass
    return out


def resolve_env_profile(app, override: Optional[str] = None) -> str:
    cfg = get_automation_config(app)
    feishu = (_app_env(app).get("feishu") or {}) if isinstance(_app_env(app).get("feishu"), dict) else {}
    ex = cfg.get("execution_env") or {}
    mode = ex.get("mode") or "fixed"
    if mode == "project_default":
        doc = load_project_env_for_app(app)
        return resolve_profile_name(doc, None)
    if mode == "task_param" and override:
        return resolve_profile_name(load_project_env_for_app(app), override)
    profile = ex.get("profile") or cfg.get("env_profile") or feishu.get("env_profile")
    return resolve_profile_name(load_project_env_for_app(app), override or profile)


def load_project_env_for_app(app) -> dict:
    project = getattr(app, "project", None)
    if not project:
        return {"default_profile": "test", "profiles": {}}
    from server.services.project_env import normalize_project_env

    return normalize_project_env(project.env)


def package_for_app(app, env_profile: Optional[str] = None) -> str:
    profile = resolve_env_profile(app, env_profile)
    snap = profile_snapshot(load_project_env_for_app(app), profile)
    android = snap.get("android") if isinstance(snap.get("android"), dict) else {}
    pkg = (android.get("package") or "").strip()
    if pkg:
        return pkg
    legacy = _app_env(app)
    if isinstance(legacy.get("android"), dict):
        pkg = (legacy["android"].get("package") or "").strip()
        if pkg:
            return pkg
    pkg = (legacy.get("package") or "").strip()
    if pkg:
        return pkg
    try:
        from server.services.copilot_service import _package_for_app_record

        pkg = (_package_for_app_record(app) or "").strip()
        if pkg:
            SLog.i(TAG, f"package_for_app fallback via app record: {pkg}")
            return pkg
    except Exception as e:
        SLog.w(TAG, f"package_for_app record fallback failed: {e}")
    env_doc = load_project_env_for_app(app)
    for prof in ("test", "dev", "pre", "prod"):
        snap2 = profile_snapshot(env_doc, prof)
        pkg = ((snap2.get("android") or {}).get("package") or "").strip()
        if pkg:
            SLog.i(TAG, f"package_for_app fallback profile={prof}: {pkg}")
            return pkg
    return ""


def save_feishu_cases_cache(app, payload: Dict[str, Any]) -> Dict[str, Any]:
    env = _app_env(app)
    cache = {
        "synced_at": datetime.now().isoformat(),
        "total": payload.get("total") or len(payload.get("cases") or []),
        "cases": payload.get("cases") or [],
        "sheet_meta": payload.get("sheet_meta") or {},
        "resolve_note": payload.get("resolve_note") or "",
    }
    env["feishu_cases_cache"] = cache
    app.env = env
    flag_modified(app, "env")
    return cache


def get_feishu_cases_cache(app) -> Optional[Dict[str, Any]]:
    cache = _app_env(app).get("feishu_cases_cache")
    return cache if isinstance(cache, dict) else None


def list_cached_feishu_cases(app) -> List[Dict[str, Any]]:
    cache = get_feishu_cases_cache(app)
    if not cache:
        return []
    return list(cache.get("cases") or [])


def _case_allows_background(case: Dict[str, Any]) -> bool:
    blob = " ".join(
        [
            case.get("precondition") or "",
            case.get("steps_raw") or "",
            " ".join(case.get("steps") or []),
        ]
    )
    return bool(re.search(r"后台|切到|切换应用|回到桌面|按Home|home键", blob, re.I))


def _engine_current_package(engine) -> str:
    try:
        fn = getattr(engine, "current_package", None)
        if callable(fn):
            pkg = fn()
            if pkg:
                return str(pkg).strip()
        ensure_u2 = getattr(engine, "_ensure_u2", None)
        if not callable(ensure_u2):
            return ""
        d = ensure_u2()
        if not d:
            return ""
        info = d.app_current() or {}
        return str(info.get("package") or "").strip()
    except Exception as e:
        SLog.w(TAG, f"read foreground package failed: {e}")
        return ""


def _pkg_matches(expected: str, actual: str) -> bool:
    if not actual:
        return False
    e, a = expected.strip(), actual.strip()
    return e == a or e in a or a in e


def guard_test_app_foreground(
    sn: str,
    package: str,
    platform: str = "android",
    *,
    phase: str = "",
) -> Dict[str, Any]:
    """
    观察被测应用是否在前台：仅记录离屏，不再自动 start_app 拉回。
    跨 App 授权（微信登录等）期间应保持当前前台应用。
    """
    if not package or not sn:
        return {"ok": True, "msg": "skip", "guarded": False, "drift": False}
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
        from server.services.regression_run_context import (
            close_foreground_drift_segment,
            record_foreground_drift,
        )

        builtins.TARGET_DEVICE_SN = sn
        engine, _ = bootstrap_mobile_engine(sn, platform)
        before = _engine_current_package(engine)
        if _pkg_matches(package, before):
            close_foreground_drift_segment()
            return {
                "ok": True,
                "msg": f"前台已是 {package}",
                "guarded": False,
                "drift": False,
                "foreground_before": before,
                "foreground_after": before,
            }

        try:
            from server.services.locate.app_packages import resolve_known_app_by_package

            known = resolve_known_app_by_package(before)
            actual_name = known.name if known else (before or "未知")
        except Exception:
            actual_name = before or "未知"

        record_foreground_drift(
            expected_package=package,
            actual_package=before,
            phase=phase,
            note=f"当前前台为 {actual_name}",
        )
        msg = (
            f"被测应用不在前台：当前 {actual_name}（{before or '-'}），"
            f"已记录离屏，未自动拉回"
        )
        SLog.i(TAG, f"foreground drift observe sn={sn} expected={package} actual={before or '-'}")
        return {
            "ok": True,
            "msg": msg,
            "guarded": False,
            "drift": True,
            "foreground_before": before,
            "foreground_after": before,
            "foreground_app_name": actual_name,
        }
    except Exception as e:
        SLog.w(TAG, f"foreground observe failed: {e}")
        return {"ok": True, "msg": str(e), "guarded": False, "drift": False}


def _screen_suggests_test_app(
    engine,
    *,
    package: str = "",
    app_name: str = "",
) -> bool:
    """启动过渡期包名可能滞后；用屏上文案辅助判断目标应用是否已出现。"""
    blob = ""
    try:
        w = int(getattr(engine, "screen_width", 0) or 0)
        h = int(getattr(engine, "screen_height", 0) or 0)
        if w > 0 and h > 0 and hasattr(engine, "ocr"):
            blob = engine.ocr(w, h) or ""
    except Exception:
        blob = ""
    if not blob:
        try:
            from server.services.page_context_service import _collect_full_screen_text

            blob = _collect_full_screen_text(engine) or ""
        except Exception:
            blob = ""
    if not blob:
        return False

    name = (app_name or "").strip()
    if name and name in blob:
        return True
    pkg_tail = (package or "").split(".")[-1]
    if pkg_tail and len(pkg_tail) >= 4 and pkg_tail in blob.lower():
        return True

    generic_markers = (
        "造物者",
        "造好物",
        "同意并继续",
        "隐私条款",
        "用户协议",
        "隐私政策",
        "访客浏览",
        "一键登录",
        "手机号登录",
    )
    return any(m in blob for m in generic_markers)


def ensure_app_foreground(
    sn: str,
    package: str,
    platform: str = "android",
    *,
    app_name: str = "",
) -> Dict[str, Any]:
    """
    尽力拉起被测应用。包名未立即匹配时不判失败、不阻断后续步骤；
    启动过渡期前台可能短暂显示其它包名（如微信授权栈），仅记录提示。
    """
    if not package or not sn:
        msg = "未配置应用包名" if not package else "未选择执行设备"
        SLog.w(TAG, f"ensure_app_foreground skipped: {msg}")
        return {"ok": False, "msg": msg, "hard_fail": True}
    try:
        import builtins
        import time as _time

        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine, resolve_mobile_serial
        from script.sleep import mSleep

        mobile_sn = resolve_mobile_serial(sn, platform)
        builtins.TARGET_DEVICE_SN = mobile_sn
        engine, _ = bootstrap_mobile_engine(sn, platform)
        if not hasattr(engine, "start_app"):
            return {"ok": False, "msg": "引擎不支持 start_app", "hard_fail": True}

        before = _engine_current_package(engine)
        SLog.i(TAG, f"Launch app sn={mobile_sn} package={package} before={before or '-'}")

        if hasattr(engine, "stop_app"):
            try:
                engine.stop_app(package)
                mSleep(0.8)
            except Exception as e:
                SLog.w(TAG, f"stop_app before launch failed: {e}")

        engine.start_app(package)

        after = before
        pkg_ok = False
        screen_ok = False
        deadline = _time.time() + 8.0
        while _time.time() < deadline:
            mSleep(0.45)
            after = _engine_current_package(engine)
            pkg_ok = _pkg_matches(package, after)
            screen_ok = _screen_suggests_test_app(
                engine, package=package, app_name=app_name
            )
            if pkg_ok or screen_ok:
                break

        if not pkg_ok and not screen_ok:
            SLog.w(TAG, f"Launch retry: expected={package} actual={after or '-'}")
            engine.start_app(package)
            retry_deadline = _time.time() + 4.0
            while _time.time() < retry_deadline:
                mSleep(0.45)
                after = _engine_current_package(engine)
                pkg_ok = _pkg_matches(package, after)
                screen_ok = _screen_suggests_test_app(
                    engine, package=package, app_name=app_name
                )
                if pkg_ok or screen_ok:
                    break

        drift = bool(after and not _pkg_matches(package, after))
        if drift:
            try:
                from server.services.regression_run_context import record_foreground_drift

                record_foreground_drift(
                    expected_package=package,
                    actual_package=after,
                    phase="launch",
                    note="应用拉起过渡期",
                )
            except Exception:
                pass

        if pkg_ok:
            msg = f"已拉起前台 {package}"
        elif screen_ok:
            msg = (
                f"已发起拉起 {package}；包名暂报为 {after or '未知'}，"
                f"界面已出现目标应用内容（可能处于启动/授权过渡期）"
            )
        else:
            msg = (
                f"已发起拉起 {package}；当前前台 {after or '未知'}，"
                f"可能仍处于应用切换中，后续步骤将继续执行"
            )

        SLog.i(
            TAG,
            f"Launch result pkg_ok={pkg_ok} screen_ok={screen_ok} after={after or '-'}",
        )
        return {
            "ok": True,
            "msg": msg,
            "package": package,
            "foreground_before": before,
            "foreground_after": after,
            "foreground_confirmed": pkg_ok,
            "foreground_uncertain": not pkg_ok,
            "screen_suggests_app": screen_ok,
            "drift": drift,
        }
    except Exception as e:
        SLog.w(TAG, f"ensure foreground failed: {e}")
        return {"ok": False, "msg": str(e), "hard_fail": True}


def build_plan_log(command: str, plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = [
        {"type": "command", "text": command, "title": "原始指令"},
    ]
    for i, step in enumerate(plan.get("steps") or []):
        entries.append(
            {
                "type": "planned_step",
                "index": i,
                "kind": step.get("kind"),
                "summary": step.get("summary") or step.get("kind"),
                "detail": {k: v for k, v in step.items() if k not in ("data",)},
            }
        )
    if plan.get("reply"):
        entries.append({"type": "reply", "text": plan.get("reply")})
    for err in plan.get("segment_errors") or []:
        entries.append({"type": "plan_error", "text": err, "title": "未识别子指令"})
    return entries


def _tap_target_rect(
    x: int,
    y: int,
    *,
    label: str = "",
    half: int = 44,
) -> Optional[Dict[str, Any]]:
    xi, yi = int(x or 0), int(y or 0)
    if xi <= 0 or yi <= 0:
        return None
    l, t = max(0, xi - half), max(0, yi - half)
    w = h = half * 2
    return {
        "left": l,
        "top": t,
        "width": w,
        "height": h,
        "center": [xi, yi],
        "label": (label or "").strip(),
    }


def _action_display_name(kind: str, method: str = "") -> str:
    k = (kind or "").lower()
    m = (method or "").lower()
    if k == "overlay_guard":
        return "Guard"
    if k == "click":
        return "Tap"
    if k == "input":
        return "Input"
    if k == "swipe":
        return "Scroll"
    if k == "open_app":
        return "Open"
    if k == "close_app":
        return "Close"
    if k == "back":
        return "Back"
    if k == "ability":
        return "Ability"
    return kind or "Action"


def _format_action_title(kind: str, method: str, summary: str) -> str:
    """避免「Tap - Tap · …」重复前缀。"""
    action_name = _action_display_name(kind, method)
    s = (summary or kind or "").strip()
    if not s:
        return action_name
    low = s.lower()
    if low.startswith(action_name.lower() + " ") or low.startswith(action_name.lower() + "·"):
        return s
    if action_name == "Tap" and (s.startswith("Tap ·") or s.startswith("Tap ")):
        return s
    if action_name == "Input":
        return s if s.lower().startswith("input") else f"Input - {s}"
    return f"{action_name} - {s}"


def business_step_results_ok(results: List[Dict[str, Any]]) -> bool:
    """以每个业务 index 的最后一次结果为准；忽略 plan_attempt 中间失败与守卫行。"""
    if not results:
        return True
    try:
        from server.services.overlay_guard_service import is_guard_plan_index
    except Exception:
        def is_guard_plan_index(pi: int) -> bool:
            return int(pi) >= 1000

    last_by_index: Dict[int, Dict[str, Any]] = {}
    for r in results:
        idx = int(r.get("index") or 0)
        if is_guard_plan_index(idx):
            continue
        if (r.get("phase") or "").strip() == "overlay_guard":
            continue
        last_by_index[idx] = r
    if not last_by_index:
        return False
    return all(bool(r.get("ok")) for r in last_by_index.values())


def build_operation_plan_tree(
    plan_log: List[Dict[str, Any]],
    execute_log: List[Dict[str, Any]],
    *,
    reply: str = "",
) -> Dict[str, Any]:
    """
    操作步骤的规划与实际动作。

    约定（对齐前端 ExecutionReplayer 的“同级”语义）：
    - Plan 与具体 Tap/Assert 等动作是同一级别；
    - 每个 Plan 可以引用若干 index，但“谁先谁后”由 flat_items 控制；
    - flat_items 是一个扁平序列：[{type: 'plan'|'action', index: int}, ...]。
    """
    planned = [e for e in (plan_log or []) if e.get("type") == "planned_step"]

    # index -> 执行动作列表
    exec_by_index: Dict[int, List[Dict[str, Any]]] = {}
    seen_gestures: set = set()
    for entry in execute_log or []:
        gid = entry.get("gesture_id") or ""
        if gid:
            if gid in seen_gestures:
                continue
            seen_gestures.add(gid)
        idx = entry.get("index")
        if idx is None:
            idx = len(exec_by_index)
        exec_by_index.setdefault(int(idx), []).append(entry)

    plans: List[Dict[str, Any]] = []
    flat_items: List[Dict[str, Any]] = []

    # 先按 index 排序，确保展平顺序稳定
    sorted_planned = sorted(planned, key=lambda x: int(x.get("index") or 0))

    for ps in sorted_planned:
        idx = int(ps.get("index") or 0)
        actions_raw = exec_by_index.get(idx, [])

        plan_shot = ""
        plan_elapsed = ""
        plan_elapsed_ms = 0
        if actions_raw:
            last_a = actions_raw[-1]
            plan_shot = (
                last_a.get("screenshot_before")
                or last_a.get("screenshot_after")
                or actions_raw[0].get("screenshot_before")
                or actions_raw[0].get("screenshot_after")
                or ""
            )
            for a in reversed(actions_raw):
                ms = int(a.get("run_elapsed_ms") or 0)
                if ms > 0:
                    plan_elapsed_ms = ms
                    plan_elapsed = a.get("run_elapsed") or plan_elapsed
                    break
            if not plan_elapsed:
                plan_elapsed = actions_raw[0].get("run_elapsed") or ""
                plan_elapsed_ms = int(actions_raw[0].get("run_elapsed_ms") or 0)

        ps_summary = (ps.get("summary") or ps.get("kind") or "").strip()
        if ps_summary.startswith("守卫 ·"):
            plan_title = ps_summary
        else:
            plan_title = f"Plan - {ps_summary or '步骤'}"

        # 记录 Plan 本身
        actions_raw = sorted(
            actions_raw,
            key=lambda a: int(a.get("run_elapsed_ms") or 0),
        )

        plan_item = {
            "plan_index": idx,
            "summary": ps_summary,
            "title": plan_title,
            "kind": ps.get("kind"),
            "detail": ps.get("detail") or {},
            "screenshot": plan_shot,
            "run_elapsed": plan_elapsed,
            "run_elapsed_ms": plan_elapsed_ms,
            "ok": actions_raw[-1].get("ok") if actions_raw else None,
        }
        plans.append({**plan_item, "actions": []})

        for a in actions_raw:
            kind = a.get("kind") or "step"
            method = a.get("method") or ""
            action_with_meta = {
                **a,
                "action_name": _action_display_name(kind, method),
                "title": _format_action_title(kind, method, a.get("summary") or kind),
                "plan_index": idx,
            }
            plans[-1]["actions"].append(action_with_meta)

    # 兼容：如果大模型没有显式输出 planned_step，而只有 execute_log
    if not plans and execute_log:
        for i, entry in enumerate(execute_log):
            kind = entry.get("kind") or "step"
            method = entry.get("method") or ""
            plans.append(
                {
                    "plan_index": i,
                    "summary": entry.get("summary") or "",
                    "title": f"Plan - {entry.get('summary') or kind}",
                    "kind": kind,
                    "detail": {},
                    "actions": [
                        {
                            **entry,
                            "action_name": _action_display_name(kind, method),
                            "title": _format_action_title(kind, method, entry.get("summary") or kind),
                            "plan_index": i,
                        }
                    ],
                    "ok": entry.get("ok", True),
                }
            )
            flat_items.append({"type": "plan", "plan_index": i})
            flat_items.append({"type": "action", "plan_index": i, "index": entry.get("index", i)})

    if plans:
        flat_items = _build_flat_items_by_execution_order(plans)
    elif flat_items:
        flat_items = _sort_flat_items_chronologically(flat_items, plans)

    return {
        "thought": (reply or "").strip(),
        "plans": plans,
        "flat_items": flat_items,
    }


def _action_flat_item(plan_index: int, action: Dict[str, Any]) -> Dict[str, Any]:
    ms = int(action.get("run_elapsed_ms") or 0)
    item: Dict[str, Any] = {
        "type": "action",
        "plan_index": plan_index,
        "index": action.get("index"),
        "gesture_index": action.get("gesture_index"),
        "gesture_id": action.get("gesture_id"),
        "phase": action.get("phase") or "",
        "click_attempt": action.get("click_attempt"),
        "guard_round": action.get("guard_round"),
    }
    if ms > 0:
        item["run_elapsed_ms"] = ms
        if action.get("run_elapsed"):
            item["run_elapsed"] = action.get("run_elapsed")
    return item


def _plan_flat_item(plan_index: int, run_elapsed_ms: int) -> Dict[str, Any]:
    item: Dict[str, Any] = {"type": "plan", "plan_index": plan_index}
    if run_elapsed_ms >= 0:
        try:
            from server.services.regression_run_context import format_run_elapsed

            item["run_elapsed"] = format_run_elapsed(run_elapsed_ms)
            item["run_elapsed_ms"] = run_elapsed_ms
        except Exception:
            pass
    return item


def _build_flat_items_by_execution_order(
    plans: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """按 run_elapsed_ms 跨 Plan 交错：业务 Plan → 点击 → 守卫 Plan → Tap → 重试业务 Plan。"""
    try:
        from server.services.overlay_guard_service import is_guard_plan_index
    except Exception:
        def is_guard_plan_index(pi: int) -> bool:
            return int(pi) >= 1000

    timeline: List[tuple] = []
    for p in plans:
        pi = int(p.get("plan_index") or 0)
        for a in p.get("actions") or []:
            timeline.append((int(a.get("run_elapsed_ms") or 0), pi, a))
    timeline.sort(key=lambda t: (t[0], t[1]))

    flat: List[Dict[str, Any]] = []
    last_was_guard = False
    emitted_guard_plans: set = set()
    emitted_business_initial: set = set()

    for _ms, pi, act in timeline:
        act_ms = int(act.get("run_elapsed_ms") or _ms or 0)
        is_guard = act.get("phase") == "overlay_guard" or is_guard_plan_index(pi)
        if is_guard:
            biz_before = int(act.get("guard_before_step") if act.get("guard_before_step") is not None else -1)
            if biz_before >= 0 and biz_before not in emitted_business_initial:
                flat.append(_plan_flat_item(biz_before, act_ms))
                emitted_business_initial.add(biz_before)
            if pi not in emitted_guard_plans:
                flat.append(_plan_flat_item(pi, act_ms))
                emitted_guard_plans.add(pi)
            flat.append(_action_flat_item(pi, act))
            last_was_guard = True
            continue

        biz_pi = int(act.get("index") if act.get("index") is not None else pi)
        if is_guard_plan_index(biz_pi):
            biz_pi = int(act.get("guard_before_step") or 0)
        if last_was_guard:
            flat.append(_plan_flat_item(biz_pi, act_ms))
        elif biz_pi not in emitted_business_initial:
            flat.append(_plan_flat_item(biz_pi, act_ms))
            emitted_business_initial.add(biz_pi)
        flat.append(_action_flat_item(biz_pi, act))
        last_was_guard = False

    return flat


def _sort_flat_items_chronologically(
    flat_items: List[Dict[str, Any]],
    plans: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """兼容旧结构：无 plans 时保留已有 flat_items。"""
    if not plans:
        return flat_items
    return _build_flat_items_by_execution_order(plans) or flat_items


def _split_expected_fragments(expected_text: str) -> List[str]:
    try:
        from server.services.expectation_semantic_service import parse_expectation_texts

        texts = parse_expectation_texts(expected_text)
        if texts:
            return texts
    except Exception:
        pass
    exp = re.sub(r"^\d+[.、．)\）]\s*", "", (expected_text or "").strip())
    if not exp:
        return []
    parts = [p.strip() for p in re.split(r"[；;、]", exp) if p.strip() and len(p.strip()) >= 2]
    return parts if parts else [exp]


def build_expected_plan_tree(
    expected_text: str,
    checks: List[Dict[str, Any]],
    *,
    reply: str = "",
) -> Dict[str, Any]:
    """预期动作 → Plan → 校验项。"""
    fragments = _split_expected_fragments(expected_text)
    claim_kinds: Dict[str, str] = {}
    try:
        from server.services.expectation_semantic_service import parse_expectation_claims

        for row in parse_expectation_claims(expected_text):
            claim_kinds[row["text"]] = row.get("kind") or "generic"
    except Exception:
        pass
    check_by_text = {(c.get("text") or "").strip(): c for c in (checks or [])}
    plans: List[Dict[str, Any]] = []

    for i, frag in enumerate(fragments):
        matched = check_by_text.get(frag)
        if not matched:
            for c in checks or []:
                ct = (c.get("text") or "").strip()
                if frag in ct or ct in frag:
                    matched = c
                    break
        verify_checks = [matched] if matched else [{"text": frag, "ok": False, "reason": "未校验"}]
        plans.append(
            {
                "plan_index": i,
                "summary": frag,
                "title": f"Plan - 校验{frag}",
                "verify_text": frag,
                "claim_kind": claim_kinds.get(frag, "generic"),
                "checks": verify_checks,
                "ok": all(c.get("ok") for c in verify_checks),
            }
        )

    thought = (reply or "").strip() or (f"验证预期：{expected_text}" if expected_text else "")
    return {"thought": thought, "plans": plans}


def build_execute_log(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    entries = []
    for r in results or []:
        gestures = r.get("gestures") or []
        if gestures:
            seen_gid: set = set()
            for g in gestures:
                gid = g.get("gesture_id") or ""
                if gid and gid in seen_gid:
                    continue
                if gid:
                    seen_gid.add(gid)
                g_kind = g.get("kind") or r.get("kind") or "click"
                g_method = g.get("method") or r.get("method") or ""
                gx = int(g.get("x") or r.get("x") or 0)
                gy = int(g.get("y") or r.get("y") or 0)
                g_label = g.get("label") or r.get("target_label") or ""
                screen_size = (
                    g.get("screen_size")
                    or r.get("screen_size")
                    or {}
                )
                target_rect = (
                    g.get("target_rect")
                    or _tap_target_rect(gx, gy, label=g_label)
                    or r.get("target_rect")
                )
                entries.append(
                    {
                        "type": "action",
                        "index": r.get("index"),
                        "gesture_index": g.get("index"),
                        "gesture_id": gid or None,
                        "parent_index": r.get("index"),
                        "kind": g_kind,
                        "text": r.get("text") or "",
                        "field_hint": r.get("field_hint") or g.get("label") or "",
                        "summary": g.get("summary") or r.get("summary"),
                        "ok": g.get("ok", r.get("ok")),
                        "msg": g.get("msg") or r.get("msg"),
                        "method": g_method,
                        "x": gx,
                        "y": gy,
                        "label": g.get("label") or "",
                        "source": g.get("source") or "",
                        "phase": g.get("phase") or "",
                        "duration_ms": g.get("duration_ms") or r.get("duration_ms"),
                        "started_at": g.get("started_at") or r.get("started_at"),
                        "screenshot_before": g.get("screenshot_before") or "",
                        "screenshot_after": g.get("screenshot_after") or "",
                        "target_rect": target_rect,
                        "screen_size": screen_size,
                        "target_label": g_label,
                        "action_name": g.get("action_name")
                        or r.get("action_name")
                        or _action_display_name(g_kind, g_method),
                        "locate_debug": r.get("locate_debug") or g.get("locate_debug"),
                        "run_elapsed": g.get("run_elapsed") or r.get("run_elapsed"),
                        "run_elapsed_ms": g.get("run_elapsed_ms") or r.get("run_elapsed_ms"),
                        "phase": g.get("phase") or r.get("phase") or "",
                        "guard_round": g.get("guard_round") if g.get("guard_round") is not None else r.get("guard_round"),
                        "guard_before_step": r.get("guard_before_step"),
                        "click_attempt": r.get("click_attempt"),
                    }
                )
            continue
        entries.append(
            {
                "type": "action",
                "index": r.get("index"),
                "kind": r.get("kind") or "step",
                "text": r.get("text") or "",
                "field_hint": r.get("field_hint") or "",
                "summary": r.get("summary"),
                "ok": r.get("ok"),
                "msg": r.get("msg"),
                "method": r.get("method"),
                "x": r.get("x"),
                "y": r.get("y"),
                "duration_ms": r.get("duration_ms"),
                "started_at": r.get("started_at"),
                "screenshot_before": r.get("screenshot_before") or "",
                "screenshot_after": r.get("screenshot_after") or "",
                "target_rect": r.get("target_rect"),
                "screen_size": r.get("screen_size"),
                "target_label": r.get("target_label") or "",
                "current_page": r.get("current_page") or "",
                "current_page_score": r.get("current_page_score"),
                "current_page_matched": r.get("current_page_matched"),
                "current_page_id": r.get("current_page_id") or "",
                "icon_auto_learned": r.get("icon_auto_learned"),
                "suggest_icon_library": r.get("suggest_icon_library"),
                "locate_debug": r.get("locate_debug"),
                "run_elapsed": r.get("run_elapsed"),
                "run_elapsed_ms": r.get("run_elapsed_ms"),
                "phase": r.get("phase") or "",
                "guard_round": r.get("guard_round"),
                "guard_before_step": r.get("guard_before_step"),
                "click_attempt": r.get("click_attempt"),
            }
        )
        if not entries[-1].get("suggest_icon_library"):
            label = (r.get("target_label") or r.get("summary") or "").strip()
            method = r.get("method") or ""
            try:
                from server.services.icon_target_service import should_auto_learn_icon

                if should_auto_learn_icon(
                    method=method,
                    target_label=label,
                    target_rect=r.get("target_rect"),
                ):
                    entries[-1]["suggest_icon_library"] = True
            except Exception:
                pass
    return entries


def patch_exec_log_after_from_page(
    exec_log: List[Dict[str, Any]],
    results: List[Dict[str, Any]],
    page_after_shot: str,
) -> None:
    """用步骤结束后稳定页截图覆盖 after（避免 consent 过渡白屏）。"""
    shot = (page_after_shot or "").strip()
    if not shot:
        return
    for entry in exec_log or []:
        entry["screenshot_after"] = shot
    for r in results or []:
        r["screenshot_after"] = shot
        for g in r.get("gestures") or []:
            g["screenshot_after"] = shot


def persist_run_start(
    db: Session,
    *,
    run_id: str,
    app_id: str,
    sn: str,
    platform: str,
    total: int,
    run_type: str = "feishu",
) -> AppRegressionRun:
    row = AppRegressionRun(
        run_id=run_id,
        app_id=app_id,
        run_type=run_type,
        sn=sn,
        platform=platform,
        status="running",
        total=float(total),
        passed=0,
        failed=0,
        payload={"cases": []},
        started_at=datetime.now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _json_safe(value: Any) -> Any:
    """将 numpy 等不可 JSON 序列化类型转为原生 Python。"""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    try:
        import numpy as np

        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, (np.integer, np.floating)):
            return float(value)
    except Exception:
        pass
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def persist_run_progress(db: Session, run_doc: Dict[str, Any]) -> None:
    """执行过程中增量写入，供前端轮询展示进度与回放。"""
    row = db.query(AppRegressionRun).filter(AppRegressionRun.run_id == run_doc.get("run_id")).first()
    if not row:
        return
    row.status = run_doc.get("status") or "running"
    row.passed = float(run_doc.get("passed") or 0)
    row.failed = float(run_doc.get("failed") or 0)
    row.payload = _json_safe(run_doc)
    db.commit()


def persist_run_pause(db: Session, run_doc: Dict[str, Any]) -> None:
    row = db.query(AppRegressionRun).filter(AppRegressionRun.run_id == run_doc.get("run_id")).first()
    if not row:
        return
    row.status = run_doc.get("status") or "awaiting_clarification"
    row.passed = float(run_doc.get("passed") or 0)
    row.failed = float(run_doc.get("failed") or 0)
    row.payload = _json_safe(run_doc)
    db.commit()


def persist_run_finish(db: Session, run_doc: Dict[str, Any]) -> None:
    row = db.query(AppRegressionRun).filter(AppRegressionRun.run_id == run_doc.get("run_id")).first()
    if not row:
        return
    row.status = run_doc.get("status") or "done"
    row.passed = float(run_doc.get("passed") or 0)
    row.failed = float(run_doc.get("failed") or 0)
    row.payload = _json_safe(run_doc)
    row.finished_at = datetime.now()
    db.commit()


def get_run_from_db(db: Session, run_id: str) -> Optional[Dict[str, Any]]:
    row = db.query(AppRegressionRun).filter(AppRegressionRun.run_id == run_id).first()
    if not row:
        return None
    payload = row.payload if isinstance(row.payload, dict) else {}
    payload.setdefault("run_id", row.run_id)
    payload.setdefault("app_id", row.app_id)
    payload.setdefault("sn", row.sn)
    payload.setdefault("platform", row.platform)
    payload.setdefault("status", row.status)
    return payload


def list_runs_for_app(db: Session, app_id: str, limit: int = 30) -> List[Dict[str, Any]]:
    rows = (
        db.query(AppRegressionRun)
        .filter(AppRegressionRun.app_id == app_id)
        .order_by(AppRegressionRun.started_at.desc())
        .limit(limit)
        .all()
    )
    out = []
    for r in rows:
        out.append(
            {
                "run_id": r.run_id,
                "app_id": r.app_id,
                "sn": r.sn,
                "platform": r.platform,
                "status": r.status,
                "total": r.total,
                "passed": r.passed,
                "failed": r.failed,
                "started_at": r.started_at.isoformat() if r.started_at else "",
                "finished_at": r.finished_at.isoformat() if r.finished_at else "",
            }
        )
    return out
