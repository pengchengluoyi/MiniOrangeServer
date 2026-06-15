# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Server / 执行器能力目录（供设置页 Skills 展示）。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

ParamItem = Dict[str, str]
OperationItem = Dict[str, Any]
ComponentItem = Dict[str, Any]
ServerItem = Dict[str, Any]


def _param(name: str, desc: str = "", *, example: str = "") -> ParamItem:
    row: ParamItem = {"name": name, "desc": desc}
    if example:
        row["example"] = example
    return row


def _op(
    op_id: str,
    name: str,
    description: str,
    *,
    params: Optional[List[ParamItem]] = None,
    invoke: str = "",
    triggers: Optional[List[str]] = None,
    examples: Optional[List[str]] = None,
    platforms: Optional[List[str]] = None,
    risk: str = "",
) -> OperationItem:
    return {
        "id": op_id,
        "name": name,
        "description": description,
        "params": params or [],
        "invoke": invoke,
        "triggers": triggers or [],
        "examples": examples or [],
        "platforms": platforms or [],
        "risk": risk,
    }


def _component(
    *,
    node_code: str,
    name: str,
    category: str,
    description: str,
    invoke_type: str = "tentacle",
    platforms: Optional[List[str]] = None,
    risk: str = "",
    operations: List[OperationItem],
) -> ComponentItem:
    return {
        "node_code": node_code,
        "name": name,
        "category": category,
        "description": description,
        "invoke_type": invoke_type,
        "platforms": platforms or [],
        "risk": risk,
        "operations": operations,
    }


