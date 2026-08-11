# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Prompt templates for LLM-based Plan generation."""
from __future__ import annotations

import json
from typing import Any, Optional

# ClawNode 设备指令 → MiniOrange Plan step kind（仅规划相关）
_CLAWNODE_COMMAND_STEP_KINDS: dict[str, list[str]] = {
    "TAP": ["click"],
    "SWIPE": ["swipe"],
    "INPUT_TEXT": ["input"],
    "OPEN_APP": ["open_app"],
    "CLOSE_APP": ["close_app"],
    "KEY_EVENT": ["system_key", "back"],
    "EXEC_SCRIPT": ["ability"],
}

CASE_CHANNELS = frozenset({"case", "case_execution", "regression", "feishu"})


def _is_case_channel(channel: str) -> bool:
    return (channel or "copilot").strip().lower() in CASE_CHANNELS


def _extract_remote_capabilities_from_context(context: str) -> Optional[dict[str, Any]]:
    if not context or context == "无":
        return None
    try:
        ctx = json.loads(context)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(ctx, dict):
        return None
    manifest = ctx.get("remote_node_capabilities")
    return manifest if isinstance(manifest, dict) else None


def _allowed_step_kinds_from_manifest(manifest: dict[str, Any]) -> list[str]:
    kinds: list[str] = []
    seen: set[str] = set()
    for item in manifest.get("capabilities") or []:
        if not isinstance(item, dict):
            continue
        cmd = str(item.get("command") or "").strip().upper()
        for kind in _CLAWNODE_COMMAND_STEP_KINDS.get(cmd, []):
            if kind not in seen:
                seen.add(kind)
                kinds.append(kind)
    return kinds


def _format_param_specs(params: list[Any]) -> str:
    parts: list[str] = []
    for p in params or []:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        ptype = str(p.get("type") or "string")
        required = "必填" if p.get("required") else "可选"
        desc = str(p.get("description") or "").strip()
        default = p.get("default")
        example = p.get("example")
        extra = []
        if default is not None:
            extra.append(f"默认={default}")
        if example is not None:
            extra.append(f"示例={example}")
        tail = f" ({'，'.join(extra)})" if extra else ""
        parts.append(f"{name}:{ptype} {required}{('，' + desc) if desc else ''}{tail}")
    return "；".join(parts) if parts else "无参数"


def build_remote_node_capabilities_hint(manifest: Optional[dict[str, Any]]) -> str:
    """把 ClawNode CAPABILITIES 清单格式化为 Plan 提示（仅 remote 节点）。"""
    if not manifest or not isinstance(manifest.get("capabilities"), list):
        return ""
    version_name = str(manifest.get("version_name") or "").strip()
    version_code = manifest.get("version_code")
    allowed_kinds = _allowed_step_kinds_from_manifest(manifest)
    if not allowed_kinds:
        return ""
    lines = [
        "【Remote 节点能力范围（来自 ClawNode CAPABILITIES）】",
        f"节点版本：{version_name or 'unknown'}"
        + (f" (version_code={version_code})" if version_code else ""),
        f"本次 Plan 仅可输出以下 step kind：{', '.join(allowed_kinds)}。",
        "禁止规划清单未覆盖的动作（如清单无 RUN_SHELL/INSTALL_APK/KILL_APP 则不可生成对应步骤）。",
        "设备指令与参数说明：",
    ]
    for item in manifest.get("capabilities") or []:
        if not isinstance(item, dict):
            continue
        cmd = str(item.get("command") or "").strip().upper()
        step_kinds = _CLAWNODE_COMMAND_STEP_KINDS.get(cmd)
        if not step_kinds:
            continue
        title = str(item.get("title") or cmd).strip()
        category = str(item.get("category") or "").strip()
        desc = str(item.get("description") or "").strip()
        params_text = _format_param_specs(item.get("params") or [])
        example = item.get("example")
        example_text = ""
        if isinstance(example, dict) and example:
            example_text = f"；示例 params={json.dumps(example, ensure_ascii=False)}"
        flags = []
        if item.get("requires_accessibility"):
            flags.append("需无障碍")
        if item.get("requires_shizuku"):
            flags.append("需Shizuku")
        flag_text = f"（{'，'.join(flags)}）" if flags else ""
        lines.append(
            f"- {cmd} / {title}"
            + (f" [{category}]" if category else "")
            + f" → step kind: {', '.join(step_kinds)}"
            + flag_text
            + (f"；{desc}" if desc else "")
            + f"；参数：{params_text}"
            + example_text
        )
    return "\n".join(lines)


