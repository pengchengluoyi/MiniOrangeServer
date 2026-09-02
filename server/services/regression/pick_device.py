# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""开跑前按用例申请设备：把可用目录交给大模型，校验后占用。

人可以不选手动指定。App+Web / A-B 会占多台，本趟仍只在主设备上执行
（配对双屏尚未接入，不假装已经同时点两块屏）。
设备需求来自 CaseScene.device_need，不再扫「后台/网页」关键字。
"""
from __future__ import annotations

import json
from typing import Any, Optional

from script.log import SLog

TAG = "PickDevice"

MODES = ("single", "farm", "app_web", "ab_pair", "manual")
ROLES = ("primary", "peer", "web", "exec")


def _case_field_text(case: dict[str, Any], *keys: str) -> str:
    for key in keys:
        val = case.get(key)
        if isinstance(val, list):
            bits = [str(x).strip() for x in val if str(x).strip()]
            if bits:
                return "\n".join(bits[:40])
        elif val:
            return str(val)
    return ""


def _need_from_scenes(scenes: list[dict[str, Any]]) -> dict[str, Any]:
    from server.services.runtime.session_gate import clamp_case_scene

    rows = [clamp_case_scene(s) for s in scenes if isinstance(s, dict)]
    if not rows:
        return {
            "mode": "single",
            "platforms": ["android"],
            "want_web": False,
            "want_ab": False,
            "want_app": True,
            "want_ios": False,
            "want_android": True,
        }
    needs = [str(s.get("device_need") or "app") for s in rows]
    plats = [str(s.get("platform") or "android") for s in rows]
    want_ab = any(n == "ab_pair" for n in needs)
    want_web = any(n in ("web", "app_web") or p == "web" for n, p in zip(needs, plats))
    want_app = any(
        n in ("app", "app_web", "ab_pair") or p in ("android", "ios", "any")
        for n, p in zip(needs, plats)
    )
    if all((n == "web" or p == "web") and n not in ("app", "app_web", "ab_pair") for n, p in zip(needs, plats)):
        want_app = False
        want_web = True
    want_ios = any(p == "ios" for p in plats)
    want_android = any(p in ("android", "any") for p in plats) or (want_app and not want_ios)
    platforms: list[str] = []
    if want_ios and not want_android:
        platforms.append("ios")
    elif want_android and not want_ios:
        platforms.append("android")
    elif want_ios and want_android:
        platforms.extend(["android", "ios"])
    elif not want_web:
        platforms.append("android")
    if want_web:
        platforms.append("web")
    if not platforms:
        platforms = ["android"]
    if want_ab:
        mode = "ab_pair"
    elif want_web and want_app:
        mode = "app_web"
    else:
        mode = "single"
    return {
        "mode": mode,
        "platforms": platforms,
        "want_web": want_web,
        "want_ab": want_ab,
        "want_app": want_app,
        "want_ios": want_ios,
        "want_android": want_android,
    }


def infer_need(
    cases: list[dict[str, Any]],
    scenes: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """从 CaseScene 聚合要占什么：单机 / App+Web / A-B。没有场景则默认单机 Android。"""
    del cases
    return _need_from_scenes(list(scenes or []))


def scenes_for_cases(
    cases: list[dict[str, Any]],
    *,
    provider_id: str = "",
) -> list[dict[str, Any]]:
    """开跑申请设备前，对每条用例做一次场景理解（按原文缓存）。"""
    from server.services.ai.regression.planner import classify_case_scene
    from server.services.runtime.session_gate import fallback_case_scene

    out: list[dict[str, Any]] = []
    for case in cases or []:
        if not isinstance(case, dict):
            continue
        name = _case_field_text(case, "name", "title")
        pre = _case_field_text(case, "precondition", "preconditions", "precondition_raw")
        steps = _case_field_text(case, "steps_raw", "steps")
        expected = _case_field_text(case, "expected_raw", "expected")
        try:
            out.append(classify_case_scene(
                name=name,
                steps=steps,
                expected=expected,
                precondition=pre,
                provider_id=provider_id or None,
            ))
        except Exception as exc:
            SLog.w(TAG, f"classify_case_scene for pick failed: {exc}")
            out.append(fallback_case_scene(f"场景理解失败: {exc}", precondition=pre))
    return out


def device_kind(row: dict[str, Any]) -> str:
    from server.services.runtime.run_context import device_platform_kind

    kind = str(row.get("platform") or "").strip().lower()
    if kind in ("android", "ios", "web"):
        return kind
    return device_platform_kind(
        str(row.get("device_type") or row.get("type") or ""),
        row.get("channels"),
        sn=str(row.get("sn") or ""),
    )


def is_free(row: dict[str, Any]) -> bool:
    st = str(row.get("status") or "online").lower()
    if st not in ("online", "idle"):
        return False
    if str(row.get("busy_task_id") or "").strip():
        return False
    if str(row.get("reserved_slot_id") or "").strip():
        return False
    return True


def catalog_brief(catalog: list[dict[str, Any]], *, free_only: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in catalog or []:
        if not isinstance(row, dict):
            continue
        if free_only and not is_free(row):
            continue
        sn = str(row.get("sn") or "").strip()
        if not sn:
            continue
        out.append({
            "sn": sn,
            "platform": device_kind(row),
            "model": str(row.get("model") or "")[:40],
            "status": str(row.get("status") or ""),
            "busy": bool(str(row.get("busy_task_id") or "").strip()),
            "reserved": bool(str(row.get("reserved_slot_id") or "").strip()),
            "role": str(row.get("role") or ""),
        })
    return out


def sns_of_plan(plan: Optional[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for slot in (plan or {}).get("slots") or []:
        if not isinstance(slot, dict):
            continue
        sn = str(slot.get("sn") or "").strip()
        if sn and sn not in seen:
            seen.add(sn)
            out.append(sn)
    return out


def exec_sns_of_plan(plan: Optional[dict[str, Any]]) -> list[str]:
    plan = plan or {}
    mode = str(plan.get("mode") or "")
    listed = [str(x).strip() for x in (plan.get("exec_sns") or []) if str(x).strip()]
    if listed and mode not in ("app_web", "ab_pair"):
        return listed
    slots = [s for s in (plan.get("slots") or []) if isinstance(s, dict)]
    if mode in ("app_web", "ab_pair"):
        prim = [s for s in slots if str(s.get("role") or "") == "primary"]
        sns = sns_of_plan({"slots": prim or slots[:1]})
        return sns[:1]
    return listed or sns_of_plan(plan)


def finalize_plan(plan: Optional[dict[str, Any]]) -> dict[str, Any]:
    plan = dict(plan or {})
    mode = str(plan.get("mode") or "single").strip() or "single"
    if mode not in MODES:
        mode = "single"
    slots: list[dict[str, str]] = []
    seen: set[str] = set()
    for slot in plan.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        sn = str(slot.get("sn") or "").strip()
        if not sn or sn in seen:
            continue
        seen.add(sn)
        role = str(slot.get("role") or "primary").strip() or "primary"
        if role not in ROLES:
            role = "exec"
        plat = str(slot.get("platform") or "").strip().lower()
        if plat not in ("android", "ios", "web"):
            plat = "android"
        slots.append({"role": role, "sn": sn, "platform": plat})
    if mode == "ab_pair" and len([s for s in slots if s["role"] in ("primary", "peer", "exec")]) < 2:
        mode = "single"
    if mode == "app_web" and not any(s["role"] == "web" or s["platform"] == "web" for s in slots):
        mode = "single"
    if not any(s["role"] == "primary" for s in slots) and slots:
        slots[0]["role"] = "primary"
    draft = {**plan, "mode": mode, "slots": slots}
    exec_sns = exec_sns_of_plan(draft)
    all_sns = sns_of_plan(draft)
    held = [s for s in all_sns if s not in exec_sns]
    return {
        "mode": mode,
        "slots": slots,
        "reason": str(plan.get("reason") or "")[:240],
        "source": str(plan.get("source") or "fallback")[:32],
        "exec_sns": exec_sns,
        "held_sns": held,
    }


def manual_plan(sns: list[str], platforms_by_sn: Optional[dict[str, str]] = None) -> dict[str, Any]:
    plats = platforms_by_sn if isinstance(platforms_by_sn, dict) else {}
    slots = []
    for i, sn in enumerate(sns):
        s = str(sn or "").strip()
        if not s:
            continue
        plat = str(plats.get(s) or "").strip().lower()
        if plat not in ("android", "ios", "web"):
            plat = "web" if s.startswith("web") else "android"
        slots.append({
            "role": "primary" if i == 0 else "exec",
            "sn": s,
            "platform": plat,
        })
    mode = "farm" if len(slots) > 1 else "single"
    return finalize_plan({
        "mode": mode,
        "slots": slots,
        "reason": "创建任务时已指定设备",
        "source": "manual",
    })


def _by_sn(catalog: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in catalog or []:
        sn = str(row.get("sn") or "").strip()
        if sn:
            out[sn] = row
    return out


def _phones(free: list[dict[str, Any]], platforms: list[str]) -> list[dict[str, Any]]:
    want = [p for p in platforms if p in ("android", "ios")]
    if want:
        hit = [d for d in free if device_kind(d) in want]
        if hit:
            return hit
    return [d for d in free if device_kind(d) in ("android", "ios")]


def fallback_pick(need: dict[str, Any], catalog: list[dict[str, Any]]) -> dict[str, Any]:
    free = [d for d in catalog or [] if is_free(d)]
    mode = str(need.get("mode") or "single")
    plats = list(need.get("platforms") or ["android"])
    slots: list[dict[str, str]] = []
    reason = "按用例端类型选一台空闲设备"

    def add(role: str, row: dict[str, Any]) -> None:
        sn = str(row.get("sn") or "").strip()
        if not sn or any(s["sn"] == sn for s in slots):
            return
        slots.append({"role": role, "sn": sn, "platform": device_kind(row)})

    if mode == "ab_pair":
        phones = _phones(free, plats)
        if len(phones) >= 2:
            add("primary", phones[0])
            add("peer", phones[1])
            reason = "用例需要 A-B 双机，占用两台空闲设备"
        elif phones:
            add("primary", phones[0])
            mode = "single"
            reason = "用例要双机但只有一台空闲，先占主设备"
        if need.get("want_web"):
            webs = [d for d in free if device_kind(d) == "web"]
            if webs:
                add("web", webs[0])
                if mode == "single" and any(s["role"] == "peer" for s in slots):
                    mode = "ab_pair"
    elif mode == "app_web":
        phones = _phones(free, plats)
        webs = [d for d in free if device_kind(d) == "web"]
        if phones:
            add("primary", phones[0])
        if webs:
            add("web", webs[0])
        if phones and webs:
            reason = "用例同时涉及 App 与 Web，占用真机和本机浏览器"
        else:
            mode = "single"
            reason = "用例像 App+Web，但当前只有一端空闲"
    else:
        ordered = [p for p in plats if p != "web"] + (["web"] if "web" in plats else [])
        picked = None
        for plat in ordered or ["android"]:
            cand = [d for d in free if device_kind(d) == plat]
            if cand:
                picked = cand[0]
                break
        if picked is None and free:
            picked = free[0]
        if picked is not None:
            add("primary", picked)
        reason = "按用例端类型选一台空闲设备"

    return finalize_plan({
        "mode": mode if slots else "single",
        "slots": slots,
        "reason": reason,
        "source": "fallback",
    })


def _validate_llm_plan(
    parsed: Optional[dict[str, Any]],
    catalog: list[dict[str, Any]],
    need: dict[str, Any],
) -> Optional[dict[str, Any]]:
    if not isinstance(parsed, dict):
        return None
    by = _by_sn(catalog)
    slots: list[dict[str, str]] = []
    for slot in parsed.get("slots") or []:
        if not isinstance(slot, dict):
            continue
        sn = str(slot.get("sn") or "").strip()
        row = by.get(sn)
        if not row or not is_free(row):
            continue
        role = str(slot.get("role") or "primary").strip() or "primary"
        slots.append({"role": role, "sn": sn, "platform": device_kind(row)})
    if not slots:
        return None
    need_mode = str(need.get("mode") or "single")
    mode = str(parsed.get("mode") or need_mode)
    if not need.get("want_web"):
        slots = [s for s in slots if s["role"] != "web" and s["platform"] != "web"]
        if mode == "app_web":
            mode = "single"
    if not need.get("want_ab") and need_mode != "ab_pair":
        slots = [s for s in slots if s["role"] != "peer"]
        if mode == "ab_pair":
            mode = "single"
    if need_mode == "single":
        mode = "single"
        slots = [s for s in slots if s["role"] not in ("web", "peer") and s["platform"] != "web"]
    if not slots:
        return None
    return finalize_plan({
        "mode": mode,
        "slots": slots,
        "reason": str(parsed.get("reason") or "")[:240],
        "source": "llm",
    })


def _ask_llm(
    *,
    need: dict[str, Any],
    catalog: list[dict[str, Any]],
    cases: list[dict[str, Any]],
    env_profile: str = "",
    provider: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    if not provider or not provider.get("api_key"):
        return None
    from server.services.ai import dispatch_log as dispatch
    from server.services.ai.regression.llm_client import call_chat_text
    from server.services.ai.roles_catalog import PICK_DEVICE_SYSTEM_PROMPT

    titles = []
    for c in (cases or [])[:12]:
        if not isinstance(c, dict):
            continue
        titles.append(f"- {c.get('case_id') or ''} {c.get('name') or ''}".strip())
    user = (
        f"环境：{env_profile or '未标明'}\n"
        f"推断需求：{json.dumps(need, ensure_ascii=False)}\n"
        f"用例：\n" + ("\n".join(titles) or "（无标题）") + "\n"
        f"可用设备（只能从这里选 sn）：\n"
        f"{json.dumps(catalog_brief(catalog), ensure_ascii=False)}\n"
        "只输出 JSON。"
    )
    try:
        tok = dispatch.bind(
            job="pick_device", skill="pick_device", role="test-engineer",
            trigger="case_run", source="case_run",
        )
        try:
            parsed, meta = call_chat_text(
                provider=provider,
                messages=[
                    {"role": "system", "content": PICK_DEVICE_SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ],
                temperature=0.1,
                max_tokens=800,
                timeout_sec=45,
            )
        finally:
            dispatch.reset(tok)
        if meta.get("truncated") and not meta.get("salvaged"):
            SLog.w(TAG, "pick_device LLM truncated")
            return None
        return _validate_llm_plan(parsed, catalog, need)
    except Exception as exc:
        SLog.w(TAG, f"pick_device LLM failed: {exc}")
        return None


def pick_devices_for_run(
    *,
    cases: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
    env_profile: str = "",
    provider: Optional[dict[str, Any]] = None,
    provider_id: str = "",
) -> dict[str, Any]:
    """返回 finalize 后的 device_plan。slots 为空表示当前没有能占的设备。"""
    scenes = scenes_for_cases(cases, provider_id=str(provider_id or "").strip())
    need = infer_need(cases, scenes=scenes)
    if provider is None and provider_id:
        try:
            from server.services.ai.regression.llm_client import resolve_regression_provider

            provider, _gate = resolve_regression_provider(str(provider_id or "").strip() or None)
        except Exception:
            provider = None
    plan = _ask_llm(
        need=need, catalog=catalog, cases=cases,
        env_profile=env_profile, provider=provider,
    )
    if plan is None or not plan.get("slots"):
        plan = fallback_pick(need, catalog)
    _record_pick(need, plan, env_profile, cases)
    return plan


def _record_pick(
    need: dict[str, Any],
    plan: dict[str, Any],
    env_profile: str,
    cases: list[dict[str, Any]],
) -> None:
    try:
        from server.services.ai import dispatch_log as dispatch

        dispatch.record_job(
            status="done" if plan.get("slots") else "skipped",
            job="pick_device",
            role="test-engineer",
            skill="pick_device",
            source="case_run",
            detail="申请执行设备",
            input_data={
                "env": env_profile,
                "need": need,
                "case_ids": [str(c.get("case_id") or "") for c in (cases or [])[:20] if isinstance(c, dict)],
            },
            output_data=plan,
        )
    except Exception as exc:
        SLog.d(TAG, f"dispatch pick_device failed: {exc}")
