# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""飞书用例「前置条件」解析与执行（环境检查 / 清缓存等）。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from script.log import SLog

TAG = "CasePrecondition"

def _wechat_pkg() -> str:
    try:
        from server.services.local.locate.app_packages import package_for_app_key

        pkg = package_for_app_key("wechat")
        if pkg:
            return pkg
    except Exception:
        pass
    return "com.tencent.mm"


WECHAT_PKG = _wechat_pkg()


def split_precondition_lines(text: str) -> List[str]:
    try:
        from server.services.shared.semantic.case_text_semantic_service import parse_precondition_lines

        lines = parse_precondition_lines(text)
        if lines:
            return lines
    except Exception:
        pass
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
    if re.search(r"测试环境|预发环境|正式环境|生产环境|开发环境|切换环境|客户端环境", t):
        return "check_env", "after_launch"
    if re.search(r"保留权限(询问|弹窗|框)?|不要(预)?授权|拒绝权限|测(试)?权限拒绝|keep_permission", t, re.I):
        return "keep_permission_prompt", "before_launch"
    if re.search(r"当前已打开|已打开\s*(造好物|.+)?\s*(App|APP|应用)|前台(是|为|应用)|应用在前台|目标应用已打开", t):
        return "check_app_foreground", "before_launch"
    if re.search(r"客户端版本|应用版本|app版本|versionName|版本\s*[≥>=≤<=]", t, re.I):
        return "check_app_version", "before_launch"
    return "unknown", "before_launch"


def _mobile_engine(sn: str, platform: str):
    plat = (platform or "android").lower()
    if plat in ("ios", "iphone", "ipad"):
        from server.services.runtime.ios_wda_session import get_ios_engine

        return get_ios_engine(str(sn))

    from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
    from server.services.runtime.device_bind import bind_device_sn

    bind_device_sn(str(sn))
    engine, _ = bootstrap_mobile_engine(str(sn), platform)
    return engine


def _is_ios_engine(engine) -> bool:
    """覆盖所有 iOS backend（IOSEngine / IOSAppiumEngine / 后续新增），不按类名硬编码。"""
    return getattr(engine, "PLATFORM", "") == "ios" if engine is not None else False


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


_SIM_READY_STATES = frozenset({"READY", "LOADED", "PIN_REQUIRED", "PUK_REQUIRED"})
_SIM_ABSENT_STATES = frozenset({"ABSENT", "NOT_READY", "UNKNOWN", "UNAVAILABLE", ""})


def _parse_sim_slot_states(raw: str) -> List[str]:
    return [p.strip().upper() for p in (raw or "").split(",") if p.strip()]


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

    slots = _parse_sim_slot_states(state)
    ready_slots = [s for s in slots if s in _SIM_READY_STATES]
    if not ready_slots:
        if not slots or all(s in _SIM_ABSENT_STATES for s in slots):
            return False, f"未检测到可用 SIM 卡（gsm.sim.state={state or '空'}）", meta
        return False, f"SIM 未就绪（gsm.sim.state={state or '空'}）", meta

    parts: List[str] = [f"SIM 已就绪（{','.join(ready_slots)}）"]
    if operator:
        parts.append(f"运营商: {operator}")
    if phone:
        parts.append(f"号码: {phone}")
    else:
        parts.append("号码: 系统未暴露本机号码")
    return True, "；".join(parts), meta


def _check_wechat(engine, *, must_exist: bool) -> Tuple[bool, str]:
    if _is_ios_engine(engine):
        bundle = "com.tencent.xin"
        try:
            # 走引擎层 app_state()（wda / appium 通用）：0 未安装，其余为已安装
            st = engine.app_state(bundle)
        except Exception as e:
            return False, f"无法探测微信安装状态: {e}"
        installed = st != 0
        if must_exist:
            return (True, "已安装微信") if installed else (False, "未安装微信，不满足前置条件")
        return (True, "未安装微信") if not installed else (False, "已安装微信，与「未装微信」前置不符")
    out = (engine.shell(f"pm path {WECHAT_PKG}") or "").strip()
    installed = "package:" in out
    if must_exist:
        return (True, "已安装微信") if installed else (False, "未安装微信，不满足前置条件")
    return (True, "未安装微信") if not installed else (False, "已安装微信，与「未装微信」前置不符")


