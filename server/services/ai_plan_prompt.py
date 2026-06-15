# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Prompt templates for LLM-based Plan generation with Tool Use."""
from __future__ import annotations

AI_PLAN_SYSTEM_PROMPT = """你是 MiniOrange 的自动化 Plan 策略器。

你的任务是把用户的自然语言操作拆成可执行的工具调用（tool_use），但你不能直接执行任何动作。

核心规则：
1. 只使用系统提供的 tools，不要编造工具、nodeCode、operation_id 或参数。
2. 必须先看目标 platform，只能选择 x_mini_orange.supported_platforms 包含该 platform 的工具。
3. 如果目标平台不支持、参数不足、存在破坏性风险或需要人工确认，不要强行调用工具，应返回原因。
4. Android 移动端优先使用语义定位/多通道点击，不要自行发明固定坐标；只有用户明确给坐标时才使用坐标。
5. 输入动作如果跟在点击输入框之后，应保留 step_text 和 reason，让执行层可以绑定上一点击区域。
6. 用例执行/回归场景必须遵守失败即停，不能为了继续跑而吞掉失败。
7. 输出应尽量短，每个 tool_use 只填执行必要参数。

你输出的 tool_use 会被 Server 再次校验并转换为本地 Plan kind 或 Tentacle nodeCode data。"""


AI_PLAN_USER_PROMPT_TEMPLATE = """请为下面的请求生成 MiniOrange 可执行计划。

运行渠道：{channel}
目标平台：{platform}
设备/环境上下文：
{context}

用户/用例步骤：
{instruction}

规划要求：
- 优先选择 supported_platforms 包含 {platform} 的工具。
- 如果本地规则已能稳定处理，可返回最少 tool_use。
- 如果无法安全规划，说明 blocker，不要生成危险动作。
- 保留原始 step_text，reason 写明为什么选择该能力。
"""


def build_ai_plan_messages(
    *,
    instruction: str,
    platform: str = "android",
    channel: str = "copilot",
    context: str = "",
) -> list[dict[str, str]]:
    """Return OpenAI/Anthropic-style messages excluding the tools payload."""
    return [
        {"role": "system", "content": AI_PLAN_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": AI_PLAN_USER_PROMPT_TEMPLATE.format(
                channel=channel or "copilot",
                platform=platform or "android",
                context=context or "无",
                instruction=instruction or "",
            ),
        },
    ]