def list_executor_components() -> List[ComponentItem]:
    """执行器：按 Tentacle nodeCode 或引擎直连 API 聚合，操作挂在参数下。"""
    return [
        _component(
            node_code="public/window",
            name="窗口",
            category="应用生命周期",
            description="跨平台应用启停与切换（移动端为包名）。",
            platforms=["android", "ios", "mac", "windows", "web"],
            operations=[
                _op(
                    "window_start",
                    "启动应用",
                    "启动或切换至目标应用，可选先杀进程再拉起。",
                    params=[
                        _param("operation", "固定 start"),
                        _param("target_mobile", "Android 包名 / iOS Bundle", example="com.example.app"),
                        _param("restart", "是否重启", example="true"),
                        _param("platform", "平台", example="mobile"),
                    ],
                    invoke="Tentacle → public/window",
                    triggers=["Copilot「打开 X」", "Plan kind=open_app"],
                    examples=["打开 造物相机", "打开 com.mathmagic.zaohaowu"],
                    platforms=["android", "ios", "mac", "windows", "web"],
                    risk="移动端需要包名/Bundle；桌面端需要应用名或进程名；web 需要 URL。",
                ),
                _op(
                    "window_close",
                    "关闭应用",
                    "结束指定包名进程。",
                    params=[
                        _param("operation", "固定 close"),
                        _param("target_mobile", "包名"),
                    ],
                    invoke="Tentacle → public/window",
                    triggers=["Copilot「关闭 X」", "Plan kind=close_app"],
                    examples=["关闭 微信"],
                    platforms=["android", "ios", "mac", "windows"],
                    risk="iOS/桌面关闭能力依赖执行器权限；web 通常应关闭标签页而不是进程。",
                ),
                _op(
                    "window_switch",
                    "切换应用",
                    "切到前台，不强制杀进程重启。",
                    params=[
                        _param("operation", "固定 switch"),
                        _param("target_mobile", "包名"),
                    ],
                    invoke="Tentacle → public/window",
                    platforms=["android", "ios", "mac", "windows"],
                ),
            ],
        ),
        _component(
            node_code="public/gesture",
            name="手势",
            category="手势",
            description="点击、长按、滑动等触控注入。",
            platforms=["android", "ios", "web"],
            operations=[
                _op(
                    "gesture_click",
                    "点击 / 长按",
                    "坐标点击或定位命中后的热区点击。",
                    params=[
                        _param("sub_type", "click | long_press | double", example="click"),
                        _param("position", "[x, y] 或归一化坐标"),
                        _param("label", "可选，审计用文案"),
                    ],
                    invoke="Tentacle → public/gesture；Copilot 经 _run_mobile_click",
                    triggers=["Plan kind=click", "定位仲裁命中后"],
                    examples=["点击「同意」", "点击 600,1200"],
                    platforms=["android", "ios", "web"],
                    risk="坐标点击依赖屏幕尺寸；web 优先使用 DOM selector，移动端优先走定位仲裁。",
                ),
                _op(
                    "gesture_swipe",
                    "滑动",
                    "四向滑动；Copilot 口语映射为 swipe_norm。",
                    params=[
                        _param("sub_type", "固定 drag"),
                        _param("direction", "up | down | left | right", example="up"),
                    ],
                    invoke="Copilot → _run_mobile_swipe（内部 swipe_norm）",
                    triggers=["Plan kind=swipe"],
                    examples=["上滑", "下滑", "左滑"],
                    platforms=["android", "ios", "web"],
                    risk="web 为滚动语义；移动端为触控滑动。",
                ),
            ],
        ),
        _component(
            node_code="",
            name="移动引擎直连",
            category="输入与系统",
            description="不经过独立 Tentacle 节点，由 Server copilot_service 直接调引擎 / adb。",
            invoke_type="engine",
            platforms=["android"],
            risk="当前引擎直连能力主要面向 Android；iOS/web/桌面需要对应执行器适配。",
            operations=[
                _op(
                    "mobile_input",
                    "文本输入",
                    "u2 EditText 聚焦填字；可绑定上一步 click 的 target_rect。",
                    params=[
                        _param("text", "要输入的内容"),
                        _param("field_hint", "字段提示，如 账号、密码"),
                        _param("bind_last_click", "是否用上一步点击区域聚焦", example="true"),
                    ],
                    invoke="copilot_service._run_mobile_input（Plan kind=input）",
                    triggers=["对话 / 飞书步骤", "点击X输入框,输入:Y"],
                    examples=["输入手机号 13800138000", "点击密码输入框,输入：123456"],
                    platforms=["android"],
                    risk="依赖 u2 EditText 与键盘状态；优先绑定上一点击区域。",
                ),
                _op(
                    "mobile_back",
                    "返回键",
                    "注入 Android BACK；可立即或累计手势后执行。",
                    params=[_param("immediate", "是否立即执行", example="true")],
                    invoke="copilot_service._run_mobile_back（Plan kind=back）",
                    triggers=["Copilot「返回」", "页面恢复"],
                    examples=["返回", "后退"],
                    platforms=["android"],
                ),
                _op(
                    "shell_pm_clear",
                    "清理应用数据",
                    "adb shell pm clear <package>。",
                    params=[_param("package", "应用包名")],
                    invoke="case_precondition_service._clear_app_data",
                    triggers=["飞书前置「应用无缓存」"],
                    examples=["前置：1. 应用无缓存"],
                    platforms=["android"],
                    risk="会清空应用数据；仅应用于明确要求无缓存/未登录的前置。",
                ),
                _op(
                    "screen_ready",
                    "亮屏解锁",
                    "检测锁屏/黑屏并唤醒解锁。",
                    params=[],
                    invoke="engine.ensure_screen_ready",
                    triggers=["点击前", "设备准备 trace"],
                    platforms=["android"],
                ),
                _op(
                    "foreground_package",
                    "读取前台包名",
                    "用于跨 App 预期与离屏观察。",
                    params=[],
                    invoke="app_automation_service.guard_test_app_foreground",
                    triggers=["预期校验", "步骤前台观察"],
                    platforms=["android"],
                ),
            ],
        ),
        _component(
            node_code="tools/screenshot",
            name="截图",
            category="感知",
            description="截取当前设备画面。",
            platforms=["android", "ios", "mac", "windows", "web"],
            operations=[
                _op(
                    "screenshot",
                    "设备截图",
                    "供 OCR / CLIP / 回归回放使用。",
                    params=[_param("tag", "落盘标签", example="step_0_click")],
                    invoke="Tentacle → tools/screenshot；或 regression_capture",
                    triggers=["定位通道", "回归 run_id", "Plan ability"],
                    platforms=["android", "ios", "mac", "windows", "web"],
                ),
            ],
        ),
        _component(
            node_code="tools/ocr",
            name="OCR",
            category="感知",
            description="全屏文字识别。",
            platforms=["android", "ios", "mac", "windows", "web"],
            operations=[
                _op(
                    "ocr",
                    "屏上 OCR",
                    "产出可点击文本候选，供 OCR 定位通道。",
                    invoke="Tentacle → tools/ocr / public/ocr",
                    triggers=["多通道定位 OCR 通道"],
                    platforms=["android", "ios", "mac", "windows", "web"],
                    risk="OCR 只给文字候选；无文字图标需结合 CLIP/DOM/Hierarchy。",
                ),
            ],
        ),
        _component(
            node_code="tools/dump_dom",
            name="布局树",
            category="感知",
            description="导出 Hierarchy / DOM。",
            platforms=["android", "ios", "web"],
            operations=[
                _op(
                    "dump_dom",
                    "布局树导出",
                    "调试与结构分析。",
                    invoke="Tentacle → tools/dump_dom",
                    platforms=["android", "ios", "web"],
                    risk="Android 为 hierarchy；web 为 DOM；iOS 依赖 WebDriverAgent/无障碍树。",
                ),
            ],
        ),
        _component(
            node_code="tools/keyevent",
            name="按键",
            category="系统",
            description="发送系统按键事件。",
            platforms=["android", "mac", "windows"],
            operations=[
                _op(
                    "keyevent",
                    "按键事件",
                    "如 HOME、BACK 等 keyevent。",
                    params=[_param("key", "按键名或 keycode")],
                    invoke="Tentacle → tools/keyevent",
                    platforms=["android", "mac", "windows"],
                    risk="不同平台 keycode 不一致；Plan 必须根据 platform 转换。",
                ),
            ],
        ),
        _component(
            node_code="cfs/sleep",
            name="等待",
            category="控制",
            description="固定时长等待。",
            platforms=["android", "ios", "mac", "windows", "web"],
            operations=[
                _op(
                    "sleep",
                    "等待",
                    "步骤间 UI 稳定。",
                    params=[_param("duration", "秒或毫秒", example="2")],
                    invoke="Tentacle → cfs/sleep；Plan 自动插入 wait",
                    examples=["等待 2 秒"],
                    platforms=["android", "ios", "mac", "windows", "web"],
                ),
            ],
        ),
        _component(
            node_code="cfs/mAssert",
            name="断言",
            category="控制",
            description="工作流条件断言节点。",
            platforms=["android", "ios", "mac", "windows", "web"],
            operations=[
                _op(
                    "assert",
                    "断言",
                    "用于可视化工作流编排。",
                    invoke="Tentacle → cfs/mAssert",
                    platforms=["android", "ios", "mac", "windows", "web"],
                ),
            ],
        ),
        _component(
            node_code="",
            name="定位内部通道",
            category="感知",
            description="无独立对外 nodeCode，由 locate 模块在点击时内部调用。",
            invoke_type="internal",
            platforms=["android"],
            risk="当前定位内部通道主要服务 Android 移动端。",
            operations=[
                _op(
                    "hierarchy_clickables",
                    "Hierarchy 可点击遍历",
                    "从无障碍树收集候选。",
                    invoke="locate.channels.collect_text_channels",
                    triggers=["点击定位多通道"],
                    platforms=["android"],
                ),
                _op(
                    "clip_vision",
                    "CLIP 视觉匹配",
                    "截图 patch 与文本 embedding 相似度。",
                    invoke="clip_locate_service / icon_row 通道",
                    triggers=["无字图标、登录 icon 行"],
                    platforms=["android"],
                    risk="视觉匹配受截图质量、缩放、键盘遮挡影响。",
                ),
            ],
        ),
    ]