def _ai_plan_request_timeout(channel: str) -> int:
    """Case planning runs overlay guard before the LLM call; allow more time."""
    return 90 if _is_case_channel(channel) else 45


def _is_volcengine_doubao_provider(provider_id: str = "", model: str = "") -> bool:
    """True when the active provider is Volcengine Doubao (not Claude/other models)."""
    pid = (provider_id or "").strip().lower()
    mid = (model or "").strip().lower()
    if pid == "volcengine":
        return True
    return "doubao" in mid


def _is_doubao_seed2_or_thinking_model(model: str = "") -> bool:
    """Seed 2.0 / thinking 系列易在 content 里输出推理链，需额外约束或关闭 thinking。"""
    mid = (model or "").strip().lower()
    if not mid:
        return False
    if "seed-2" in mid or "seed_2" in mid or "seed2" in mid:
        return True
    return "thinking" in mid


VOLCENGINE_DOUBAO_COORD_PRECISION_APPEND = """

【火山引擎 Doubao 坐标 — 必须使用 0~1000 归一化，违反将拒绝执行】
1. click/input/swipe 的坐标必须是 0~1000 的整数，表示目标中心在附图上的相对位置（与附图宽高成比例，与设备分辨率、压缩比例无关）。
2. 坐标系：左上角 (0,0)，右下角 (1000,1000)。x 越大越靠右，y 越大越靠下。
3. 计算公式（在脑中完成，不要输出推算过程）：x=round(目标中心距左边缘÷图宽×1000)，y=round(目标中心距上边缘÷图高×1000)。
4. 硬性边界：0≤x≤1000、0≤y≤1000。禁止输出超过 1000 的绝对像素值；禁止假设 1080/1200/2608 等固定分辨率。
5. 先辨认目标控件可点击区域，再取几何中心对应的归一化坐标；坐标须与 label/summary 描述一致。
6. 只输出一个 JSON 对象，不要 Markdown；steps 必须是数组。"""


VOLCENGINE_DOUBAO_JSON_ONLY_APPEND = """

【火山引擎 Doubao — 仅 JSON，禁止思考链外泄】
1. 你的整条回复有且只能是一个合法 JSON 对象（从 { 开始、到 } 结束），禁止 Markdown 代码块。
2. 禁止在 JSON 之前、之中或之后输出思考过程、坐标推算、自我纠错、中文解释（如「不对哦」「重新算」）。
3. 禁止先输出半截 JSON 再输出第二段 JSON；坐标推算在内部完成，只写最终 steps。
4. 若需先处理弹窗再继续业务步骤，仍只输出一个 JSON，steps 里写当前应点的那个 click。"""


def volcengine_chat_payload_extras(*, provider_id: str = "", model: str = "") -> dict[str, Any]:
    """方舟 Chat Completions 附加参数（Plan 场景关闭 thinking，避免污染 JSON）。"""
    if not _is_volcengine_doubao_provider(provider_id, model):
        return {}
    return {"thinking": {"type": "disabled"}}


