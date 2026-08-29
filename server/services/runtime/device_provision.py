# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""开跑前设备预置：亮屏 / 解锁 / 运行时权限。

Android 可用 `pm grant` 静默授权白名单权限。
iOS 没有等价 TCC 接口（真机不可静默授权），只能尝试关掉已弹出的系统 Alert。
用例前置声明「保留权限询问」时两边都不预授权，把框留给用例本身。
"""
from __future__ import annotations

import re
import subprocess
from typing import Any, Optional

from script.log import SLog

from server.services.ai.regression.schemas import EventStatus, PlanEvent

TAG = "DeviceProvision"

# 可静默 grant 的常见运行时权限。不包含通知监听 / 辅助功能 / 悬浮窗。
GRANT_WHITELIST = (
    "android.permission.CAMERA",
    "android.permission.RECORD_AUDIO",
    "android.permission.ACCESS_FINE_LOCATION",
    "android.permission.ACCESS_COARSE_LOCATION",
    "android.permission.READ_EXTERNAL_STORAGE",
    "android.permission.WRITE_EXTERNAL_STORAGE",
    "android.permission.READ_MEDIA_IMAGES",
    "android.permission.READ_MEDIA_VIDEO",
    "android.permission.READ_MEDIA_AUDIO",
    "android.permission.POST_NOTIFICATIONS",
    "android.permission.READ_CONTACTS",
    "android.permission.READ_PHONE_STATE",
    "android.permission.READ_MEDIA_VISUAL_USER_SELECTED",
)

GRANT_DENY = (
    "android.permission.BIND_NOTIFICATION_LISTENER_SERVICE",
    "android.permission.BIND_ACCESSIBILITY_SERVICE",
    "android.permission.SYSTEM_ALERT_WINDOW",
    "android.permission.WRITE_SETTINGS",
    "android.permission.PACKAGE_USAGE_STATS",
)

_PERM_LINE_RE = re.compile(r"(android\.permission\.[A-Z0-9_]+)")
_KEEP_PROMPT_RE = re.compile(
    r"保留权限(询问|弹窗|框)?|不要(预)?授权|拒绝权限|测(试)?权限拒绝|"
    r"keep_permission_prompt|keep_permission",
    re.I,
)
_IOS_ALLOW_LABELS = ("允许", "Allow", "好", "OK", "始终允许", "Allow While Using App", "Allow Once")


def wants_keep_permission_prompt(precondition: str = "") -> bool:
    text = str(precondition or "")
    return bool(_KEEP_PROMPT_RE.search(text))


def _adb_shell(sn: str, *args: str, timeout_sec: float = 20.0) -> tuple[int, str, str]:
    if not sn:
        return -1, "", "sn empty"
    try:
        proc = subprocess.run(
            ["adb", "-s", sn, "shell", *args],
            capture_output=True, text=True, timeout=timeout_sec,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout {timeout_sec}s"
    except FileNotFoundError:
        return -2, "", "adb not in PATH"


def parse_requested_runtime_permissions(dumpsys_text: str) -> list[str]:
    """从 `dumpsys package` 抽出应用声明的 android.permission.*。"""
    found: list[str] = []
    seen: set[str] = set()
    for line in str(dumpsys_text or "").splitlines():
        m = _PERM_LINE_RE.search(line)
        if not m:
            continue
        perm = m.group(1)
        if perm in seen:
            continue
        seen.add(perm)
        found.append(perm)
    return found


def _dispatch(router, capability_id: str, *, run_id: str = "", params: Optional[dict] = None) -> dict[str, Any]:
    event = PlanEvent(
        seq=0,
        capability_id=capability_id,
        event_kind=capability_id,
        params=dict(params or {}),
        needs_vlm=False,
        ai_reasoning="device provision",
        label=capability_id,
    )
    try:
        result = router.dispatch(event, run_id=run_id or "", case_id="", case_brief="provision", shared={})
        status = getattr(result, "status", None)
        ok = status == EventStatus.PASS
        return {
            "ok": ok,
            "status": str(getattr(status, "value", status) or ""),
            "summary": str(getattr(result, "summary", "") or ""),
            "error": str(getattr(result, "error", "") or ""),
        }
    except Exception as exc:
        return {"ok": False, "status": "error", "summary": "", "error": str(exc)}


def _android_adb_serial(ctx, sn: str) -> str:
    serial = ""
    adb = getattr(ctx, "adb", None) or {}
    if isinstance(adb, dict):
        serial = str(adb.get("serial") or "").strip()
    if serial and not serial.startswith("claw-"):
        return serial
    token = str(sn or "").strip()
    if token and not token.startswith("claw-"):
        try:
            from server.services.runtime.ios_ids import is_physical_ios_udid, is_simulator_udid
            if is_physical_ios_udid(token) or is_simulator_udid(token):
                return ""
        except Exception:
            pass
        return token
    return ""


def _grant_android(sn: str, package: str) -> dict[str, Any]:
    out: dict[str, Any] = {"granted": [], "skipped": [], "failed": []}
    if not package:
        out["skipped"].append("no_package")
        return out
    if not sn:
        out["skipped"].append("no_adb_serial")
        return out
    rc, text, err = _adb_shell(sn, "dumpsys", "package", package, timeout_sec=25.0)
    if rc != 0:
        out["failed"].append(f"dumpsys:{err or text or rc}")
        return out
    requested = parse_requested_runtime_permissions(text)
    for perm in GRANT_WHITELIST:
        if perm in GRANT_DENY:
            continue
        if requested and perm not in requested:
            continue
        grc, gout, gerr = _adb_shell(sn, "pm", "grant", package, perm, timeout_sec=8.0)
        if grc == 0:
            out["granted"].append(perm)
        else:
            # 未声明或旧 API 上不存在的权限：不算硬失败
            msg = (gerr or gout or f"rc={grc}").strip()[:120]
            out["skipped"].append(f"{perm}:{msg}" if msg else perm)
    return out


def _accept_ios_alert(sn: str) -> dict[str, Any]:
    """WDA Alert：点允许类按钮。真机无法 pm grant。"""
    try:
        from server.services.runtime.ios_wda_session import get_ios_engine

        engine = get_ios_engine(str(sn))
        client = getattr(engine, "driver", None)
        alert = getattr(client, "alert", None) if client is not None else None
        if alert is None:
            return {"ok": False, "reason": "wda_alert_unavailable"}
        exists = bool(getattr(alert, "exists", False))
        if not exists:
            return {"ok": True, "reason": "no_alert", "clicked": ""}
        labels: list[str] = []
        try:
            raw = alert.buttons() if callable(getattr(alert, "buttons", None)) else []
            labels = [str(x) for x in (raw or [])]
        except Exception:
            labels = []
        for want in _IOS_ALLOW_LABELS:
            if labels and want not in labels:
                continue
            try:
                if callable(getattr(alert, "click", None)) and want in (labels or _IOS_ALLOW_LABELS):
                    alert.click(want)
                    return {"ok": True, "reason": "clicked", "clicked": want, "buttons": labels}
            except Exception:
                continue
        try:
            if callable(getattr(alert, "accept", None)):
                alert.accept()
                return {"ok": True, "reason": "accepted", "clicked": "accept", "buttons": labels}
        except Exception as exc:
            return {"ok": False, "reason": str(exc), "buttons": labels}
        return {"ok": False, "reason": "no_allow_button", "buttons": labels}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


def provision_device(
    ctx,
    router,
    *,
    package: str = "",
    platform: str = "android",
    keep_permission_prompt: bool = False,
    run_id: str = "",
) -> dict[str, Any]:
    """开跑前预置。返回挂到 trace 的 provision_report。"""
    plat = (platform or getattr(ctx, "platform", "") or "android").lower()
    sn = str(getattr(ctx, "sn", "") or "")
    pkg = str(package or getattr(ctx, "target_package", "") or "")
    report: dict[str, Any] = {
        "platform": plat,
        "keep_permission_prompt": bool(keep_permission_prompt),
        "awake": "skipped",
        "unlocked": "skipped",
        "granted": [],
        "skipped": [],
        "failed": [],
        "ios_alert": {},
    }
    from server.services.runtime.playwright_hub import is_web_slot

    if plat in ("web", "browser", "playwright") or is_web_slot(sn, plat):
        report["awake"] = "n/a"
        report["unlocked"] = "n/a"
        report["skipped"].append("web_no_device_wake")
        SLog.i(TAG, f"[{run_id}] web provision skipped sn={sn}")
        return report
    if plat in ("ios", "iphone", "ipad"):
        report["awake"] = "n/a"
        report["unlocked"] = "engine_init"
        if keep_permission_prompt:
            report["skipped"].append("ios_no_tcc_grant")
            report["skipped"].append("keep_permission_prompt")
        else:
            report["skipped"].append("ios_no_tcc_grant")
            report["ios_alert"] = _accept_ios_alert(sn)
        SLog.i(TAG, f"[{run_id}] ios provision keep={keep_permission_prompt} alert={report['ios_alert']}")
        return report

    if router is not None:
        wake = _dispatch(router, "wake_screen", run_id=run_id)
        report["awake"] = "yes" if wake.get("ok") else f"fail:{wake.get('error') or wake.get('summary')}"
        unlock = _dispatch(router, "dismiss_keyguard", run_id=run_id)
        report["unlocked"] = "yes" if unlock.get("ok") else f"fail:{unlock.get('error') or unlock.get('summary')}"
    else:
        report["awake"] = "no_router"
        report["unlocked"] = "no_router"

    if keep_permission_prompt:
        report["skipped"].append("keep_permission_prompt")
    else:
        adb_serial = _android_adb_serial(ctx, sn)
        if not adb_serial:
            report["skipped"].append("no_adb_serial")
        else:
            grant = _grant_android(adb_serial, pkg)
            report["granted"] = list(grant.get("granted") or [])
            report["skipped"].extend(list(grant.get("skipped") or []))
            report["failed"].extend(list(grant.get("failed") or []))

    SLog.i(
        TAG,
        f"[{run_id}] android provision awake={report['awake']} unlocked={report['unlocked']} "
        f"granted={len(report['granted'])} keep={keep_permission_prompt}",
    )
    return report


def accept_post_launch_alerts(
    *,
    sn: str,
    platform: str,
    keep_permission_prompt: bool,
) -> dict[str, Any]:
    """启动应用后：iOS 再收一次系统 Alert。保留询问时不动。"""
    if keep_permission_prompt:
        return {"skipped": True, "reason": "keep_permission_prompt"}
    plat = (platform or "").lower()
    if plat in ("ios", "iphone", "ipad"):
        return _accept_ios_alert(sn)
    if plat in ("web", "browser", "playwright"):
        return {"skipped": True, "reason": "web_no_system_alert"}
    return {"skipped": True, "reason": "android_uses_pm_grant_and_l0"}