def list_server_groups() -> List[Dict[str, Any]]:
    """Server 编排能力：无 nodeCode，按模块分组并标明调用入口。"""
    return [
        {
            "key": "copilot",
            "title": "Copilot 规划与执行",
            "description": "对话流 / 飞书步骤拆解为 Plan 并逐步执行。",
            "items": [
                {
                    "id": "plan_message",
                    "name": "自然语言规划",
                    "description": "将指令拆解为 click / input / open_app 等步骤。",
                    "invoke": "WebSocket / HTTP copilot · plan_message(text, sn, context)",
                    "examples": ["点击一键登录", "点击账号输入框,输入: test@x.com"],
                },
                {
                    "id": "execute_steps",
                    "name": "步骤执行",
                    "description": "按 Plan 驱动设备，含 Overlay Guard、截图、失败即停。",
                    "invoke": "copilot_service.execute_steps(steps, sn, …)",
                    "triggers": ["Copilot 自动执行", "飞书逐步操作"],
                },
                {
                    "id": "locate_arbitrate",
                    "name": "多通道定位",
                    "description": "OCR / Hierarchy / CLIP / 图标库并行 → profile 加权取最高。",
                    "invoke": "locate.resolver.resolve_locate_target（click 步骤内自动）",
                    "examples": ["点击「同意」", "点击账号输入框"],
                },
            ],
        },
        {
            "key": "regression",
            "title": "飞书回归",
            "description": "用例批量执行、预期校验与报告。",
            "items": [
                {
                    "id": "feishu_regression_run",
                    "name": "批量回归",
                    "description": "拉表格用例 → 前置 → 拉起 → 逐步 → 预期。",
                    "invoke": "POST /feishu/run · feishu_regression_service.run_cases",
                },
                {
                    "id": "expectation_verify",
                    "name": "步骤预期校验",
                    "description": "OCR + 前台包名 + 页面语义判断预期。",
                    "invoke": "feishu_regression_service._verify_step_expected（每步后自动）",
                    "examples": ["登录成功", "切换到微信app"],
                },
                {
                    "id": "skills_pre_post",
                    "name": "应用 Skills 前置/后置",
                    "description": "应用配置里额外的 Copilot 指令块。",
                    "invoke": "应用配置 → Skills；飞书 skill_pre / skill_post",
                    "examples": ["前置：打开设置", "后置：返回桌面"],
                },
            ],
        },
        {
            "key": "precondition",
            "title": "前置条件",
            "description": "飞书用例「前置」列自动解析执行，无需手写 nodeCode。",
            "items": [
                {
                    "id": "precondition_clear_cache",
                    "name": "清理应用缓存",
                    "description": "pm clear 清数据。",
                    "invoke": "case_precondition_service · kind=clear_cache",
                    "examples": ["1. 应用无缓存"],
                },
                {
                    "id": "precondition_check_sim",
                    "name": "SIM 卡检测",
                    "invoke": "case_precondition_service · kind=check_sim",
                    "examples": ["1. 手机内安装sim卡"],
                },
                {
                    "id": "precondition_check_wechat",
                    "name": "微信安装检测",
                    "invoke": "case_precondition_service · kind=check_wechat",
                },
                {
                    "id": "precondition_logged_in",
                    "name": "登录态检测",
                    "invoke": "case_precondition_service · kind=check_logged_in",
                    "examples": ["1. 已登录", "1. 未登录"],
                },
            ],
        },
        {
            "key": "guard",
            "title": "页面守卫与恢复",
            "items": [
                {
                    "id": "open_app_foreground",
                    "name": "拉起被测应用",
                    "description": "每条用例开始前 ensure_app_foreground。",
                    "invoke": "app_automation_service.ensure_app_foreground(sn, package)",
                },
                {
                    "id": "overlay_guard",
                    "name": "阻塞弹窗守卫",
                    "description": "隐私同意、系统权限等自动处置。",
                    "invoke": "overlay_guard_service（click 前后 / 校验前自动）",
                },
                {
                    "id": "page_navigation_recovery",
                    "name": "页面导航恢复",
                    "invoke": "page_navigation_service.ensure_page_ready_before_action",
                },
                {
                    "id": "device_prep",
                    "name": "设备唤醒解锁",
                    "invoke": "feishu_regression · _append_device_prep_trace",
                },
                {
                    "id": "regression_capture",
                    "name": "执行截图落盘",
                    "invoke": "regression_capture.capture_device_screenshot",
                },
            ],
        },
        {
            "key": "knowledge",
            "title": "知识库",
            "items": [
                {
                    "id": "failure_knowledge",
                    "name": "失败知识沉淀",
                    "invoke": "POST /settings/knowledge/analyze-failure",
                },
            ],
        },
    ]