AI_PLAN_SYSTEM_PROMPT = """你是 MiniOrange 的自动化 Plan 策略器。

你的任务是把用户的自然语言操作拆成 MiniOrange 标准 JSON steps，但你不能直接执行任何动作。

核心规则：
1. 只输出 MiniOrange 支持的 step kind：click、input、swipe、open_app、close_app、back、system_key、ability。
2. 必须先看目标 platform，只能生成该 platform 可执行的步骤。
3. 如果目标平台不支持、参数不足、或存在破坏性风险（如卸载应用、清空数据），不要强行调用工具，应返回原因。
4. 大模型模式必须基于当前截图直接输出坐标。click/input/swipe 这类视觉动作不能只返回 label、direction、field_hint 让本地执行器再判断。
5. click 必须包含 x、y、coords_explicit=true；坐标必须基于 screen.preview_width × screen.preview_height（即你看到的截图像素尺寸），不要自行换算到设备分辨率；点击点应对准目标控件可点击区域中心，不要落在系统状态栏、刘海或屏幕外缘。
6. input 必须包含 x、y、text、coords_explicit=true；x/y 同样基于 preview 图像素坐标。
7. swipe 必须包含 start_x、start_y、end_x、end_y；同样基于 preview 图像素坐标；direction 只能作为审计说明。
8. open_app / close_app 必须填写 package（Android 包名）。优先使用上下文 known_apps 里的准确包名，不要猜测；不要因包名不确定而设 auto_run=false。
9. 如果没有截图、截图不清晰或无法判断坐标，返回 blocker，不要生成可执行视觉动作。
10. 用例执行/回归场景必须遵守失败即停，不能为了继续跑而吞掉失败。
11. 输出应尽量短，每个 step 只填执行必要参数。
12. 不要输出 tool_code、函数调用对象或伪代码；只输出标准 JSON。
13. Home 键、菜单键、电源键这类系统按键使用 system_key，并填写 key，例如 {"kind":"system_key","key":"home","summary":"按 Home 键"}。
14. 不要规划锁屏解锁、输入锁屏密码、唤醒屏幕、清理后台/清理应用数据等设备准备动作；这些属于执行器前置准备或专门能力，不能由大模型静默触发。
15. 不要自行推断业务前置条件（如协议是否已勾选、是否需先同意条款才能点击按钮）。用户明确要求 click/input 时，应基于截图返回目标控件坐标；这类业务判断由执行器处理，不要写成 blocker 或 auto_run=false。

你输出的 steps 会被 Server 再次校验并下发执行。"""


AI_CASE_PLAN_SYSTEM_PROMPT = """你是 MiniOrange 飞书/回归用例的单步 Plan 策略器。

输入来自用例表的一条操作步骤（不是 Copilot 自由对话）。你必须根据附带截图输出可直接执行的 JSON plan。

核心规则：
1. 默认只输出 1 个 step，对应该条用例步骤；只有步骤文本明确包含多个动作（如「点击 A，再点击 B」）时才输出多个 step。
2. click/input/swipe 必须基于截图返回 preview 像素坐标（screen.preview_width × screen.preview_height），并设 coords_explicit=true。
3. 步骤常是「点击 xxx 按钮/图标」—— xxx 可能是文字按钮、底部图标、圆形图标；必须从截图判断位置，不能只返回 label。
4. 截图发送前 Server 已尝试清除阻塞弹窗；若仍看到隐私协议/权限弹窗，且用例步骤不是点「同意/允许」，你应只规划关闭阻碍的一个 click，并设 plan_complete=false（见下条）。
5. 必须返回 plan_complete 布尔字段：
   - plan_complete=true：本次 steps 直接完成用例步骤原文目标（例如步骤是「点击一键登录」，你规划的就是点一键登录按钮；步骤是「点击同意并继续」且你点的就是同意并继续/同意，也必须 plan_complete=true，即使该按钮在弹窗里）。
   - plan_complete=false：仅当用例步骤原文是别的目标（如一键登录），而你本次只处理了弹窗/权限等阻碍、尚未点到步骤要求的目标时。
6. 禁止返回空 steps；禁止 auto_run=false；不要只写 blockers 而不给可执行 step。
7. open_app/close_app 使用 known_apps 中的准确 package 字段。
8. input 必须带 text；summary 应保留用例步骤语义（完成目标时）或写明当前前置动作（plan_complete=false 时）。
9. 失败即停：不要规划设备准备、解锁、清缓存等动作。
10. 不要自行推断业务前置条件（如协议是否已勾选）；按用例步骤原文规划点击/输入坐标。

输出 JSON：{"reply":"...","steps":[...],"auto_run":true,"plan_complete":true|false}"""


AI_PLAN_USER_PROMPT_TEMPLATE = """请为下面的请求生成 MiniOrange 可执行计划。

运行渠道：{channel}
目标平台：{platform}
设备/环境上下文：
{context}

用户/用例步骤：
{instruction}

规划要求：
- 优先选择 supported_platforms 包含 {platform} 的工具。
- 如果无法安全规划，说明 blocker，不要生成危险动作。
- 保留原始 step_text，reason 写明为什么选择该能力。
- 当前屏幕截图在上下文的 screen 字段中。所有视觉动作必须直接返回坐标，不能把判断交给本地 OCR/CLIP/多通道定位。
- 坐标必须基于 screen.preview_width×screen.preview_height；x∈[1,preview_width]、y∈[1,preview_height]。
- 禁止照抄固定坐标数字或按设备物理分辨率填坐标；须按当前附图重算目标控件中心。
"""


