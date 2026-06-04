# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
对话流：自然语言 → 可执行步骤 → Manager/引擎执行（类似 Midscene 的规划+执行循环）。
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from script.log import SLog

TAG = "CopilotService"

# 规划阶段无坐标时的占位；执行时会按文案/层级/OCR 重新定位
_DEFAULT_CLICK_XY = (0, 0)

# 常见底栏 Tab 顺序（5 Tab 布局，从右往左匹配「我的」等）
_BOTTOM_TAB_SLOTS = 5
_BOTTOM_TAB_INDEX = {
    "首页": 0,
    "发现": 1,
    "探索": 1,
    "消息": 2,
    "创作": 2,
    "相机": 2,
    "购物车": 2,
    "分类": 3,
    "我的": 4,
    "我": 4,
    "个人": 4,
    "账户": 4,
    "设置": 4,
}

# 全局手势计数（用于「每 50 次点击/滑动最多按一次返回」）
_GESTURE_COUNT = 0
_BACK_PENDING = False
_BACK_FLUSH_EVERY = 50


def _gesture_tick() -> None:
    global _GESTURE_COUNT, _BACK_PENDING
    _GESTURE_COUNT += 1
    if _BACK_PENDING and _GESTURE_COUNT >= _BACK_FLUSH_EVERY:
        _flush_back()


def _schedule_back() -> None:
    global _BACK_PENDING
    _BACK_PENDING = True
    _gesture_tick()


def _flush_back() -> None:
    global _GESTURE_COUNT, _BACK_PENDING
    if not _BACK_PENDING:
        return
    try:
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine
        import builtins

        sn = getattr(builtins, "TARGET_DEVICE_SN", None)
        if sn:
            engine, _ = bootstrap_mobile_engine(str(sn), "android")
            if hasattr(engine, "press_key"):
                SLog.i(TAG, "Copilot deferred back key")
                engine.press_key("back")
    except Exception as e:
        SLog.w(TAG, f"deferred back failed: {e}")
    _BACK_PENDING = False
    _GESTURE_COUNT = 0


def _task_payload(
    node_code: str,
    *,
    platform: str = "mobile",
    data: Optional[Dict] = None,
    display_name: str = "copilot",
) -> Dict[str, Any]:
    return {
        "id": f"copilot-{uuid.uuid4().hex[:8]}",
        "nodeCode": node_code,
        "nodeType": 200,
        "platform": platform,
        "displayName": display_name,
        "lastCodes": [],
        "nextCodes": [],
        "data": data or {},
    }


def _execute_ability(payload: Dict[str, Any]) -> Dict[str, Any]:
    from driver.tentacle.manager import Manager

    try:
        result = Manager().execute_interface(payload)
        if result is None:
            return {"ok": False, "msg": "组件未执行或节点被跳过"}
        if hasattr(result, "to_dict"):
            d = result.to_dict()
            ok = d.get("success", d.get("code") in (200, None))
            return {"ok": bool(ok), "msg": d.get("msg", ""), "data": d}
        return {"ok": True, "data": result}
    except Exception as e:
        SLog.e(TAG, f"execute failed: {e}")
        return {"ok": False, "msg": str(e)}


