# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""开跑前环境闸门：App / PC 看当前屏并切到本趟环境。

Web / Server 用域名区分，不走这闸。账号租约、简报、填码都读 RunContext 上
同一份 env_profile / resource_env，不再各猜各的。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from script.log import SLog

TAG = "EnvGate"

APP_PLATFORMS = frozenset({"android", "ios", "iphone", "ipad", "pc", "mac", "windows", "desktop"})
URL_PLATFORMS = frozenset({"web", "browser", "playwright", "server"})

_CANON = {
    "test": "test",
    "testing": "test",
    "qa": "test",
    "dev": "dev",
    "pre": "pre",
    "staging": "pre",
    "stg": "pre",
    "prod": "prod",
    "production": "prod",
    "live": "prod",
    "测试": "test",
    "开发": "dev",
    "预发": "pre",
    "正式": "prod",
    "生产": "prod",
}


def canon_run_env(raw: str) -> str:
    s = str(raw or "").strip()
    if not s or s.lower() == "unknown":
        return ""
    return _CANON.get(s.lower(), _CANON.get(s, s.lower() if s.lower() in _CANON.values() else ""))


def env_matches(observed: str, wanted: str) -> bool:
    a, b = canon_run_env(observed), canon_run_env(wanted)
    return bool(a and b and a == b)


def env_conflicts(observed: str, wanted: str) -> bool:
    """两边都识别成环境 key 且不是同一个。unknown 不算冲突。"""
    a, b = canon_run_env(observed), canon_run_env(wanted)
    return bool(a and b and a != b)


def needs_env_align(platform: str = "", sn: str = "") -> bool:
    """App / PC 要看屏切环境；Web / Server 靠地址，跳过。"""
    plat = str(platform or "").strip().lower()
    try:
        from server.services.runtime.playwright_hub import is_web_slot

        if is_web_slot(sn, plat):
            return False
    except Exception:
        pass
    if plat in URL_PLATFORMS:
        return False
    if plat in APP_PLATFORMS:
        return True
    return bool(plat) and plat not in URL_PLATFORMS


def env_label_of(env_doc: Optional[dict], key: str) -> str:
    from server.services.project_env import ENV_PROFILE_LABELS

    want = str(key or "").strip()
    for row in (env_doc or {}).get("environments") or []:
        if isinstance(row, dict) and str(row.get("key") or "") == want:
            return str(row.get("label") or ENV_PROFILE_LABELS.get(want, want) or want)
    return str(ENV_PROFILE_LABELS.get(want, want) or want)


def public_channel_rows(env_doc: Optional[dict], env_key: str) -> list[dict[str, str]]:
    from server.services.project_env import profile_snapshot

    doc = env_doc if isinstance(env_doc, dict) else {}
    snap = profile_snapshot(doc, env_key)
    channels = doc.get("channels") if isinstance(doc.get("channels"), list) else []
    rows: list[dict[str, str]] = []
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        cid = str(ch.get("id") or "").strip()
        if not cid:
            continue
        field = str(ch.get("field") or "value")
        block = snap.get(cid) if isinstance(snap.get(cid), dict) else {}
        value = ""
        if isinstance(block, dict):
            value = str(block.get(field) or block.get("value") or "").strip()
        rows.append({
            "id": cid,
            "kind": str(ch.get("kind") or ""),
            "platform": str(ch.get("platform") or ""),
            "label": str(ch.get("label") or cid),
            "field": field,
            "value": value,
        })
    return rows


def attach_run_env(
    ctx,
    *,
    app_id: str = "",
    env_profile: str = "",
    platform: str = "",
    env_doc: Optional[dict] = None,
) -> dict[str, Any]:
    """任务一开始就把本趟环境挂上 ctx。租号 / 简报 / 填码共用这一份。"""
    from server.services.project_env import env_secrets, resolve_profile_name

    doc = env_doc
    if doc is None:
        try:
            from server.services.account_issue_service import _env_doc_for_app

            doc = _env_doc_for_app(str(app_id or "")) if app_id else {}
        except Exception as exc:
            SLog.w(TAG, f"load env doc failed: {exc}")
            doc = {}
    key = resolve_profile_name(doc or {}, env_profile) if doc else str(env_profile or "").strip()
    if not key:
        key = str(env_profile or "").strip()
    label = env_label_of(doc, key)
    secrets = env_secrets(doc, key) if doc else {}
    channels = public_channel_rows(doc, key)
    prev = dict(getattr(ctx, "resource_env", None) or {})
    prev_secrets = prev.get("secrets") if isinstance(prev.get("secrets"), dict) else {}
    snap = {
        "env_key": key,
        "label": label,
        "app_id": str(app_id or prev.get("app_id") or ""),
        "project_id": str(prev.get("project_id") or ""),
        "platform": str(platform or getattr(ctx, "platform", "") or ""),
        "channels": channels,
        "secrets": prev_secrets or secrets,
    }
    if not snap["project_id"] and app_id:
        try:
            from server.services.resources.lease import _project_id_for_app

            snap["project_id"] = _project_id_for_app(str(app_id))
        except Exception:
            pass
    if hasattr(ctx, "env_profile"):
        ctx.env_profile = key
    if hasattr(ctx, "env_label"):
        ctx.env_label = label
    ctx.resource_env = snap
    return snap


