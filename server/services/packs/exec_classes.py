# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""执行能力四类：前置 / 步骤 / 预期 / 通用。

扩展包控制台 Tab 由这类元数据下发，前端不再写死「能力/恢复/…」。
对照 docs/四类能力.md。YAML 能力按 id 归类；编排能力（筛账号、看图决策等）合成条目。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

# 控制台主 Tab。恢复/知识/判定仍是包 kind，由 rPacks 追加。
EXEC_KINDS: Tuple[str, ...] = ("prep", "step", "expect", "generic")

KIND_META: Dict[str, Dict[str, str]] = {
    "prep": {"label": "前置操作", "desc": "开跑前：账号、登录、环境"},
    "step": {"label": "操作步骤", "desc": "按编号走到场景"},
    "expect": {"label": "预期结果", "desc": "做成之后只看、不准再点"},
    "generic": {"label": "通用能力", "desc": "点击、滑动、等待：哪一列都能调"},
}

# plugins/capabilities/*.yaml id → 四类。未列出的 YAML 默认进通用。
CAP_CLASS: Dict[str, str] = {
    "wake_screen": "prep",
    "dismiss_keyguard": "prep",
    "clear_app_cache": "prep",
    "kill_app": "prep",
    "install_apk": "prep",
    "read_device_data": "prep",
    "probe_device_state": "prep",
    "get_app_version": "prep",
    "get_foreground_app": "prep",
    "persona_subtask": "prep",
    "assert_visual": "expect",
    "tap_element": "generic",
    "multi_tap": "generic",
    "long_press_element": "generic",
    "swipe_direction": "generic",
    "swipe_element_to_element": "generic",
    "input_text": "generic",
    "press_key": "generic",
    "set_clipboard": "generic",
    "launch_app": "generic",
    "close_app": "generic",
    "wait_ms": "generic",
    "wait_screen_ready": "generic",
    "exec_script": "generic",
    "human_confirm": "generic",
    "human_input_text": "generic",
    "human_choice_single": "generic",
    "human_choice_multiple": "generic",
    "human_upload_image": "generic",
    "human_acknowledge": "generic",
}

# 编排 / 服务：没有 YAML 能力文件，控制台仍要能看见。
RUNTIME: Tuple[Dict[str, str], ...] = (
    {"id": "pick_device", "exec_class": "prep", "title": "申请执行设备",
     "summary": "按用例从当前环境可用设备里申请并占用。人可以不选设备。App+Web / A-B 占多台，本趟仍在主设备执行。"},
    {"id": "pick_account", "exec_class": "prep", "title": "租账号",
     "summary": "按场景和环境从账号管理里租号。标签优先，占用/租用中的往后排。"},
    {"id": "case-scene", "exec_class": "prep", "title": "用例场景理解",
     "summary": "读用例原文判断登录闸门、占什么设备、前置每条 kind。只出枚举。占设备看这趟要操作哪些端。"},
    {"id": "inspect-session", "exec_class": "prep", "title": "观察登录态",
     "summary": "看截图判断已登录 / 游客 / 微信一键不可测。只观察，不切号。业务用例前置重新登录后写入 RunContext，后续跳过。"},
    {"id": "inspect-env", "exec_class": "prep", "title": "观察客户端环境",
     "summary": "看截图判断开发/测试/预发/正式。登录页没有角标记 unknown，不是失败。"},
    {"id": "env_align", "exec_class": "prep", "title": "切换执行环境",
     "summary": "开跑前把 App/PC 切到本趟环境。Web/Server 用地址区分，跳过。"},
    {"id": "session_gate", "exec_class": "prep", "title": "登录态闸门",
     "summary": "非登录用例前置退出并重新登录；登录相关用例不自动登录。对不齐则标不足 / 不可测。"},
    {"id": "agent-restart", "exec_class": "prep", "title": "开场是否重开应用",
     "summary": "开跑前判断要不要先强关再启动目标包。"},
    {"id": "precondition-parse", "exec_class": "prep", "title": "前置条件解析",
     "summary": "把飞书「前置条件」拆成可执行检查项。"},
    {"id": "persona-clear-cache", "exec_class": "prep", "title": "拟人化 · 清缓存",
     "summary": "无特权时从设置里清除应用数据。"},
    {"id": "persona-force-stop", "exec_class": "prep", "title": "拟人化 · 强停应用",
     "summary": "从设置里强制停止应用。"},
    {"id": "persona-allow-install", "exec_class": "prep", "title": "拟人化 · 允许安装",
     "summary": "点掉未知来源安装许可。"},
    {"id": "agent-decide", "exec_class": "step", "title": "看图决策",
     "summary": "看当前截图，每次只决定下一个动作。主归属步骤；会话对齐会借用。"},
    {"id": "plan-overview", "exec_class": "step", "title": "规划事件序列",
     "summary": "纯文本排出事件，不看截图（Plan 模式）。"},
    {"id": "single-step-replan", "exec_class": "step", "title": "单步重规划",
     "summary": "某步失败或偏离时只重规划这一步。"},
    {"id": "case-step-parse", "exec_class": "step", "title": "测试步骤解析",
     "summary": "飞书「测试步骤」拆成带编号的操作列表。"},
    {"id": "copilot-rewrite", "exec_class": "step", "title": "步骤指令改写",
     "summary": "把步骤改写成一条可规划指令。"},
    {"id": "assert-vision", "exec_class": "expect", "title": "视觉校验",
     "summary": "看操作之后的新图，判断这句预期成不成立。不准再点。"},
    {"id": "playwright_check", "exec_class": "expect", "title": "Web DOM 校验",
     "summary": "URL / DOM 能判的文案、到页、登录；能判则不看图。"},
    {"id": "expect_catalog", "exec_class": "expect", "title": "质检分类",
     "summary": "把预期句子打成可验 / 无法验证 / 无法识别。"},
    {"id": "goal-extract", "exec_class": "expect", "title": "目标抽取",
     "summary": "有预期列则当检查点；没有才抽取。"},
    {"id": "case-expected-parse", "exec_class": "expect", "title": "预期效果解析",
     "summary": "飞书预期列对齐到步骤编号。"},
    {"id": "expectation-claims", "exec_class": "expect", "title": "断言拆分",
     "summary": "一条预期拆成可独立校验的观察点。"},
    {"id": "locate-vision", "exec_class": "generic", "title": "视觉定位",
     "summary": "根据截图给出点击 / 输入坐标。不判断预期是否成立。"},
    {"id": "hitl-composer", "exec_class": "generic", "title": "问人话术",
     "summary": "把卡住的步骤改写成问人的话。只要填进框的数据。"},
)