def _run_mobile_swipe(sn: str, direction: str, platform: str = "android") -> Dict[str, Any]:
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

        builtins.TARGET_DEVICE_SN = sn
        engine, (w, h) = bootstrap_mobile_engine(sn, platform)
        if not hasattr(engine, "swipe_norm"):
            return {"ok": False, "msg": "设备引擎不支持滑动"}
        sw, sh = w, h
        if direction == "up":
            engine.swipe_norm(0.5, 0.72, 0.5, 0.38, 0.35)
        elif direction == "down":
            engine.swipe_norm(0.5, 0.38, 0.5, 0.72, 0.35)
        elif direction == "left":
            engine.swipe_norm(0.78, 0.5, 0.22, 0.5, 0.35)
        else:
            engine.swipe_norm(0.22, 0.5, 0.78, 0.5, 0.35)
        _gesture_tick()
        return {"ok": True, "msg": f"滑动 {direction}"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def _label_variants(label: str) -> List[str]:
    raw = (label or "").strip()
    if not raw:
        return []
    out: List[str] = []
    for cand in (
        raw,
        re.sub(r"(按钮|按键|图标|入口|菜单)$", "", raw).strip(),
        re.sub(r"(按钮|按键|图标|入口|菜单|tab|Tab|TAB)$", "", raw, flags=re.I).strip(),
    ):
        if cand and cand not in out:
            out.append(cand)
    return out


def _bottom_tab_guess(
    label: str,
    screen_w: int,
    screen_h: int,
) -> Optional[Tuple[int, int, str]]:
    """底栏 Tab 启发式坐标（5 等分，y≈91%）。"""
    for variant in _label_variants(label):
        for key, idx in _BOTTOM_TAB_INDEX.items():
            if key in variant or variant in key:
                slot_w = max(48, screen_w // _BOTTOM_TAB_SLOTS)
                x = idx * slot_w + slot_w // 2
                tab_h = max(40, int(screen_h * 0.06))
                y = int(screen_h * 0.91) + tab_h // 2
                return x, y, f"底栏Tab「{key}」≈({x},{y})"
    return None


def _match_target_label(label: str, target_label: str) -> bool:
    a = (label or "").strip()
    b = (target_label or "").strip()
    if not a or not b:
        return False
    for va in _label_variants(a):
        for vb in _label_variants(b):
            if va == vb or va in vb or vb in va:
                return True
    return False


def _resolve_click_target(
    engine,
    screen_w: int,
    screen_h: int,
    *,
    label: str = "",
    x: int = 0,
    y: int = 0,
    coords_explicit: bool = False,
) -> Tuple[Optional[Tuple[int, int]], str, str]:
    """
    解析点击目标，返回 (position|None, method, detail)。
    method: label | hierarchy | ocr | bottom_tab | coordinate
    """
    if label and hasattr(engine, "click_by_label"):
        for variant in _label_variants(label):
            if engine.click_by_label(variant):
                return None, "label", f"无障碍文案「{variant}」"

    try:
        from driver.agent.Crawl.ui_discovery import (
            discover_clickables_from_hierarchy,
            discover_clickables_ocr,
        )

        if label:
            for t in discover_clickables_from_hierarchy(engine, screen_w, screen_h, max_items=48):
                if _match_target_label(label, t.label):
                    cx, cy = t.center
                    return (cx, cy), "hierarchy", f"层级「{t.label}」@({cx},{cy})"

            shot_path = None
            if hasattr(engine, "screenshot"):
                try:
                    shot_path = engine.screenshot()
                except Exception:
                    shot_path = None
            if shot_path:
                for t in discover_clickables_ocr(shot_path, screen_w, screen_h, max_items=24):
                    if _match_target_label(label, t.label):
                        cx, cy = t.center
                        return (cx, cy), "ocr", f"OCR「{t.label}」@({cx},{cy})"
    except Exception as e:
        SLog.w(TAG, f"resolve click target failed: {e}")

    if label:
        guess = _bottom_tab_guess(label, screen_w, screen_h)
        if guess:
            gx, gy, detail = guess
            return (gx, gy), "bottom_tab", detail

    if coords_explicit and x > 0 and y > 0:
        return (x, y), "coordinate", f"坐标({x},{y})"

    return None, "none", "未找到可点击目标"


def _run_mobile_click(
    sn: str,
    x: int,
    y: int,
    *,
    label: str = "",
    platform: str = "android",
    coords_explicit: bool = False,
) -> Dict[str, Any]:
    try:
        import builtins
        from driver.agent.Crawl.device_bootstrap import bootstrap_mobile_engine

        builtins.TARGET_DEVICE_SN = sn
        engine, (screen_w, screen_h) = bootstrap_mobile_engine(sn, platform)

        pos, method, detail = _resolve_click_target(
            engine,
            screen_w,
            screen_h,
            label=label,
            x=x,
            y=y,
            coords_explicit=coords_explicit,
        )
        if method == "label":
            _gesture_tick()
            return {"ok": True, "msg": detail, "method": method}

        if pos is None:
            return {
                "ok": False,
                "msg": detail or "未找到可点击目标，请指定坐标如：点击 1080,2450",
                "method": method,
            }

        cx, cy = pos
        if hasattr(engine, "click"):
            try:
                engine.click(None, position=(cx, cy), label="")
            except TypeError:
                engine.click(None, position=(cx, cy))
            _gesture_tick()
            return {"ok": True, "msg": detail, "method": method, "x": cx, "y": cy}
        return {"ok": False, "msg": "引擎不支持点击"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def _nav_route(text: str) -> Optional[Dict[str, str]]:
    t = text.lower()
    rules = [
        (("应用列表", "应用", "首页", "dashboard", "apps", "app list"), "AppList", "/report/apps"),
        (("设备", "device"), "DeviceManage", "/device"),
        (("定时", "schedule", "任务"), "Schedule", "/schedule"),
        (("时间线", "timeline", "日志"), "Timeline", "/timeline"),
        (("对话", "copilot", "dialogue", "助手"), "Dialogue", "/dialogue"),
        (("资源", "resource"), "ResourceList", "/resources"),
    ]
    for keys, name, path in rules:
        if any(k in t for k in keys):
            return {"name": name, "path": path}
    return None


_PACKAGE_RE = re.compile(r"(com\.[a-zA-Z0-9_.]+)")


def _normalize_app_token(token: str) -> str:
    t = (token or "").strip()
    t = re.sub(r"(?:应用|软件|程序|app|APP|应用程序)$", "", t, flags=re.I).strip()
    return t


def _looks_like_package(token: str) -> bool:
    t = (token or "").strip()
    return bool(t) and ("." in t) and not re.search(r"[\u4e00-\u9fff]", t)


def _name_match_score(query: str, candidate: str) -> int:
    q = _normalize_app_token(query).lower()
    c = _normalize_app_token(candidate).lower()
    if not q or not c:
        return 0
    if q == c:
        return 100
    if q in c:
        return 80 + int(len(q) / max(len(c), 1) * 15)
    if c in q:
        return 70 + int(len(c) / max(len(q), 1) * 15)
    return 0


def _pkg_match_score(query: str, package: str) -> int:
    q = _normalize_app_token(query).lower().replace(" ", "")
    p = (package or "").lower()
    if not q or not p:
        return 0
    if q == p:
        return 95
    if q in p:
        return 55
    seg = p.rsplit(".", 1)[-1]
    if q == seg or q in seg or seg in q:
        return 65
    return 0


def _package_from_env(env_raw: Any) -> str:
    """从 app.env 或 project.env 解析 Android 包名。"""
    try:
        from server.services.project_env import normalize_project_env, profile_snapshot

        snap = profile_snapshot(normalize_project_env(env_raw or {}))
        return ((snap.get("android") or {}).get("package") or "").strip()
    except Exception:
        return ""


def _package_for_app_record(app) -> str:
    """应用包名：优先 app.env，否则继承所属项目的 project.env。"""
    pkg = _package_from_env(getattr(app, "env", None))
    if pkg:
        return pkg
    project = getattr(app, "project", None)
    if project:
        return _package_from_env(getattr(project, "env", None))
    return ""


def _resolve_app_from_db(name: str, *, app_id: Optional[str] = None) -> Optional[Tuple[str, str, str]]:
    """从 MiniOrange 项目/应用库解析 (package, source, display_name)。"""
    try:
        from server.core.database import SessionLocal
        from server.models.project import App, Project
        from sqlalchemy.orm import joinedload

        session = SessionLocal()
        try:
            best_pkg = ""
            best_name = ""
            best_score = 0

            app_query = session.query(App).options(joinedload(App.project))
            if app_id:
                app_query = app_query.filter(App.id == str(app_id))
            for app in app_query.all():
                pkg = _package_for_app_record(app)
                project_name = app.project.name if app.project else ""
                score = max(
                    _name_match_score(name, app.name),
                    _name_match_score(name, project_name),
                    _pkg_match_score(name, pkg),
                )
                if score > best_score:
                    best_score = score
                    best_pkg = pkg
                    best_name = app.name or project_name

            for project in session.query(Project).all():
                pkg = _package_from_env(project.env)
                score = max(_name_match_score(name, project.name), _pkg_match_score(name, pkg))
                if score > best_score:
                    best_score = score
                    best_pkg = pkg
                    best_name = project.name

            if best_score >= 55 and best_pkg:
                return best_pkg, "db", best_name
            if best_score >= 55 and not best_pkg:
                SLog.w(
                    TAG,
                    f"app name matched「{best_name}」but package empty; "
                    "check project env android.package",
                )
        finally:
            session.close()
    except Exception as e:
        SLog.w(TAG, f"resolve app from db failed: {e}")
    return None


def _resolve_app_from_device(sn: str, name: str) -> Optional[Tuple[str, str, str]]:
    """从手机已安装应用解析 (package, source, display_name)。"""
    if not sn:
        return None
    try:
        import uiautomator2 as u2

        d = u2.connect(str(sn))
        out = d.shell("pm list packages -3").output or ""
        pkgs = [
            line.replace("package:", "").strip()
            for line in out.splitlines()
            if line.strip().startswith("package:")
        ]
        best_pkg = ""
        best_label = ""
        best_score = 0
        for pkg in pkgs:
            label = ""
            try:
                info = d.app_info(pkg) or {}
                label = (info.get("label") or info.get("name") or "").strip()
            except Exception:
                label = ""
            score = max(_name_match_score(name, label), _pkg_match_score(name, pkg))
            if score > best_score:
                best_score = score
                best_pkg = pkg
                best_label = label or pkg
        if best_score >= 55 and best_pkg:
            return best_pkg, "device", best_label
    except Exception as e:
        SLog.w(TAG, f"resolve app from device failed: {e}")
    return None


def resolve_app_package(
    name: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
) -> Tuple[Optional[str], str, str]:
    """
    应用名/别名 → 包名。
    返回 (package|None, source, display_name)。
    """
    ctx = context or {}
    token = _normalize_app_token(name)
    if not token:
        return None, "", ""

    if _looks_like_package(token):
        return token, "package", token

    pkg_match = _PACKAGE_RE.search(token)
    if pkg_match:
        pkg = pkg_match.group(1)
        return pkg, "package", pkg

    ctx_pkg = (ctx.get("package") or ctx.get("android_package") or "").strip()
    ctx_name = (ctx.get("app_name") or ctx.get("appName") or "").strip()
    if ctx_pkg and (not ctx_name or _name_match_score(token, ctx_name) >= 55):
        return ctx_pkg, "context", ctx_name or ctx_pkg

    db_hit = _resolve_app_from_db(token, app_id=ctx.get("app_id") or ctx.get("appId"))
    if db_hit:
        return db_hit

    device_hit = _resolve_app_from_device(sn, token) if sn else None
    if device_hit:
        return device_hit

    return None, "", ""


def _extract_app_identifier(raw: str, operation: str) -> str:
    quoted = re.search(r"[「『\"']([^」』\"']+)[」』\"']", raw)
    if quoted:
        return _normalize_app_token(quoted.group(1))
    if operation == "open":
        m = re.search(r"(?:打开|启动|open|launch)\s*(.+)$", raw, re.I)
    else:
        m = re.search(r"(?:关闭|退出|关掉|关|close|kill|force[- ]?stop)\s*(.+)$", raw, re.I)
    if not m:
        return ""
    return _normalize_app_token(m.group(1))


def _plan_app_action(
    raw: str,
    operation: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
) -> Optional[Dict[str, Any]]:
    """operation: open | close"""
    pkg_match = _PACKAGE_RE.search(raw)
    if pkg_match:
        pkg = pkg_match.group(1)
        display = pkg
        source = "package"
    else:
        name = _extract_app_identifier(raw, operation)
        if not name:
            return None
        pkg, source, display = resolve_app_package(name, sn=sn, context=context)
        if not pkg:
            verb = "打开" if operation == "open" else "关闭"
            return {
                "error": (
                    f"未找到应用「{name}」对应的包名。"
                    f"请在项目「运行环境」中配置 Android 包名，或确认手机已安装该应用。"
                ),
                "reply_hint": f"{verb} {name}",
            }

    is_open = operation == "open"
    kind = "open_app" if is_open else "close_app"
    op = "start" if is_open else "close"
    verb = "启动" if is_open else "关闭"
    label = display or pkg
    src_hint = {"db": "项目环境", "device": "本机", "context": "上下文", "package": "包名"}.get(source, source)
    return {
        "step": {
            "kind": kind,
            "nodeCode": "public/window",
            "platform": "mobile",
            "data": {
                "operation": op,
                "target_mobile": pkg,
                "restart": is_open,
                "platform": "mobile",
            },
            "summary": f"{verb}应用 {label} ({pkg})",
            "app_name": label,
            "package": pkg,
            "resolve_source": source,
        },
        "reply": f"将{verb}「{label}」→ {pkg}（{src_hint}）",
    }


# 多指令拆分：标点 / 连接词 / 连续动词
_SPLIT_DELIM_RE = re.compile(
    r"(?:"
    r"然后|接着|之后|接下来|再然后|然后再|并且|并|"
    r"and then|then|after that|"
    r"[,，;；\n|]|"
    r"→|->"
    r")+",
    re.I,
)
_VERB_BOUNDARY_RE = re.compile(
    r"(?=(?:"
    r"打开|启动|关闭|退出|关掉|"
    r"点击|点一下|"
    r"上滑|下滑|左滑|右滑|滑动|滑一下|"
    r"截图|截屏|等待|返回|后退|"
    r"open|launch|close|kill|click|tap|swipe|screenshot|wait|back"
    r"))",
    re.I,
)
_NUMBERED_STEP_RE = re.compile(r"(?:^|\s)\d+[.、)\）]\s*")


def _normalize_segment(segment: str) -> str:
    seg = (segment or "").strip()
    seg = re.sub(r"^(?:再|然后|接着|并|并且|接下来)\s*", "", seg, flags=re.I).strip()
    return seg


def _split_commands(text: str) -> List[str]:
    """将一条用户输入拆成多个可独立规划的子指令。"""
    raw = (text or "").strip()
    if not raw:
        return []

    protected: Dict[str, str] = {}

    def _protect(match: re.Match) -> str:
        key = f"__MO_{len(protected)}__"
        protected[key] = match.group(0)
        return key

    shielded = _PACKAGE_RE.sub(_protect, raw)
    shielded = re.sub(r"\d{2,4}\s*[,，]\s*\d{2,4}", _protect, shielded)

    parts: List[str] = []
    if _NUMBERED_STEP_RE.search(shielded):
        chunks = _NUMBERED_STEP_RE.split(shielded)
        parts = [c.strip() for c in chunks if c and c.strip()]
    else:
        chunks = _SPLIT_DELIM_RE.split(shielded)
        parts = [c.strip() for c in chunks if c and c.strip()]

    if len(parts) <= 1:
        parts = [shielded]

    expanded: List[str] = []
    for part in parts:
        subs = [s.strip() for s in _VERB_BOUNDARY_RE.split(part) if s and s.strip()]
        expanded.extend(subs if subs else [part])

    restored: List[str] = []
    seen = set()
    for part in expanded:
        seg = part
        for key, val in protected.items():
            seg = seg.replace(key, val)
        seg = _normalize_segment(seg)
        if seg and seg not in seen:
            seen.add(seg)
            restored.append(seg)
    return restored or [raw]


def _inject_step_waits(steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """启动应用后若紧跟点击/滑动，自动插入短暂等待。"""
    if not steps:
        return steps
    out: List[Dict[str, Any]] = []
    for i, step in enumerate(steps):
        out.append(step)
        if step.get("kind") != "open_app" or i + 1 >= len(steps):
            continue
        nxt = steps[i + 1]
        if nxt.get("kind") not in ("click", "swipe"):
            continue
        if out and out[-1].get("kind") == "ability" and out[-1].get("nodeCode") == "cfs/sleep":
            continue
        out.append({
            "kind": "ability",
            "nodeCode": "cfs/sleep",
            "platform": "common",
            "data": {"seconds": 2},
            "summary": "等待应用就绪 2 秒",
        })
    return out


def _plan_segment(
    raw: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
) -> Dict[str, Any]:
    """单条子指令 → steps + reply_parts + errors。"""
    segment = _normalize_segment(raw)
    if not segment:
        return {"steps": [], "reply_parts": [], "errors": []}

    steps: List[Dict[str, Any]] = []
    reply_parts: List[str] = []
    errors: List[str] = []

    open_intent = bool(re.search(r"打开|启动|open|launch", segment, re.I))
    close_intent = bool(re.search(r"关闭|退出|close|kill|force[- ]?stop", segment, re.I))
    if open_intent and not close_intent:
        app_plan = _plan_app_action(segment, "open", sn=sn, context=context)
        if app_plan:
            if app_plan.get("error"):
                errors.append(app_plan["error"])
            else:
                steps.append(app_plan["step"])
                reply_parts.append(app_plan["reply"])
    elif close_intent and not open_intent:
        app_plan = _plan_app_action(segment, "close", sn=sn, context=context)
        if app_plan:
            if app_plan.get("error"):
                errors.append(app_plan["error"])
            else:
                steps.append(app_plan["step"])
                reply_parts.append(app_plan["reply"])

    coord = re.search(r"(\d{2,4})\s*[,，]\s*(\d{2,4})", segment)
    label_m = re.search(r"[「『\"']([^」』\"']+)[」』\"']", segment) or re.search(
        r"点击\s*([^\s,，]+)", segment
    )
    if re.search(r"点击|点一下|tap|click", segment, re.I):
        label = label_m.group(1).strip() if label_m else ""
        coords_explicit = bool(coord)
        if coord:
            x, y = int(coord.group(1)), int(coord.group(2))
        else:
            x, y = _DEFAULT_CLICK_XY
        steps.append({
            "kind": "click",
            "x": x,
            "y": y,
            "label": label,
            "coords_explicit": coords_explicit,
            "summary": f"点击「{label or (f'{x},{y}' if coords_explicit else '目标')}」",
        })
        reply_parts.append(steps[-1]["summary"])

    swipe_dir = None
    if re.search(r"上滑|向上滑|swipe\s*up", segment, re.I):
        swipe_dir = "up"
    elif re.search(r"下滑|向下滑|swipe\s*down", segment, re.I):
        swipe_dir = "down"
    elif re.search(r"左滑|向左", segment, re.I):
        swipe_dir = "left"
    elif re.search(r"右滑|向右", segment, re.I):
        swipe_dir = "right"
    elif re.search(r"滑动|滑一下|scroll|swipe", segment, re.I):
        swipe_dir = "up"
    if swipe_dir:
        steps.append({"kind": "swipe", "direction": swipe_dir, "summary": f"滑动 {swipe_dir}"})
        reply_parts.append(f"滑动 {swipe_dir}")

    if re.search(r"截图|截屏|screenshot", segment, re.I):
        steps.append({
            "kind": "ability",
            "nodeCode": "tools/screenshot",
            "platform": "mobile",
            "data": {"platform": "mobile"},
            "summary": "截图",
        })
        reply_parts.append("截图")

    if re.search(r"等待|wait|sleep", segment, re.I):
        sec_m = re.search(r"(\d+)\s*秒", segment)
        sec = int(sec_m.group(1)) if sec_m else 2
        steps.append({
            "kind": "ability",
            "nodeCode": "cfs/sleep",
            "platform": "common",
            "data": {"seconds": sec},
            "summary": f"等待 {sec} 秒",
        })
        reply_parts.append(f"等待 {sec}s")

    if re.search(r"返回|后退|back", segment, re.I) and not re.search(r"页面", segment):
        steps.append({"kind": "back", "summary": "返回（累计手势后执行）"})
        reply_parts.append("登记返回键")

    if not steps and not errors:
        errors.append(f"未识别子指令：{segment}")

    return {"steps": steps, "reply_parts": reply_parts, "errors": errors}


def plan_message(
    text: str,
    *,
    sn: Optional[str] = None,
    context: Optional[Dict] = None,
) -> Dict[str, Any]:
    """将用户输入拆解为步骤列表 + 回复文案。"""
    raw = (text or "").strip()
    if not raw:
        return {"reply": "请输入指令，例如：打开 造物相机 / 关闭 美团 / 点击 600,1200", "steps": [], "navigate": None}

    if raw.startswith("/"):
        cmd = raw[1:].strip().lower()
        nav = _nav_route(cmd)
        if nav:
            return {
                "reply": f"切换到：{nav['name']}",
                "steps": [],
                "navigate": nav,
            }

    nav = _nav_route(raw)
    if nav and len(raw) < 24 and len(_split_commands(raw)) <= 1:
        return {"reply": f"切换到页面：{nav['path']}", "steps": [], "navigate": nav}

    segments = _split_commands(raw)
    steps: List[Dict[str, Any]] = []
    reply_parts: List[str] = []
    segment_errors: List[str] = []

    for seg in segments:
        planned = _plan_segment(seg, sn=sn, context=context)
        steps.extend(planned.get("steps") or [])
        reply_parts.extend(planned.get("reply_parts") or [])
        segment_errors.extend(planned.get("errors") or [])

    steps = _inject_step_waits(steps)

    if not steps and segment_errors:
        return {
            "reply": "\n".join(segment_errors),
            "steps": [],
            "navigate": None,
            "sn": sn,
            "auto_run": False,
        }

    if not steps:
        return {
            "reply": (
                "未识别指令。可尝试：\n"
                "· 打开 造物相机，点击我的，上滑\n"
                "· 打开 造物相机 / 关闭 美团\n"
                "· 打开 com.xxx.app（也支持包名）\n"
                "· 点击 600,1200 或 点击「我的」\n"
                "· 上滑 / 左滑\n"
                "· 去应用列表 / 设备管理\n"
                "· / 查看快捷命令"
            ),
            "steps": [],
            "navigate": None,
        }

    if not sn and any(s.get("kind") in ("click", "swipe", "open_app", "close_app", "ability") for s in steps):
        reply_parts.append("（未选设备：请先在顶部选择在线手机）")

    reply = " → ".join(reply_parts) if reply_parts else "好的"
    if len(steps) > 1:
        reply = f"共 {len(steps)} 步：{reply}"
    if segment_errors:
        reply += "\n⚠ " + "；".join(segment_errors)

    return {
        "reply": reply,
        "steps": steps,
        "navigate": None,
        "sn": sn,
        "auto_run": True,
    }


def execute_steps(
    steps: List[Dict[str, Any]],
    *,
    sn: Optional[str] = None,
    platform: str = "android",
) -> List[Dict[str, Any]]:
    """逐步执行并返回每步结果（供前端展示判断循环）。"""
    import builtins

    if sn:
        builtins.TARGET_DEVICE_SN = str(sn)
        try:
            from driver.agent.Memory import memory_manager
            memory_manager.short_term.set_global("run_device_sn", str(sn))
            memory_manager.short_term.set_global("platform", platform)
        except Exception:
            pass

    results: List[Dict[str, Any]] = []
    for i, step in enumerate(steps or []):
        kind = step.get("kind", "")
        summary = step.get("summary", kind)
        out: Dict[str, Any] = {"index": i, "summary": summary, "ok": False, "msg": ""}

        if kind in ("open_app", "close_app"):
            payload = _task_payload(
                step.get("nodeCode", "public/window"),
                platform=step.get("platform", "mobile"),
                data=step.get("data", {}),
            )
            r = _execute_ability(payload)
            out.update(r)
            if r.get("ok") and step.get("resolve_source"):
                out["msg"] = (
                    f"{out.get('msg', '')} "
                    f"[{step.get('app_name', '')} → {step.get('package', '')} "
                    f"via {step.get('resolve_source')}]"
                ).strip()

        elif kind == "click":
            if not sn:
                out["msg"] = "未选择设备"
            else:
                r = _run_mobile_click(
                    sn,
                    int(step.get("x", 0)),
                    int(step.get("y", 0)),
                    label=step.get("label", ""),
                    platform=platform,
                    coords_explicit=bool(step.get("coords_explicit")),
                )
                out.update(r)

        elif kind == "swipe":
            if not sn:
                out["msg"] = "未选择设备"
            else:
                r = _run_mobile_swipe(sn, step.get("direction", "up"), platform)
                out.update(r)

        elif kind == "back":
            _schedule_back()
            out["ok"] = True
            out["msg"] = "已登记返回（满 50 次手势后执行一次）"

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

        results.append(out)
        SLog.i(TAG, f"Step {i}: {summary} -> ok={out.get('ok')} {out.get('msg')}")

    return results