def _engine_shell(engine, cmd: str) -> str:
    if engine is None:
        return ""
    try:
        return str(engine.shell(cmd) or "")
    except Exception:
        return ""


def _check_app_version(engine, package: str, line: str) -> Tuple[bool, str, Dict[str, Any]]:
    from server.services.runtime.app_query import (
        compare_version,
        parse_package_version,
        parse_version_constraint,
        version_dump_shell,
    )

    if not package:
        return False, "未配置应用包名，无法读版本", {}
    raw = _engine_shell(engine, version_dump_shell(package))
    parsed = parse_package_version(raw)
    name = str(parsed.get("version_name") or "").strip()
    meta: Dict[str, Any] = {"package": package, **parsed}
    if not name:
        return False, f"dumpsys 读不到 {package} 的 versionName", meta
    constraint = parse_version_constraint(line)
    if not constraint:
        return True, f"{package} {name}", meta
    op, expected = constraint["op"], constraint["expected"]
    ok = compare_version(name, op, expected)
    msg = f"{package} {name} {op} {expected}"
    if re.search(r"测试服|预发|正式服|生产", line or ""):
        msg += "；环境标签不从 adb 读取"
    if not ok:
        return False, f"版本不满足：{msg}", meta
    return True, msg, meta


def _check_app_foreground(engine, package: str) -> Tuple[bool, str, Dict[str, Any]]:
    from server.services.runtime.app_query import FOREGROUND_SHELL, parse_foreground

    raw = _engine_shell(engine, FOREGROUND_SHELL)
    parsed = parse_foreground(raw)
    fg = str(parsed.get("package") or "").strip()
    meta: Dict[str, Any] = {**parsed, "expected_package": package}
    if not fg:
        return True, "读不到前台应用，开场启动后再看时间线", meta
    if package and fg == package:
        return True, f"前台已是 {fg}", meta
    if package:
        return True, f"开场前前台是 {fg}，不是 {package}；开场启动后以时间线为准", meta
    return True, f"前台 {fg}", meta


def _clear_app_data(engine, package: str) -> Tuple[bool, str]:
    if not package:
        return False, "未配置应用包名，无法清除缓存"
    if _is_ios_engine(engine):
        try:
            # 走引擎层 stop_app()（wda app_stop / appium terminateApp）
            engine.stop_app(package)
            return True, f"iOS 已结束应用进程（{package}）；系统不提供等价于 pm clear 的清缓存"
        except Exception as e:
            return False, f"iOS 结束应用失败: {e}"
    from server.services.shared.clawnode_engine import is_clawnode_remote_engine

    if is_clawnode_remote_engine(engine):
        if hasattr(engine, "clear_app_cache"):
            ok = engine.clear_app_cache(package)
            if ok:
                return True, f"已清缓存（{package}）：打开应用详情后分步完成存储清理"
            return False, f"清缓存失败（{package}）"

    out = (engine.shell(f"pm clear {package}") or "").strip()
    if "Success" in out:
        return True, f"已清除应用数据（{package}）"
    return False, f"清除应用数据失败: {out or 'unknown'}"


def _check_platform(expected: str, actual: str) -> Tuple[bool, str]:
    exp = (expected or "").lower()
    act = (actual or "").lower()
    if "ios" in exp or "苹果" in expected:
        ok = act in ("ios", "mobile", "iphone", "ipad")
        return ok, "当前为 iOS 设备" if ok else f"当前设备类型为 {actual}，需要 iOS"
    if "android" in exp or "安卓" in expected:
        ok = act == "android"
        return ok, "当前为 Android 设备" if ok else f"当前设备类型为 {actual}，需要 Android"
    if "web" in exp or "网页" in expected or "浏览器" in expected:
        ok = act in ("web", "browser", "playwright")
        return ok, "当前为本机浏览器" if ok else f"当前设备类型为 {actual}，需要 Web"
    return True, ""


def _screen_blob(engine) -> str:
    try:
        from server.services.shared.page_context.page_context_service import _collect_full_screen_text

        return _collect_full_screen_text(engine) or ""
    except Exception as e:
        SLog.w(TAG, f"collect screen for precondition failed: {e}")
        return ""