def list_skills_catalog() -> Dict[str, Any]:
    server_groups = list_server_groups()
    executor_components = list_executor_components()
    server_count = sum(len(g.get("items") or []) for g in server_groups)
    op_count = sum(len(c.get("operations") or []) for c in executor_components)
    return {
        "server": {
            "intro": "Server 能力没有 nodeCode：通过 HTTP / WebSocket / 回归流程自动调用。",
            "groups": server_groups,
        },
        "executor": {
            "intro": "执行器能力按 nodeCode 聚合；同一组件下不同操作用 data 参数区分。无 nodeCode 项为引擎直连或内部通道。",
            "components": executor_components,
        },
        "summary": {
            "server_count": server_count,
            "executor_count": op_count,
            "executor_component_count": len(executor_components),
        },
        "platforms": [
            {"id": "android", "label": "Android", "notes": "当前移动端主路径，支持 u2 / adb / 多通道定位。"},
            {"id": "ios", "label": "iOS", "notes": "依赖 iOS 执行器适配，部分系统能力不可用。"},
            {"id": "mac", "label": "macOS", "notes": "桌面 App/按键/截图类能力，需桌面执行器权限。"},
            {"id": "windows", "label": "Windows", "notes": "桌面 App/按键/截图类能力，需桌面执行器权限。"},
            {"id": "web", "label": "Web", "notes": "优先 DOM/浏览器自动化；坐标手势只作兜底。"},
        ],
    }


