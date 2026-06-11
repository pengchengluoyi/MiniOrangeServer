# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""飞书用例「前置条件」解析与执行（环境检查 / 清缓存等）。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from script.log import SLog

TAG = "CasePrecondition"

WECHAT_PKG = "com.tencent.mm"


def split_precondition_lines(text: str) -> List[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    parts = re.split(r"(?:\n|^)\s*\d+[.、．)\）]\s*", raw, flags=re.M)
    lines = [p.strip() for p in parts if p and p.strip()]
    if len(lines) <= 1 and raw:
        return [raw]
    return lines


def _classify_line(line: str) -> Tuple[str, str]:
    """返回 (kind, phase)；phase=before_launch | after_launch。"""
    t = (line or "").strip()
    low = t.lower()

    if re.search(r"无缓存|清除缓存|清理缓存|清空缓存|清缓存|清除应用", t):
        return "clear_cache", "before_launch"
    if re.search(r"sim卡|sim\s*卡|安装\s*sim|手机卡|电话卡", t, re.I):
        return "check_sim", "before_launch"
    if re.search(r"安装.*微信|已装.*微信|有微信|装了微信|微信已安装", t):
        return "check_wechat", "before_launch"
    if re.search(r"未安装微信|没装微信|无微信", t):
        return "check_no_wechat", "before_launch"
    if re.search(r"ios|苹果机|iphone|ipad", low) and re.search(r"设备|执行|手机", t):
        return "check_ios_device", "before_launch"
    if re.search(r"安卓|android", low) and re.search(r"设备|执行|手机", t):
        return "check_android_device", "before_launch"
    if re.search(r"已登录|登录状态|保持登录", t):
        return "check_logged_in", "after_launch"
    if re.search(r"未登录|游客|未登陆", t):
        return "check_not_logged_in", "after_launch"
    return "unknown", "before_launch"


def _mobile_engine(sn: str, platform: str):
    import builtins

    from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

    builtins.TARGET_DEVICE_SN = str(sn)
    engine, _ = bootstrap_mobile_engine(str(sn), platform)
    return engine


def _read_sim_phone_number(engine) -> str:
    """尽力读取本机号码（部分机型/运营商可能为空）。"""
    import re

    def _normalize_msisdn(raw: str) -> str:
        digits = re.sub(r"\D", "", raw or "")
        if len(digits) >= 11 and digits.startswith("86") and len(digits) > 11:
            digits = digits[2:]
        return digits if len(digits) >= 7 else ""

    for prop in ("gsm.line1.number", "persist.radio.line1_number"):
        phone = _normalize_msisdn((engine.shell(f"getprop {prop}") or "").strip())
        if phone:
            return phone
    try:
        out = engine.shell(
            "content query --uri content://telephony/siminfo --projection number 2>/dev/null"
        ) or ""
        for m in re.finditer(r"number=([+\d][\d+]*)", out):
            phone = _normalize_msisdn(m.group(1))
            if phone:
                return phone
    except Exception:
        pass
    for cmd in (
        "dumpsys iphonesubinfo",
        "dumpsys telephony.registry",
    ):
        try:
            out = engine.shell(cmd) or ""
            for pat in (
                r"Phone Number.*?:\s*(\+?\d[\d\s-]{6,})",
                r"mLine1Number[=:]\s*(\+?\d[\d\s-]{6,})",
                r"line1Number[=:]\s*(\+?\d[\d\s-]{6,})",
            ):
                m = re.search(pat, out, re.I)
                if m:
                    phone = _normalize_msisdn(m.group(1))
                    if phone:
                        return phone
        except Exception:
            pass
    return ""


def _check_sim(engine) -> Tuple[bool, str, Dict[str, Any]]:
    meta: Dict[str, Any] = {"sim_state": "", "operator": "", "phone_number": ""}
    state = (engine.shell("getprop gsm.sim.state") or "").strip().upper()
    meta["sim_state"] = state
    operator = (engine.shell("getprop gsm.operator.alpha") or "").strip()
    if not operator:
        operator = (engine.shell("getprop gsm.sim.operator.alpha") or "").strip()
    meta["operator"] = operator
    phone = _read_sim_phone_number(engine)
    meta["phone_number"] = phone

    parts: List[str] = []
    if state in ("READY", "LOADED", "PIN_REQUIRED", "PUK_REQUIRED"):
        parts.append(f"SIM 已就绪（{state}）")
    if operator:
        parts.append(f"运营商: {operator}")
    if phone:
        parts.append(f"号码: {phone}")
    else:
        parts.append("号码: 系统未暴露本机号码")
    if parts:
        return True, "；".join(parts), meta
    if operator:
        return True, f"检测到运营商: {operator}", meta
    return False, f"未检测到可用 SIM 卡（gsm.sim.state={state or '空'}）", meta


def _check_wechat(engine, *, must_exist: bool) -> Tuple[bool, str]:
    out = (engine.shell(f"pm path {WECHAT_PKG}") or "").strip()
    installed = "package:" in out
    if must_exist:
        return (True, "已安装微信") if installed else (False, "未安装微信，不满足前置条件")
    return (True, "未安装微信") if not installed else (False, "已安装微信，与「未装微信」前置不符")


def _clear_app_data(engine, package: str) -> Tuple[bool, str]:
    if not package:
        return False, "未配置应用包名，无法清除缓存"
    out = (engine.shell(f"pm clear {package}") or "").strip()
    if "Success" in out:
        return True, f"已清除应用数据（{package}）"
    return False, f"清除应用数据失败: {out or 'unknown'}"


def _check_platform(expected: str, actual: str) -> Tuple[bool, str]:
    exp = (expected or "").lower()
    act = (actual or "").lower()
    if "ios" in exp or "苹果" in expected:
        ok = act in ("ios", "mobile")
        return ok, "当前为 iOS 设备" if ok else f"当前设备类型为 {actual}，需要 iOS"
    if "android" in exp or "安卓" in expected:
        ok = act == "android"
        return ok, "当前为 Android 设备" if ok else f"当前设备类型为 {actual}，需要 Android"
    return True, ""


def _screen_blob(engine) -> str:
    try:
        from server.services.page_context_service import _collect_full_screen_text

        return _collect_full_screen_text(engine) or ""
    except Exception as e:
        SLog.w(TAG, f"collect screen for precondition failed: {e}")
        return ""


def _check_logged_in(engine, *, expect_logged_in: bool) -> Tuple[bool, str]:
    from server.services.page_navigation_service import _screen_is_login_home
    from server.services.page_context_service import _identify_page_by_screen_keywords

    blob = _screen_blob(engine)
    on_login = _screen_is_login_home(blob)
    page = _identify_page_by_screen_keywords(blob) or {}
    label = (page.get("label") or "").strip()

    if expect_logged_in:
        if on_login:
            return False, "当前仍在登录页，未满足「已登录」前置"
        if label in ("首页", "消息", "我的"):
            return True, f"当前在「{label}」，视为已登录"
        if label and "登录" not in label:
            return True, f"当前在「{label}」，未识别为登录页"
        return False, "无法确认已登录状态（仍可能处于登录流程）"

    if on_login or label == "登录注册页":
        return True, "当前在登录页，满足「未登录」前置"
    return False, f"当前在「{label or '未知页'}」，不满足「未登录」前置"


def _stamp_precondition_item(entry: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from server.services.regression_run_context import stamp_run_timing

        return stamp_run_timing(entry)
    except Exception:
        return entry


def _run_one(
    kind: str,
    line: str,
    *,
    engine,
    platform: str,
    package: str,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"text": line, "kind": kind, "ok": False, "msg": ""}
    try:
        if kind == "clear_cache":
            ok, msg = _clear_app_data(engine, package)
        elif kind == "check_sim":
            ok, msg, sim_meta = _check_sim(engine)
            entry.update(sim_meta)
        elif kind == "check_wechat":
            ok, msg = _check_wechat(engine, must_exist=True)
        elif kind == "check_no_wechat":
            ok, msg = _check_wechat(engine, must_exist=False)
        elif kind == "check_ios_device":
            ok, msg = _check_platform("ios", platform)
        elif kind == "check_android_device":
            ok, msg = _check_platform("android", platform)
        elif kind == "check_logged_in":
            ok, msg = _check_logged_in(engine, expect_logged_in=True)
        elif kind == "check_not_logged_in":
            ok, msg = _check_logged_in(engine, expect_logged_in=False)
        else:
            ok, msg = True, f"暂未自动化: {line}（已跳过，请人工确认环境）"
            entry["skipped"] = True
        entry["ok"] = ok
        entry["msg"] = msg
    except Exception as e:
        entry["ok"] = False
        entry["msg"] = str(e)
    return _stamp_precondition_item(entry)


def precondition_cleared_app_cache(items: List[Dict[str, Any]]) -> bool:
    """本用例是否已成功执行「应用无缓存 / 清缓存」类前置。"""
    return any(
        i.get("kind") == "clear_cache" and i.get("ok") and not i.get("skipped")
        for i in (items or [])
    )


def has_precondition_phase(precondition_raw: str, phase: str) -> bool:
    for line in split_precondition_lines(precondition_raw):
        _, line_phase = _classify_line(line)
        if line_phase == phase:
            return True
    return False


def run_preconditions(
    precondition_raw: str,
    *,
    sn: str,
    platform: str,
    package: str,
    phase: str,
) -> Dict[str, Any]:
    """
    执行指定阶段的前置条件。
    phase: before_launch（清缓存/SIM/微信/设备类型）| after_launch（已登录等）
    """
    lines = split_precondition_lines(precondition_raw)
    tasks: List[Tuple[str, str]] = []
    for line in lines:
        kind, line_phase = _classify_line(line)
        if line_phase != phase:
            continue
        tasks.append((kind, line))

    if not tasks:
        return {"ok": True, "items": [], "msg": ""}

    if platform != "android" and phase == "before_launch":
        non_platform = [k for k, _ in tasks if k not in ("check_ios_device", "check_android_device", "unknown")]
        if non_platform:
            return {
                "ok": False,
                "items": [
                    {
                        "text": precondition_raw,
                        "kind": "platform",
                        "ok": False,
                        "msg": f"前置检查 {non_platform} 暂仅支持 Android 设备",
                    }
                ],
                "msg": "前置条件需要 Android 设备执行",
            }

    engine = None
    items: List[Dict[str, Any]] = []
    try:
        if platform == "android":
            engine = _mobile_engine(sn, platform)
        for kind, line in tasks:
            if kind != "check_ios_device" and kind != "check_android_device" and engine is None:
                items.append(
                    _stamp_precondition_item(
                        {
                            "text": line,
                            "kind": kind,
                            "ok": False,
                            "msg": "需要 Android 设备执行该检查",
                        }
                    )
                )
                continue
            if kind in ("check_ios_device", "check_android_device"):
                if engine:
                    items.append(
                        _run_one(kind, line, engine=engine, platform=platform, package=package)
                    )
                else:
                    ok, msg = _check_platform(
                        "ios" if kind == "check_ios_device" else "android", platform
                    )
                    items.append(
                        _stamp_precondition_item(
                            {
                                "text": line,
                                "kind": kind,
                                "ok": ok,
                                "msg": msg,
                            }
                        )
                    )
            else:
                items.append(
                    _run_one(kind, line, engine=engine, platform=platform, package=package)
                )
    except Exception as e:
        SLog.e(TAG, f"precondition engine failed: {e}")
        return {
            "ok": False,
            "items": items,
            "msg": f"前置条件执行失败: {e}",
        }

    ok = all(i.get("ok") for i in items)
    fail_msgs = [i.get("msg") for i in items if not i.get("ok")]
    return {
        "ok": ok,
        "items": items,
        "msg": "；".join(fail_msgs) if fail_msgs else "前置条件已满足",
    }