def _main_tab_bar_logged_in(blob: str, profile=None) -> bool:
    """底栏主导航齐全 ⇒ 已登录。

    tab 文案是**应用事实**，来自 ui_profile，不能写死在这里 —— 写死的话换一个应用
    永远返回 False，「已登录」前置检查永远失败，用例会在前置阶段全部阻塞。
    画像里没有配 tab 时这条信号不可用，返回 False 交给其他信号判断（和以前对未知应用的
    实际效果一致，但现在是显式的）。
    """
    from server.services.ai import app_profile as ap

    prof = profile if profile is not None else ap.current()
    tabs = prof.login_signal_tabs()
    if not tabs:
        return False
    hits = sum(1 for t in tabs if t in (blob or ""))
    return hits >= max(1, int(prof.logged_in_tab_hits or 3))


def _has_persisted_login_session(engine, package: str) -> Tuple[bool, str]:
    """通过应用本地存储启发式判断是否存在登录会话（不读取敏感内容）。"""
    if _is_ios_engine(engine):
        return False, ""
    pkg = (package or "").strip()
    if not pkg:
        return False, ""
    for sub, min_files, keywords in (
        ("shared_prefs", 2, ("user", "login", "session", "token", "account", "auth")),
        ("databases", 1, ("user", "account", "session", "login", "token")),
    ):
        out = (engine.shell(f"ls /data/data/{pkg}/{sub} 2>/dev/null") or "").strip()
        if not out or "Permission denied" in out or "No such file" in out:
            continue
        files = [ln.strip() for ln in out.splitlines() if ln.strip() and not ln.startswith("total")]
        if len(files) < min_files:
            continue
        joined = " ".join(files).lower()
        if sub == "shared_prefs" and len(files) >= 2:
            if any(k in joined for k in keywords):
                return True, f"检测到登录相关本地配置（{len(files)} 个 shared_prefs）"
            return True, f"检测到应用本地数据（{len(files)} 个 shared_prefs），视为可能有登录会话"
        if any(k in joined for k in keywords):
            return True, "检测到用户/会话类本地数据库，视为已登录"
    return False, ""


def _check_logged_in(
    engine,
    *,
    expect_logged_in: bool,
    package: str = "",
) -> Tuple[bool, str]:
    """底栏 / 界面树启发式。会话闸门不再调用；仅给历史校验脚本用。"""
    from server.services.local.navigation.page_navigation_service import _screen_is_login_home
    from server.services.shared.page_context.page_context_service import _identify_page_by_screen_keywords
    from server.services.ai import app_profile as ap

    # 这里拿得到 package，直接按包解析画像，不依赖 contextvar 是否被绑定。
    profile = ap.current(package)
    blob = _screen_blob(engine)
    on_login = _screen_is_login_home(blob)
    page = _identify_page_by_screen_keywords(blob, profile=profile) or {}
    label = (page.get("label") or "").strip()
    tab_logged_in = _main_tab_bar_logged_in(blob, profile)
    logged_in_pages = profile.logged_in_pages or profile.login_signal_tabs()

    if expect_logged_in:
        session_ok, session_msg = _has_persisted_login_session(engine, package)
        if session_ok:
            return True, session_msg
        if tab_logged_in:
            return True, f"底栏主导航齐全（{'/'.join(profile.login_signal_tabs())}），视为已登录"
        if label and label in logged_in_pages:
            return True, f"当前在「{label}」，视为已登录"
        if on_login and tab_logged_in:
            return True, "主界面底栏已出现（登录流程中意外完成登录，未走退出）"
        if on_login:
            return False, "当前仍在登录页，未满足「已登录」前置"
        if label and "登录" not in label:
            return True, f"当前在「{label}」，未识别为登录页"
        return False, "无法确认已登录状态（仍可能处于登录流程）"

    if on_login or label == "登录注册页":
        return True, "当前在登录页，满足「未登录」前置"
    if tab_logged_in or (package and _has_persisted_login_session(engine, package)[0]):
        return False, "当前似已登录（底栏或本地会话存在），不满足「未登录」前置"
    return False, f"当前在「{label or '未知页'}」，不满足「未登录」前置"