def public_env_snapshot(ctx) -> dict[str, Any]:
    """给任务快照 / 简报用。不含密钥。"""
    snap = dict(getattr(ctx, "resource_env", None) or {})
    return {
        "env_key": str(snap.get("env_key") or getattr(ctx, "env_profile", "") or ""),
        "label": str(snap.get("label") or getattr(ctx, "env_label", "") or ""),
        "channels": list(snap.get("channels") or []),
        "env_fact": dict(getattr(ctx, "env_fact", None) or {}),
    }


def env_briefing_text(ctx, *, query: str = "") -> str:
    from server.services.knowledge_briefing import compile_briefing
    from server.services.ai.playbook_service import env_howto_block

    playbook = getattr(ctx, "playbook", None) or {}
    packet = compile_briefing(
        str(getattr(ctx, "app_id", "") or ""),
        {"lane": "prep", "need": "howto", "surface": "app"},
        query=query or "切换环境 当前环境",
        case_intent="确认并切换到本趟执行环境",
        playbook=playbook,
        synthesize=False,
        app_version=str(getattr(ctx, "app_version", "") or ""),
        env_profile=str(getattr(ctx, "env_profile", "") or ""),
        env_label=str(getattr(ctx, "env_label", "") or ""),
    )
    bits = [str(getattr(packet, "text", "") or "").strip()]
    how = env_howto_block(playbook)
    if how and how not in bits[0]:
        bits.append(how)
    return "\n".join(x for x in bits if x).strip()