def class_of_cap(cap_id: str) -> str:
    raw = str(cap_id or "").strip()
    if raw in CAP_CLASS:
        return CAP_CLASS[raw]
    return CAP_CLASS.get(raw.replace("-", "_"), "generic")


_PREP_RUNTIME = {
    "pick_account", "pick_device", "case_scene", "inspect_session",
    "inspect_env", "env_align",
    "session_gate", "session_align", "skip_restart", "check_logged",
    "check_not_logged", "check_sim", "check_wechat", "grant_perm",
    "keep_permission", "bind_account", "clear_cache", "clear_app_cache",
    "wake_screen", "dismiss_keyguard", "get_app_version", "get_foreground_app",
    "kill_app", "install_apk", "read_device_data", "probe_device_state",
    "persona_subtask",
}
_EXPECT_CAPS = {
    "assert_visual", "assert_goal", "assert_skip", "playwright_check",
    "assert_vision",
}


def lane_for_event(
    *,
    cap: str = "",
    prep_done: bool = False,
    session_mode: bool = False,
    seq_phase: str = "",
) -> str:
    """这条事件服务哪一列：前置 / 步骤 / 预期。通用点击跟当前阶段走。"""
    if session_mode or not prep_done:
        return "prep"
    if str(seq_phase or "") == "check":
        return "expect"
    c = str(cap or "").strip().lower().replace("-", "_")
    if c in _EXPECT_CAPS or class_of_cap(cap) == "expect":
        return "expect"
    if c in _PREP_RUNTIME or class_of_cap(cap) == "prep":
        return "prep"
    return "step"


def runtime_rows(exec_class: str) -> List[dict[str, Any]]:
    out: List[dict[str, Any]] = []
    for spec in RUNTIME:
        if spec["exec_class"] != exec_class:
            continue
        cid = spec["id"]
        out.append({
            "uid": f"runtime/{exec_class}/{cid}",
            "kind": exec_class,
            "id": cid,
            "title": spec["title"],
            "enabled": True,
            "lifecycle": "active",
            "provider": "platform",
            "owner": "@platform",
            "root": "builtin",
            "overridden_by": "",
            "scope": {
                "platforms": ["android", "ios", "web"],
                "app_ids": [],
                "visible_to": ["case", "system"],
            },
            "when": "",
            "summary": spec["summary"],
            "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
            "source_path": "",
            "detail": {
                "origin": "runtime",
                "exec_class": exec_class,
                "writable": False,
            },
        })
    return out