def _preview_dimensions(screen: Optional[dict[str, Any]]) -> tuple[int, int]:
    if not screen:
        return 0, 0
    pw = int(screen.get("preview_width") or screen.get("width") or 0)
    ph = int(screen.get("preview_height") or screen.get("height") or 0)
    return pw, ph


def screen_coord_space_hint(screen: Optional[dict[str, Any]]) -> str:
    """Explicit coordinate space for the current screenshot."""
    pw, ph = _preview_dimensions(screen)
    if pw <= 0 or ph <= 0:
        return ""
    return (
        f"【本次坐标系】preview_width={pw}、preview_height={ph}；"
        f"x∈[1,{pw}]、y∈[1,{ph}]，超出则 Server 拒绝执行。"
    )


def screen_coord_space_hint_normalized(screen: Optional[dict[str, Any]]) -> str:
    """Doubao: 0~1000 normalized coordinate space."""
    pw, ph = _preview_dimensions(screen)
    if pw <= 0 or ph <= 0:
        return ""
    return (
        f"【本次坐标系·归一化】附图 {pw}×{ph} 像素；"
        "x、y 必须输出 0~1000 整数（左上角 0,0，右下角 1000,1000）。"
        "禁止输出超过 1000 的像素坐标。"
    )


def screen_coord_examples_hint(screen: Optional[dict[str, Any]]) -> str:
    """Dimension-matched click examples so static device-resolution coords do not confuse the model."""
    pw, ph = _preview_dimensions(screen)
    if pw <= 0 or ph <= 0:
        return ""
    top_y = max(72, int(ph * 0.08))
    bottom_y = max(top_y + 24, int(ph * 0.97))
    mid_y = int(ph * 0.52)
    dialog_y = max(top_y + 24, int(ph * 0.92))
    right_x = max(1, int(pw * 0.88))
    left_x = max(1, int(pw * 0.12))
    mid_x = max(1, int(pw * 0.50))
    dialog_agree_x = max(1, int(pw * 0.75))
    input_y = max(top_y + 24, int(ph * 0.35))
    swipe_start_y = max(top_y + 24, int(ph * 0.78))
    swipe_end_y = max(top_y + 24, int(ph * 0.30))
    return (
        f"【本次坐标示例（preview {pw}×{ph}，禁止照抄其他分辨率的数字）】\n"
        f'- 右上角文字：{{"kind":"click","x":{right_x},"y":{top_y},"coords_explicit":true,"label":"访客浏览"}}\n'
        f'- 底部导航左侧：{{"kind":"click","x":{left_x},"y":{bottom_y},"coords_explicit":true,"label":"首页"}}\n'
        f'- 页面中部按钮：{{"kind":"click","x":{mid_x},"y":{mid_y},"coords_explicit":true,"label":"一键登录"}}\n'
        f'- 弹窗右下按钮：{{"kind":"click","x":{dialog_agree_x},"y":{dialog_y},"coords_explicit":true,"label":"同意"}}\n'
        f'- 输入框：{{"kind":"input","x":{mid_x},"y":{input_y},"text":"13800138000","coords_explicit":true,"label":"手机号输入框"}}\n'
        f'- 上滑：{{"kind":"swipe","start_x":{mid_x},"start_y":{swipe_start_y},"end_x":{mid_x},"end_y":{swipe_end_y},"duration_ms":350}}\n'
    )


def screen_coord_examples_hint_normalized(screen: Optional[dict[str, Any]]) -> str:
    """Doubao examples in 0~1000 space (layout ratios, not pixel coords)."""
    if _preview_dimensions(screen) == (0, 0):
        return ""
    return (
        "【本次坐标示例·归一化 0~1000（须按当前附图重算，禁止照抄）】\n"
        '- 右上角文字：{"kind":"click","x":880,"y":80,"coords_explicit":true,"label":"访客浏览"}\n'
        '- 底部导航左侧：{"kind":"click","x":120,"y":970,"coords_explicit":true,"label":"首页"}\n'
        '- 页面中部按钮：{"kind":"click","x":500,"y":520,"coords_explicit":true,"label":"一键登录"}\n'
        '- 弹窗右下「同意」：{"kind":"click","x":750,"y":920,"coords_explicit":true,"label":"同意"}\n'
        '- 输入框：{"kind":"input","x":500,"y":350,"text":"13800138000","coords_explicit":true,"label":"手机号"}\n'
        '- 上滑：{"kind":"swipe","start_x":500,"start_y":780,"end_x":500,"end_y":300,"duration_ms":350}\n'
    )


