# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Prompt templates for LLM-based Plan generation."""
from __future__ import annotations

from typing import Any

CASE_CHANNELS = frozenset({"case", "case_execution", "regression", "feishu"})


def _is_case_channel(channel: str) -> bool:
    return (channel or "copilot").strip().lower() in CASE_CHANNELS


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

【火山引擎 Doubao 坐标精度补充要求 — 违反将拒绝执行】
1. 附图像素尺寸即 screen.width × screen.height（通常 600×1304 等，以上下文为准）。所有 x、y 必须且只能基于该尺寸。
2. 硬性边界（Server 会校验，超出则整单拒绝、不下发点击）：
   - 1 ≤ x ≤ screen.width
   - 1 ≤ y ≤ screen.height
   禁止返回 x>width 或 y>height；禁止用设备物理分辨率填坐标；禁止乘除 compress_ratio。
3. click/input/swipe 必须给出精确整数坐标（个位），先辨认目标控件可点击区域边界，再取几何中心。
4. 输出前必须同时满足上述边界；坐标与 label/summary 描述的目标位置一致。
5. 只输出一个 JSON 对象，不要 Markdown，不要在 JSON 后追加解释；steps 必须是数组。"""


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
4. 截图发送前 Server 已尝试清除阻塞弹窗；若仍看到隐私协议/权限弹窗，且步骤不是点「同意/允许」，优先在截图中寻找步骤描述的目标；若目标确实不可见，返回 click 点击步骤描述中最匹配的可见控件。
5. 禁止返回空 steps；禁止 auto_run=false；不要只写 blockers 而不给可执行 step。
6. open_app/close_app 使用 known_apps 中的准确 package 字段。
7. input 必须带 text；summary 保留原始步骤语义（如「点击一键登录按钮」）。
8. 失败即停：不要规划设备准备、解锁、清缓存等动作。
9. 不要自行推断业务前置条件（如协议是否已勾选）；按用例步骤原文规划点击/输入坐标。

输出 JSON：{"reply":"...","steps":[...],"auto_run":true}"""


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
- click 输出示例（底部导航）：{{"kind":"click","x":449,"y":2492,"coords_explicit":true,"label":"造物秀","reason":"截图底部导航栏右侧显示该入口"}}。
- click 输出示例（顶部导航）：{{"kind":"click","x":720,"y":210,"coords_explicit":true,"label":"想要成真","reason":"截图顶部导航栏第二个 Tab 文字中心"}}。
- input 输出示例：{{"kind":"input","x":520,"y":1180,"text":"13800138000","coords_explicit":true,"label":"手机号输入框"}}。
- swipe 输出示例：{{"kind":"swipe","start_x":600,"start_y":1900,"end_x":600,"end_y":850,"duration_ms":350}}。
"""


def build_ai_plan_messages(
    *,
    instruction: str,
    platform: str = "android",
    channel: str = "copilot",
    context: str = "",
    provider_id: str = "",
    model: str = "",
) -> list[dict[str, str]]:
    """Return OpenAI/Anthropic-style messages excluding the tools payload."""
    is_case = _is_case_channel(channel)
    system_prompt = AI_CASE_PLAN_SYSTEM_PROMPT if is_case else AI_PLAN_SYSTEM_PROMPT
    if _is_volcengine_doubao_provider(provider_id, model):
        system_prompt = system_prompt + VOLCENGINE_DOUBAO_COORD_PRECISION_APPEND
        if _is_doubao_seed2_or_thinking_model(model):
            system_prompt = system_prompt + VOLCENGINE_DOUBAO_JSON_ONLY_APPEND

    if is_case:
        user_content = (
            "请为下面的飞书/回归用例单步操作生成 MiniOrange 可执行计划。\n\n"
            f"运行渠道：{channel or 'case_execution'}\n"
            f"目标平台：{platform or 'android'}\n"
            f"用例步骤原文：\n{instruction or ''}\n\n"
            f"设备/环境上下文：\n{context or '无'}\n\n"
            "规划要求：\n"
            "- 只规划这一条用例步骤，默认输出 1 个 step。\n"
            "- 必须根据 screen 截图返回 preview 像素坐标，coords_explicit=true。\n"
            "- 图标按钮（微信、手机、一键登录等）也要从截图判断位置。\n"
            "- 必须 auto_run=true，禁止返回空 steps。\n"
            "- 坐标必须基于 screen.width×screen.height（附图像素）；x 不得超过 width，y 不得超过 height，否则 Server 拒绝执行。\n"
            '- click 示例（preview 坐标，须按当前截图重算）：{"kind":"click","x":300,"y":680,"coords_explicit":true,"label":"一键登录","summary":"点击页面中部一键登录按钮中心"}。\n'
            '- input 示例：{"kind":"input","x":180,"y":420,"text":"17633379569","coords_explicit":true,"summary":"输入手机号"}。\n'
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
            "- 坐标必须基于 screen.preview_width × screen.preview_height（发给模型的截图像素），Server 会映射到设备 screen.width × screen.height。\n"
            "- 示例坐标仅说明 preview 坐标系，须按当前截图重算，禁止照抄固定数字。\n"
            '- click 示例（顶部，y≈preview_height×0.09）：{"kind":"click","x":720,"y":72,"coords_explicit":true,"label":"访客浏览","reason":"右上角文字中心，避开状态栏"}。\n'
            '- click 示例（底部，y≈preview_height×0.97）：{"kind":"click","x":120,"y":745,"coords_explicit":true,"label":"首页","reason":"底部导航栏左侧入口中心"}。\n'
            '- input 输出示例：{"kind":"input","x":180,"y":420,"text":"13800138000","coords_explicit":true,"label":"手机号输入框"}。\n'
            '- swipe 输出示例：{"kind":"swipe","start_x":220,"start_y":680,"end_x":220,"end_y":300,"duration_ms":350}。\n'
        )
    if _is_volcengine_doubao_provider(provider_id, model):
        user_content += (
            "\n- 【重要】回复必须且只能是一个 JSON 对象，禁止输出思考过程或坐标推算文字。\n"
            "- 【坐标边界】x≤screen.width、y≤screen.height，超出则 Server 拒绝执行并重试一次。\n"
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