def _stamp_precondition_item(entry: Dict[str, Any]) -> Dict[str, Any]:
    try:
        from server.services.shared.run_context.regression_run_context import stamp_run_timing

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
    plat = (platform or "").lower()
    ios_plat = plat in ("ios", "iphone", "ipad") or _is_ios_engine(engine)
    web_plat = plat in ("web", "browser", "playwright")
    try:
        from server.services.regression.coverage_codes import (
            refine_precondition_kind,
            UNSUPPORTED_PREP_KINDS,
        )

        kind = refine_precondition_kind(kind, line)
        entry["kind"] = kind
        if kind == "check_sim" and ios_plat:
            entry["ok"] = True
            entry["skipped"] = True
            entry["gap"] = True
            entry["msg"] = "iOS 无法读取 SIM，已跳过"
            return _stamp_precondition_item(entry)
        if kind in UNSUPPORTED_PREP_KINDS:
            entry["ok"] = True
            entry["skipped"] = True
            entry["gap"] = True
            entry["msg"] = f"前置引擎无法执行（{kind}）"
            return _stamp_precondition_item(entry)
        if kind == "web_config":
            entry["ok"] = True
            entry["skipped"] = True
            entry["msg"] = "后台开关由用例步骤/预期验证，不单独查后台"
            return _stamp_precondition_item(entry)
        if kind in ("check_app_version", "check_app_foreground") and (ios_plat or web_plat):
            entry["ok"] = True
            entry["skipped"] = True
            entry["gap"] = True
            entry["msg"] = "网页通道不读安装包版本/前台" if web_plat else "iOS 尚未接 dumpsys 读版本/前台"
            return _stamp_precondition_item(entry)
        if web_plat and kind == "clear_cache":
            entry["ok"] = True
            entry["msg"] = "网页每次新开浏览器上下文，无持久化缓存"
            return _stamp_precondition_item(entry)
        if web_plat and kind in ("check_sim", "check_wechat", "check_no_wechat"):
            entry["ok"] = True
            entry["skipped"] = True
            entry["gap"] = True
            entry["msg"] = f"网页通道不检查 {kind}"
            return _stamp_precondition_item(entry)
        if kind in ("check_logged_in", "check_not_logged_in"):
            entry["ok"] = True
            entry["skipped"] = True
            entry["msg"] = "登录态由开场看图与本应用登录知识对齐，不按界面树/底栏/本地存储判定"
            return _stamp_precondition_item(entry)
        if kind == "check_env":
            entry["ok"] = True
            entry["skipped"] = True
            entry["msg"] = "客户端环境由开跑前环境闸门对齐"
            return _stamp_precondition_item(entry)
        if kind == "unknown":
            entry["ok"] = True
            entry["skipped"] = True
            entry["gap"] = True
            entry["msg"] = f"前置未命中引擎库: {line}"
            return _stamp_precondition_item(entry)
        if kind == "clear_cache":
            ok, msg = _clear_app_data(engine, package)
        elif kind == "check_sim":
            if _is_ios_engine(engine):
                ok, msg = True, "iOS 无法读取 SIM，已跳过"
                entry["skipped"] = True
                entry["gap"] = True
            else:
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
        elif kind == "keep_permission_prompt":
            ok, msg = True, "已标记保留权限询问，预置层不 pm grant / 不自动点允许"
            entry["skipped"] = True
        elif kind == "check_app_version":
            ok, msg, meta = _check_app_version(engine, package, line)
            entry.update(meta)
        elif kind == "check_app_foreground":
            ok, msg, meta = _check_app_foreground(engine, package)
            entry.update(meta)
        else:
            ok, msg = True, f"前置未命中引擎库: {line}"
            entry["skipped"] = True
            entry["gap"] = True
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


def has_precondition_phase(
    precondition_raw: str,
    phase: str,
    scene: Optional[dict] = None,
) -> bool:
    if scene:
        from server.services.runtime.session_gate import clamp_case_scene

        return any(
            str(it.get("phase") or "") == phase
            for it in (clamp_case_scene(scene).get("prep_items") or [])
        )
    try:
        from server.services.shared.semantic.case_text_semantic_service import parse_precondition_items

        for item in parse_precondition_items(precondition_raw):
            if item.get("phase") == phase:
                return True
        return False
    except Exception:
        pass
    from server.services.runtime.session_gate import split_precondition_text

    if phase != "before_launch":
        return False
    return bool(split_precondition_text(precondition_raw))