def build_ai_plan_prompt_preview(
    *,
    provider_id: str = "",
    model: str = "",
    screen: Optional[dict[str, Any]] = None,
    drift_replan: Optional[dict[str, Any]] = None,
    goal_continue_replan: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Snapshot of coord guidance embedded in the LLM user prompt (for ai_debug)."""
    pw, ph = _preview_dimensions(screen)
    if _is_volcengine_doubao_provider(provider_id, model):
        preview = {
            "coord_mode": "normalized_1000",
            "preview_width": pw,
            "preview_height": ph,
            "coord_space_hint": screen_coord_space_hint_normalized(screen),
            "coord_examples_hint": screen_coord_examples_hint_normalized(screen),
        }
    else:
        preview = {
            "coord_mode": "preview_pixels",
            "preview_width": pw,
            "preview_height": ph,
            "coord_space_hint": screen_coord_space_hint(screen),
            "coord_examples_hint": screen_coord_examples_hint(screen),
        }
    replan_note = build_foreground_drift_replan_note(drift_replan)
    if replan_note:
        preview["drift_replan_note"] = replan_note
    continue_note = build_goal_continue_replan_note(goal_continue_replan)
    if continue_note:
        preview["goal_continue_replan_note"] = continue_note
    return preview


def _append_preview_coord_guidance(
    user_content: str,
    screen: Optional[dict[str, Any]],
    *,
    provider_id: str = "",
    model: str = "",
) -> str:
    if _is_volcengine_doubao_provider(provider_id, model):
        space_hint = screen_coord_space_hint_normalized(screen)
        examples_hint = screen_coord_examples_hint_normalized(screen)
    else:
        space_hint = screen_coord_space_hint(screen)
        examples_hint = screen_coord_examples_hint(screen)
    if space_hint or examples_hint:
        parts = [user_content.rstrip(), space_hint, examples_hint]
        return "\n".join(part for part in parts if part)
    if _is_volcengine_doubao_provider(provider_id, model):
        return user_content + "\n- 坐标须为 0~1000 归一化整数；禁止输出绝对像素坐标。\n"
    return (
        user_content
        + "\n- 坐标须基于 context.screen.preview_width×preview_height；"
        "禁止假设设备分辨率或照抄固定像素值。\n"
    )


def build_goal_continue_replan_note(goal_continue: Optional[dict[str, Any]]) -> str:
    """前置弹窗/阻碍处理完后，要求模型基于新截图继续完成原用例步骤。"""
    if not isinstance(goal_continue, dict) or not goal_continue:
        return ""
    command = str(goal_continue.get("command") or "").strip()
    prev_reply = str(goal_continue.get("previous_reply") or "").strip()
    executed_summary = str(goal_continue.get("executed_summary") or "").strip()
    executed_label = str(goal_continue.get("executed_label") or "").strip()
    attempt = int(goal_continue.get("attempt") or 1)
    parts = [
        f"【继续完成用例步骤·第 {attempt} 次】上一轮仅执行了前置动作，尚未完成用例步骤原文。",
        f"用例步骤原文（必须在本轮达成）：{command or '（见上文）'}",
        "附图是执行前置动作后的最新屏幕；请基于新截图规划，禁止沿用旧坐标。",
        "若用例步骤目标已可见，直接规划点击/输入该目标，并设 plan_complete=true。",
        "若仍有新的弹窗/权限阻碍，可再规划一个前置 click，并设 plan_complete=false。",
    ]
    if executed_summary or executed_label:
        parts.append(f"上一轮已执行：{executed_summary or executed_label}。")
    if prev_reply:
        parts.append(f"上一轮规划说明：{prev_reply[:240]}")
    parts.append("请只返回一个可执行 JSON，含 plan_complete 字段。")
    return "\n".join(part for part in parts if part)


def build_foreground_drift_replan_note(drift_replan: Optional[dict[str, Any]]) -> str:
    """离屏阻断后，要求模型基于新截图重新规划。"""
    if not isinstance(drift_replan, dict) or not drift_replan:
        return ""
    drift_note = str(drift_replan.get("drift_note") or "").strip()
    expected = str(drift_replan.get("expected_package") or "").strip()
    actual_name = str(drift_replan.get("actual_app_name") or "").strip()
    actual_pkg = str(drift_replan.get("actual_package") or "").strip()
    blocked_summary = str(drift_replan.get("blocked_step_summary") or "").strip()
    previous_reply = str(drift_replan.get("previous_reply") or "").strip()
    attempt = int(drift_replan.get("attempt") or 1)
    parts = [
        f"【离屏重规划·第 {attempt} 次】上次计划在执行前被阻断：{drift_note or '被测应用已不在前台'}。",
        (
            f"被测应用包名应为 {expected or '（见上下文 package）'}，"
            f"但执行时前台为 {actual_name or actual_pkg or '其他应用'}（{actual_pkg or '-'}）。"
        ),
        "附图是当前最新屏幕，与上次规划时不同；请基于新截图重新规划，禁止沿用旧坐标。",
        (
            "若当前是系统弹窗/权限页/安全中心页，优先规划关闭、拒绝或返回，使被测应用回到前台；"
            "若已回到被测应用，再完成原用例步骤目标。"
        ),
    ]
    if blocked_summary:
        parts.append(f"被阻断的步骤：{blocked_summary}。")
    if previous_reply:
        parts.append(f"上次规划结论：{previous_reply[:240]}")
    parts.append("请只返回一个可执行 JSON，坐标必须按本次附图重算。")
    return "\n".join(part for part in parts if part)


def build_ai_plan_messages(
    *,
    instruction: str,
    platform: str = "android",
    channel: str = "copilot",
    context: str = "",
    provider_id: str = "",
    model: str = "",
    screen: Optional[dict[str, Any]] = None,
    drift_replan: Optional[dict[str, Any]] = None,
    goal_continue_replan: Optional[dict[str, Any]] = None,
) -> list[dict[str, str]]:
    """Return OpenAI/Anthropic-style messages excluding the tools payload."""
    is_case = _is_case_channel(channel)
    remote_manifest = _extract_remote_capabilities_from_context(context)
    remote_cap_hint = build_remote_node_capabilities_hint(remote_manifest)
    allowed_remote_kinds = _allowed_step_kinds_from_manifest(remote_manifest) if remote_manifest else []
    system_prompt = AI_CASE_PLAN_SYSTEM_PROMPT if is_case else AI_PLAN_SYSTEM_PROMPT
    if allowed_remote_kinds:
        kinds_text = "、".join(allowed_remote_kinds)
        system_prompt += (
            f"\n\n【Remote 节点约束】当前设备为 ClawNode 直连节点，"
            f"仅可规划以下 step kind：{kinds_text}。"
            "不得输出清单外的 kind（例如清单无 ability 则禁止 ability 步骤）。"
        )
    if _is_volcengine_doubao_provider(provider_id, model):
        system_prompt = system_prompt + VOLCENGINE_DOUBAO_COORD_PRECISION_APPEND
        if _is_doubao_seed2_or_thinking_model(model):
            system_prompt = system_prompt + VOLCENGINE_DOUBAO_JSON_ONLY_APPEND

    is_doubao = _is_volcengine_doubao_provider(provider_id, model)
    if is_case:
        user_content = (
            "请为下面的飞书/回归用例单步操作生成 MiniOrange 可执行计划。\n\n"
            f"运行渠道：{channel or 'case_execution'}\n"
            f"目标平台：{platform or 'android'}\n"
            f"用例步骤原文：\n{instruction or ''}\n\n"
            f"设备/环境上下文：\n{context or '无'}\n\n"
            "规划要求：\n"
            "- 只规划这一条用例步骤，默认输出 1 个 step。\n"
            "- 必须根据 screen 截图返回坐标，coords_explicit=true。\n"
            "- 图标按钮（微信、手机、一键登录等）也要从截图判断位置。\n"
            "- 必须 auto_run=true，禁止返回空 steps。\n"
            "- 必须返回 plan_complete：true=完成用例步骤目标；false=仅处理弹窗/权限等前置阻碍。\n"
            "- screen.foreground_package / foreground_note 仅为设备前台包名观察数据，由你结合截图自行判断是否离屏，Server 不会在 AI 模式下据此阻断执行。\n"
            + (
                "- 坐标必须为 0~1000 归一化整数；示例见下方【本次坐标示例·归一化】。\n"
                if is_doubao
                else (
                    "- 坐标必须基于 screen.preview_width×preview_height；x 不得超过 preview_width，y 不得超过 preview_height，否则 Server 拒绝执行。\n"
                    "- 坐标示例见下方【本次坐标示例】；须按当前截图重算，禁止照抄其他分辨率的数字。\n"
                )
            )
        )
    else:
        user_content = (
            "请为下面的请求生成 MiniOrange 可执行计划。\n\n"
            f"运行渠道：{channel or 'copilot'}\n"
            f"目标平台：{platform or 'android'}\n"
            f"设备/环境上下文：\n{context or '无'}\n\n"
            f"用户/用例步骤：\n{instruction or ''}\n\n"
            "规划要求：\n"
            f"- 优先选择 supported_platforms 包含 {platform or 'android'} 的工具。\n"
            "- 如果无法安全规划，说明 blocker，不要生成危险动作。\n"
            "- 保留原始 step_text，reason 写明为什么选择该能力。\n"
            "- 当前屏幕截图在上下文的 screen 字段中。所有视觉动作必须直接返回坐标，不能把判断交给本地 OCR/CLIP/多通道定位。\n"
            + (
                "- 坐标必须为 0~1000 归一化整数；示例见下方【本次坐标示例·归一化】。\n"
                if is_doubao
                else (
                    "- 坐标必须基于 screen.preview_width × screen.preview_height（发给模型的截图像素），Server 会映射到设备物理分辨率。\n"
                    "- 坐标示例见下方【本次坐标示例】；须按当前截图重算，禁止照抄其他分辨率的数字。\n"
                )
            )
        )
    user_content = _append_preview_coord_guidance(
        user_content, screen, provider_id=provider_id, model=model
    )
    if remote_cap_hint:
        user_content = f"{user_content}\n\n{remote_cap_hint}"
    replan_note = build_foreground_drift_replan_note(drift_replan)
    if replan_note:
        user_content = f"{user_content}\n\n{replan_note}"
    continue_note = build_goal_continue_replan_note(goal_continue_replan)
    if continue_note:
        user_content = f"{user_content}\n\n{continue_note}"
    if is_doubao:
        user_content += (
            "\n- 【重要】回复必须且只能是一个 JSON 对象，禁止输出思考过程或坐标推算文字。\n"
            "- 【坐标边界】0≤x≤1000、0≤y≤1000；禁止输出绝对像素坐标。\n"
        )
        if _is_doubao_seed2_or_thinking_model(model):
            user_content += (
                "- 【Seed 2.0】禁止在 JSON 前后写任何中文；禁止输出多个 JSON；"
                '直接返回 {"reply":"...","steps":[...],"auto_run":true}。\n'
            )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


AI_CASE_ASSERT_SYSTEM_PROMPT = """你是 MiniOrange 飞书/回归用例的预期校验器。

你会收到一条预期描述和当前屏幕截图，需要判断预期是否成立。

输出 JSON（不要 Markdown）：
{"passed": true/false, "reply": "一句话结论", "reason": "基于截图的判断依据", "evidence": "截图中看到的关键界面元素"}

规则：
1. passed=true 表示预期已达成；false 表示未达成。
2. 预期可能是语义描述（如「切换到手机号登录页面」「登录成功进入首页」），需结合截图理解，不是简单 OCR 字面匹配。
3. 若截图是弹窗/权限页而预期是业务页，通常 passed=false，并在 reason 说明。
4. 必须给出 reason；禁止返回空 JSON。"""


def build_ai_assert_messages(
    *,
    expected_text: str,
    platform: str = "android",
    channel: str = "case_execution",
    context: str = "",
) -> list[dict[str, str]]:
    exp = (expected_text or "").strip()
    user_content = (
        "请根据截图判断下面这条用例预期是否成立。\n\n"
        f"运行渠道：{channel or 'case_execution'}\n"
        f"目标平台：{platform or 'android'}\n"
        f"预期描述：\n{exp}\n\n"
        f"设备/环境上下文：\n{context or '无'}\n\n"
        "请只返回 JSON："
        '{"passed":true,"reply":"...","reason":"...","evidence":"..."}。\n'
    )
    return [
        {"role": "system", "content": AI_CASE_ASSERT_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