def _tool_name(op_id: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in str(op_id or "tool").lower())
    return "mo_" + "_".join([x for x in cleaned.split("_") if x])


def _param_schema(param: ParamItem) -> Dict[str, Any]:
    desc = param.get("desc") or ""
    schema: Dict[str, Any] = {"type": "string", "description": desc or param.get("name") or ""}
    example = param.get("example")
    if example:
        schema["examples"] = [example]
    if "true" in desc.lower() or "是否" in desc:
        schema["type"] = "boolean"
    if "|" in desc:
        values = [x.strip() for x in desc.replace("固定", "").split("|") if x.strip()]
        if values:
            schema["enum"] = values
    if desc.startswith("固定 "):
        fixed = desc.replace("固定", "", 1).strip()
        if fixed:
            schema["const"] = fixed
    return schema


def _effective_platforms(comp: ComponentItem, op: OperationItem) -> List[str]:
    return list(op.get("platforms") or comp.get("platforms") or [])


def _operation_tool(comp: ComponentItem, op: OperationItem) -> Dict[str, Any]:
    properties: Dict[str, Any] = {
        p["name"]: _param_schema(p)
        for p in (op.get("params") or [])
        if p.get("name")
    }
    properties.setdefault(
        "reason",
        {
            "type": "string",
            "description": "为什么当前计划需要调用这个能力，供策略审计与失败分析使用。",
        },
    )
    properties.setdefault(
        "step_text",
        {
            "type": "string",
            "description": "原始自然语言步骤或策略片段。",
        },
    )
    platforms = _effective_platforms(comp, op)
    if platforms:
        properties.setdefault(
            "platform",
            {
                "type": "string",
                "enum": platforms,
                "description": "目标运行平台。Plan 必须选择受支持的平台，避免把 Android-only 能力下发到 iOS/web/桌面。",
            },
        )
    dispatch_type = comp.get("invoke_type") or "tentacle"
    if comp.get("node_code"):
        dispatch_type = "node_code"
    risk = op.get("risk") or comp.get("risk") or ""
    return {
        "name": _tool_name(op.get("id") or comp.get("name")),
        "description": f"{op.get('name')}: {op.get('description') or comp.get('description') or ''}".strip(),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        },
        "x_mini_orange": {
            "operation_id": op.get("id"),
            "category": comp.get("category"),
            "node_code": comp.get("node_code") or "",
            "dispatch_type": dispatch_type,
            "supported_platforms": platforms,
            "risk": risk,
            "invoke": op.get("invoke") or "",
            "triggers": op.get("triggers") or [],
            "examples": op.get("examples") or [],
        },
    }


def list_anthropic_tool_use_catalog() -> Dict[str, Any]:
    """Anthropic Tool Use API 兼容格式，供 Plan 分析、策略选择和下发层复用。"""
    components = list_executor_components()
    tools: List[Dict[str, Any]] = []
    for comp in components:
        for op in comp.get("operations") or []:
            tools.append(_operation_tool(comp, op))
    return {
        "provider": "anthropic",
        "version": "tool_use_2024_01",
        "usage": {
            "analysis": "Plan 阶段读取 tools 判断可用能力。",
            "strategy": "先按 platform 过滤 supported_platforms，再根据 x_mini_orange.dispatch_type 选择 nodeCode / engine / internal 下发策略。",
            "dispatch": "执行层使用 operation_id、node_code 与输入参数映射到现有 Plan kind 或 Tentacle data。",
        },
        "tools": tools,
    }