def run_preconditions(
    precondition_raw: str,
    *,
    sn: str,
    platform: str,
    package: str,
    phase: str,
    scene: Optional[dict] = None,
) -> Dict[str, Any]:
    """
    执行指定阶段的前置条件。
    phase: before_launch（清缓存/SIM/微信/设备类型）| after_launch（已登录等）
    有 CaseScene 时只读 prep_items，不再扫关键字。
    """
    tasks: List[Tuple[str, str]] = []
    if scene:
        from server.services.runtime.session_gate import clamp_case_scene

        for item in clamp_case_scene(scene).get("prep_items") or []:
            if str(item.get("phase") or "") != phase:
                continue
            tasks.append((str(item.get("kind") or "unknown"), str(item.get("text") or "")))
    else:
        try:
            from server.services.shared.semantic.case_text_semantic_service import parse_precondition_items

            for item in parse_precondition_items(precondition_raw):
                if item.get("phase") != phase:
                    continue
                tasks.append((str(item.get("kind") or "unknown"), item.get("text") or ""))
        except Exception:
            tasks = []
        if not tasks:
            from server.services.runtime.session_gate import split_precondition_text

            if phase == "before_launch":
                for line in split_precondition_text(precondition_raw):
                    tasks.append(("unknown", line))

    if not tasks:
        return {"ok": True, "items": [], "msg": ""}

    from server.services.regression.coverage_codes import (
        refine_precondition_kind,
        stamp_precondition_items,
        prep_blocks_run,
        UNSUPPORTED_PREP_KINDS,
    )

    tasks = [(refine_precondition_kind(k, t), t) for k, t in tasks]
    plat = (platform or "android").lower()
    ios = plat in ("ios", "iphone", "ipad")
    from server.services.runtime.playwright_hub import is_web_slot

    web = is_web_slot(sn, plat) or plat in ("web", "browser", "playwright")
    no_engine = {"check_ios_device", "check_android_device", "unknown", "web_config"} | set(UNSUPPORTED_PREP_KINDS)
    if ios:
        no_engine = set(no_engine) | {"check_sim", "check_app_version", "check_app_foreground"}
    if web:
        no_engine = set(no_engine) | {
            "clear_cache",
            "check_sim",
            "check_wechat",
            "check_no_wechat",
            "check_logged_in",
            "check_not_logged_in",
            "keep_permission_prompt",
            "check_app_version",
            "check_app_foreground",
        }

    engine = None
    items: List[Dict[str, Any]] = []
    try:
        needs_engine = any(k not in no_engine for k, _ in tasks)
        if needs_engine:
            engine = _mobile_engine(sn, platform)
        for kind, line in tasks:
            if kind in no_engine:
                items.append(
                    _run_one(kind, line, engine=engine, platform=platform, package=package)
                )
                continue
            if kind not in ("check_ios_device", "check_android_device") and engine is None:
                items.append(
                    _stamp_precondition_item(
                        {
                            "text": line,
                            "kind": kind,
                            "ok": False,
                            "msg": (
                                "网页通道无法初始化移动执行引擎，前置检查未执行"
                                if web
                                else f"无法初始化{(' iOS' if ios else ' Android')}执行引擎，前置检查未执行"
                            ),
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
        stamped = stamp_precondition_items(items, platform=platform)
        return {
            "ok": False,
            "items": stamped,
            "msg": f"前置条件执行失败: {e}",
        }

    items = stamp_precondition_items(items, platform=platform)
    ok = not prep_blocks_run(items)
    fail_msgs = [
        i.get("msg")
        for i in items
        if not i.get("ok") and not i.get("gap")
    ]
    gap_n = sum(1 for i in items if i.get("gap"))
    if fail_msgs:
        msg = "；".join(str(m) for m in fail_msgs if m)
    elif gap_n:
        msg = f"前置可执行项已满足，{gap_n} 条无法执行已跳过"
    else:
        msg = "前置条件已满足"
    return {
        "ok": ok,
        "items": items,
        "msg": msg,
    }
