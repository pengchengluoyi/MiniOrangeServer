# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""ClawNode Remote 路径下的应用生命周期拟人操作。

- 强停：CapabilityRouter → AiPersonaExecutor（设置 UI 分步）
- 清缓存：先 EXEC_SCRIPT 打开应用详情页，再 persona 按当前截图逐步点可见入口完成清空
"""
from __future__ import annotations

from typing import Any, Optional, Tuple

from script.log import SLog

from server.services.ai.regression.schemas import EventStatus, PlanEvent

TAG = "PersonaRemoteLifecycle"


def _exec_script_reached_app_details(stdout: str) -> bool:
    """EXEC_SCRIPT 输出里前台应为设置/应用信息页，不能仍是 ClawNode 自身。"""
    text = (stdout or "").lower()
    if "com.clawnode.agent" in text and "com.android.settings" not in text:
        return False
    if "fg=com.clawnode" in text.replace(" ", ""):
        return False
    markers = (
        "com.android.settings",
        "settings",
        "securitycenter",
        "permcenter",
        "step2 open_app_details -> true",
    )
    return any(m in text for m in markers)


def open_app_details_via_exec_script(sn: str, package: str) -> Tuple[bool, str]:
    """
    与设备详情页 /device/command 一致：EXEC_SCRIPT script_id=open_app_settings。
    打开指定包名的应用信息页（com.android.settings 内）。
    """
    pkg = (package or "").strip()
    if not pkg:
        return False, "missing package"
    if not sn:
        return False, "missing sn"
    try:
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

        engine, _ = bootstrap_mobile_engine(sn, "android")
        if not hasattr(engine, "exec_script"):
            return False, "engine has no exec_script"
        ok, stdout, stderr = engine.exec_script(
            script_id="open_app_settings",
            script_vars={"package": pkg},
            timeout_ms=60_000,
        )
        if not ok or not _exec_script_reached_app_details(stdout or ""):
            if ok and stdout:
                SLog.w(TAG, f"open_app_details dsl fg bad sn={sn} pkg={pkg}, retry js")
            else:
                SLog.w(TAG, f"open_app_details dsl fail sn={sn} pkg={pkg}: {stderr or stdout}")
            ok, stdout, stderr = engine.exec_script(
                script_id="open_app_settings_js",
                script_vars={"package": pkg},
                timeout_ms=60_000,
            )
        if ok:
            fg = (stdout or "").strip()
            if not _exec_script_reached_app_details(fg):
                SLog.w(TAG, f"open_app_details fg unexpected sn={sn} pkg={pkg}: {fg[:160]}")
                return False, f"应用详情页未打开（fg 仍为 ClawNode 或其它应用）: {fg[:200]}"
            SLog.i(TAG, f"open_app_details ok sn={sn} pkg={pkg} fg={fg[:80]}")
            return True, fg or "ok"
        err = stderr or stdout or "EXEC_SCRIPT failed"
        SLog.w(TAG, f"open_app_details fail sn={sn} pkg={pkg}: {err}")
        return False, err
    except Exception as exc:
        SLog.e(TAG, f"open_app_details exception sn={sn} pkg={pkg}: {exc}")
        return False, str(exc)


def _dispatch_persona(
    sn: str,
    *,
    capability_id: str,
    package: str,
    task_description: str,
    template_id: str,
    platform: str = "android",
    app_name: str = "",
    timeout_hint: str = "",
) -> Tuple[bool, str, dict[str, Any]]:
    pkg = (package or "").strip()
    if not pkg:
        return False, "缺少 package", {}

    SLog.i(TAG, f"persona dispatch start cap={capability_id} pkg={pkg} sn={sn}")

    try:
        from server.services.runtime.run_context import build_run_context
        from server.services.regression.router import CapabilityRouter
        from server.services.regression.screen import capture_screen

        run_ctx = build_run_context(sn, platform=platform)
        if run_ctx.remote.get("state") != "connected":
            return False, f"remote 未连接 sn={sn}", {}
        if run_ctx.vlm.get("state") != "available":
            return False, "拟人化需要可用的回归执行模型（VLM/LLM）", {}

        prefer = ("remote", "adb") if str(sn).startswith("claw-") else ("adb", "remote")
        router = CapabilityRouter(run_ctx, capture_prefer=prefer)

        params: dict[str, Any] = {
            "package": pkg,
            "task": task_description,
            "force_persona_ui": True,
        }
        if app_name:
            params["app_name"] = app_name

        event = PlanEvent(
            seq=0,
            capability_id=capability_id,
            event_kind=capability_id,
            params=params,
            needs_vlm=True,
            expected_executor="ai_persona",
            fallback_executors=[],
            ai_reasoning=timeout_hint or task_description,
            label=task_description[:80],
        )

        shared = {"target_package": pkg}
        if app_name:
            shared["app_name"] = app_name

        result = router.dispatch(event, shared=shared)
        detail = {
            "status": str(result.status.value if hasattr(result.status, "value") else result.status),
            "executor": result.executor_used,
            "summary": result.summary,
            "vlm_meta": result.vlm_meta,
        }
        if result.status == EventStatus.PASS:
            SLog.i(TAG, f"persona dispatch ok cap={capability_id} pkg={pkg} sn={sn}")
            return True, result.summary or "ok", detail
        err = result.error or result.summary or "persona failed"
        SLog.w(TAG, f"persona cap={capability_id} pkg={pkg} sn={sn} failed: {err}")
        return False, err, detail
    except Exception as exc:
        SLog.e(TAG, f"persona dispatch exception cap={capability_id} sn={sn}: {exc}")
        return False, str(exc), {}


def force_stop_app_via_persona(
    sn: str,
    package: str,
    *,
    platform: str = "android",
    app_name: str = "",
) -> Tuple[bool, str, dict[str, Any]]:
    """设置 → 应用信息 → 强制停止。"""
    label = (app_name or package).strip()
    task = (
        f"强制停止应用 {label}（包名 {package}）。"
        "必须走系统设置 UI：设置 → 应用 → 找到该应用 → 应用信息 → 强制停止 → 确认。"
        "禁止使用 adb shell、禁止仅按 Home。"
    )
    return _dispatch_persona(
        sn,
        capability_id="kill_app",
        package=package,
        task_description=task,
        template_id="PERSONA_FORCE_STOP_VIA_SETTINGS",
        platform=platform,
        app_name=app_name,
        timeout_hint="launch/close 前置强停，必须完成强制停止",
    )


def clear_app_storage_via_persona(
    sn: str,
    package: str,
    *,
    platform: str = "android",
    app_name: str = "",
) -> Tuple[bool, str, dict[str, Any]]:
    """从当前屏（通常已在应用信息页）分步清空存储。"""
    label = (app_name or package).strip()
    task = (
        f"从【当前屏幕】继续清空应用 {label}（包名 {package}）的存储空间。"
        "这是流程化多步操作：每一步只点当前屏可见的下一入口，不要跳步，也不要从设置首页重头导航。"
        "若已在应用信息页，直接点「存储空间和缓存」「存储」「存储用量」等入口；"
        "若已看到「清空存储空间」「清除全部数据」「清除数据」，直接点击并处理确认弹窗。"
        "禁止使用 adb pm clear。"
    )
    return _dispatch_persona(
        sn,
        capability_id="clear_app_cache",
        package=package,
        task_description=task,
        template_id="PERSONA_CLEAR_CACHE_VIA_SETTINGS",
        platform=platform,
        app_name=app_name,
        timeout_hint=(
            "【前置】Server 已用 EXEC_SCRIPT 打开目标应用详情页。"
            "请基于当前截图从应用信息页继续：看到存储入口就点存储，看到清空按钮就点清空，"
            "不要重新打开设置或应用列表。"
        ),
    )