def _launch_target(ctx, router, *, package: str, platform: str, run_id: str) -> dict[str, Any]:
    from server.services.ai.regression.schemas import EventStatus, PlanEvent

    pkg = str(package or getattr(ctx, "target_package", "") or "").strip()
    plat = str(platform or getattr(ctx, "platform", "") or "").lower()
    if not pkg or plat in URL_PLATFORMS:
        return {"ok": True, "skipped": "no_package"}
    event = PlanEvent(
        seq=0, capability_id="launch_app", event_kind="launch_app",
        params={"package": pkg}, needs_vlm=False,
        ai_reasoning="env align launch", label="打开被测应用",
    )
    try:
        result = router.dispatch(
            event, run_id=run_id or "", case_id="__env_align__",
            case_brief="环境对齐", shared={},
        )
        status = getattr(result, "status", None)
        ok = status == EventStatus.PASS
        return {
            "ok": ok,
            "summary": str(getattr(result, "summary", "") or ""),
            "error": str(getattr(result, "error", "") or ""),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _inspect(ctx, *, wanted: str, label: str, hint: str, provider_id: str, capture_prefer) -> dict[str, Any]:
    from server.services.regression.screen import capture_screen
    from server.services.ai.regression.planner import inspect_env

    screen = capture_screen(
        ctx, prefer=capture_prefer or ("adb", "remote"),
        timeout_sec=20.0, force_fresh=True,
    )
    if not screen.has_image():
        return {
            "env": "unknown", "seen": "", "ok": False,
            "reason": screen.error or "无截图，无法查看当前环境",
        }
    return inspect_env(
        wanted_env=wanted,
        wanted_label=label,
        knowledge_hint=hint,
        image_base64=screen.image_base64,
        image_mime=screen.image_mime,
        provider_id=provider_id,
    )


def _switch(ctx, router, *, wanted: str, label: str, run_id: str, provider_id: str) -> bool:
    from server.services.ai.regression.schemas import CaseGoal
    from server.services.regression.agent_executor import AgentExecutor, AgentOptions

    goal = CaseGoal(
        case_id="__env_align__",
        goal=(
            f"把本应用切换到「{label or wanted}」环境。"
            "只按说明书/知识里的切换路径操作。不要登录，不要做业务用例。"
        ),
        success_criteria=f"当前屏能看出已经是 {label or wanted}",
    )
    ex = AgentExecutor(
        goal=goal,
        run_context=ctx,
        router=router,
        run_id=run_id,
        case_id="__env_align__",
        case_brief=goal.goal,
        provider_id=provider_id,
        options=AgentOptions(),
        case_name="环境对齐",
    )
    return bool(ex.run_env_switch(wanted, label))


def align_device_env(
    ctx,
    router,
    *,
    package: str = "",
    platform: str = "",
    run_id: str = "",
    case_id: str = "",
    provider_id: str = "",
    capture_prefer: tuple[str, ...] = ("adb", "remote"),
    recorder=None,
    switch_fn=None,
) -> dict[str, Any]:
    """看屏确认环境，不对再切。登录页没有角标是 unknown，不是冲突。

    recorder(cap, status, summary)：把观察/切换写成用例前置步。
    switch_fn()：调用方自己的切环境循环（同一条用例的 AgentExecutor）。
    """
    wanted = str(getattr(ctx, "env_profile", "") or "").strip()
    label = str(getattr(ctx, "env_label", "") or wanted).strip()
    plat = str(platform or getattr(ctx, "platform", "") or "")
    sn = str(getattr(ctx, "sn", "") or "")
    report: dict[str, Any] = {
        "wanted": wanted,
        "label": label,
        "observed": "",
        "matched": False,
        "conflicted": False,
        "switched": False,
        "unconfirmed": False,
        "ok": False,
        "reason": "",
        "skipped": "",
    }

    def rec(cap: str, status: str, summary: str) -> None:
        if recorder is None:
            return
        try:
            recorder(str(cap), str(status), str(summary or "")[:200])
        except Exception as exc:
            SLog.w(TAG, f"env recorder failed: {exc}")

    if not needs_env_align(plat, sn):
        report["ok"] = True
        report["skipped"] = "url_distinguishes"
        report["reason"] = "Web/Server 用地址区分环境，不在设备上切换"
        rec("env_align", "skipped", report["reason"])
        ctx.env_fact = dict(report)
        return report
    hint = env_briefing_text(ctx)
    launched = _launch_target(ctx, router, package=package, platform=plat, run_id=run_id)
    if not launched.get("ok") and not launched.get("skipped"):
        SLog.w(TAG, f"[{run_id}] env launch failed: {launched.get('error') or launched.get('summary')}")
    inspect = _inspect(
        ctx, wanted=wanted, label=label, hint=hint,
        provider_id=provider_id, capture_prefer=capture_prefer,
    )
    report["observed"] = str(inspect.get("env") or "")
    report["seen"] = str(inspect.get("seen") or "")
    report["reason"] = str(inspect.get("reason") or "")
    rec(
        "inspect_env", "pass",
        f"当前环境 {report['observed'] or 'unknown'}"
        + (f"：{report['reason']}" if report["reason"] else ""),
    )
    if wanted and env_matches(report["observed"], wanted):
        report["matched"] = True
        report["ok"] = True
        rec("env_align", "skipped", f"已是{label or wanted}，无需切换")
        ctx.env_fact = dict(report)
        SLog.i(TAG, f"[{run_id}] env already {report['observed']} wanted={wanted}")
        return report
    if not wanted:
        report["ok"] = True
        report["skipped"] = "no_target"
        report["reason"] = report["reason"] or "本趟没有指定环境，只记录观察"
        rec("env_align", "skipped", report["reason"])
        ctx.env_fact = dict(report)
        return report
    rec("env_align", "pass", f"本趟要对齐环境：{label or wanted}")
    if switch_fn is not None:
        switched = bool(switch_fn())
    else:
        switched = _switch(
            ctx, router, wanted=wanted, label=label,
            run_id=run_id, provider_id=provider_id,
        )
    report["switched"] = bool(switched)
    inspect2 = _inspect(
        ctx, wanted=wanted, label=label, hint=hint,
        provider_id=provider_id, capture_prefer=capture_prefer,
    )
    report["observed"] = str(inspect2.get("env") or report["observed"])
    report["seen"] = str(inspect2.get("seen") or report.get("seen") or "")
    report["reason"] = str(inspect2.get("reason") or report["reason"])
    rec(
        "inspect_env", "pass",
        f"切换后环境 {report['observed'] or 'unknown'}"
        + (f"：{report['reason']}" if report["reason"] else ""),
    )
    report["matched"] = env_matches(report["observed"], wanted)
    report["conflicted"] = env_conflicts(report["observed"], wanted)
    if report["matched"]:
        report["ok"] = True
        rec("env_align", "pass", f"已对齐到{label or wanted}")
        SLog.i(TAG, f"[{run_id}] env aligned to {report['observed']} switched={report['switched']}")
    elif report["conflicted"]:
        report["ok"] = False
        report["reason"] = (
            report["reason"]
            or f"当前环境是 {report['observed']}，本趟要 {label or wanted}"
        )
        rec("env_align", "fail", report["reason"])
        SLog.w(TAG, f"[{run_id}] env mismatch wanted={wanted} observed={report['observed']}")
    else:
        report["ok"] = True
        report["unconfirmed"] = True
        report["reason"] = (
            (report["reason"] or "当前屏看不出环境标识")
            + "。未当作与本趟环境不一致。"
        )
        rec("env_align", "pass", report["reason"])
        SLog.i(TAG, f"[{run_id}] env unconfirmed wanted={wanted} observed={report['observed']}")
    ctx.env_fact = dict(report)
    return report
