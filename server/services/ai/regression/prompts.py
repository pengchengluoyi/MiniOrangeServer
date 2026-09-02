# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""AI-led 回归测试的所有 prompt 在这里集中。

分类（每类一段 SYSTEM + 一个 build_*_messages）：
    1. PLAN_OVERVIEW_TEXT   — 纯文本规划（不带截图），输入用例 + RunContext + Menu
    2. SINGLE_STEP_REPLAN   — 单步重规划（执行失败/偏离 baseline 时）
    3. DIFF_SUMMARIZER      — 占位：Step 8 才正式用，先留 skeleton
    4. PERSONA_TASK         — 占位：Step 7 才正式用

设计原则：
  - 所有 prompt 都强制 JSON-only，禁止 Markdown / 思考链外泄
  - 强制 ai_reasoning 字段，AI 必须解释为什么
  - decline 通道明确写入 system，AI 不会乱编
  - 不在 prompt 里硬塞执行细节（坐标/像素等），那些是带截图的 VLM 阶段做的事
"""
from __future__ import annotations

import json
from typing import Any, Optional

from server.services.ai.regression.schemas import (
    BaselineContext,
    BaselineSnippet,
    CaseSpec,
    PlanEvent,
)

# ============== Plan Overview (Stage 1: text only) ==============

PLAN_OVERVIEW_SYSTEM_PROMPT = """你是 MiniOrange 的 AI 回归测试规划器（PLAN_OVERVIEW_TEXT）。

【输入】
- case_spec：用例文本（name / preconditions / steps / expected）。
- run_context：当前设备 + 通道连通性（adb / remote / vlm / hitl）。
- capability_menu：当前 Run 里能用的 capability（id、一句 summary、以及可选 executor/cost）。不要臆造菜单外的能力。
- baseline（可选）：本 case 之前若执行过，会附上一次的事件序列摘要供你参考。

【你的任务】
基于以上**纯文本**信息，规划出完整的事件执行序列（events）。不要画坐标、不要看截图（这一步根本没有截图）；
具体的"该点哪个像素"是后面带截图的 VLM 子流程的事。

【硬性规则】
1. 只能从 capability_menu 里挑 capability_id；不在菜单里的能力一律禁止。
2. 每个 event 必须从对应 capability 的 implementations[].executor 里挑 expected_executor；菜单里没有的 executor 禁止填。
3. 强制返回 ai_reasoning（整 case 一段 + 每个事件一段），说清你为什么这么排。
4. baseline 是"经验"，不是"脚本"：如果你判断 baseline 这一步已经不适合当前 case_spec / connectivity，可以直接换 capability。
5. 不要规划"清缓存 / 解锁 / 装包"这类设备准备动作，除非用例 preconditions 明确要求。
6. 点哪一像素是后面带截图的执行阶段的事；规划阶段只要决定用哪条 capability、走哪个 executor。
7. router_advice 给出了通道偏好（如"系统级优先 adb"），请遵守。
8. 如果用例描述与 capability_menu 严重不匹配（如需要 web 但只有 adb+remote），返回 mode=decline + 详细 decline_reason，
   并把无法对应的具体步骤写进 open_questions。
9. needs_human=true 的事件（human_*）：只在用例确实需要人工提供信息（验证码、人工确认）时规划，不要为了"安全"乱用。
10. 同一条用例步骤可以拆成多个事件（如"打开 app 并登录" → launch_app + tap_element + input_text + ...），
    但请用 case_step_index 标明这些事件挂到哪条用例步骤。
11. 前置要核对客户端版本或「当前已打开 App」时，用 get_app_version / get_foreground_app，不要用 assert_visual 看图猜。

【输出 JSON Schema（严格，唯一一个 JSON 对象，禁止 Markdown）】
{
  "mode": "plan" | "decline",
  "case_id": "...",
  "ai_reasoning": "<整 case 的总体策略>",
  "confidence": 0.0~1.0,
  "events": [
    {
      "seq": 1,
      "case_step_index": 1,
      "capability_id": "launch_app",
      "event_kind": "launch_app",
      "params": { "package": "com.example.app" },
      "needs_vlm": false,
      "expected_executor": "adb",
      "fallback_executors": ["remote"],
      "ai_reasoning": "用例第1步要打开 app；adb=true 且 router_advice 偏好 adb",
      "label": "打开 com.example.app"
    }
  ],
  "decline_reason": "",
  "open_questions": []
}

mode=decline 时 events 必须为 []，并把 decline_reason 写明白；mode=plan 时 decline_reason 留空字串。
"""


PLAN_OVERVIEW_USER_TEMPLATE = """请为下面这条飞书/回归用例输出 PLAN_OVERVIEW_TEXT。

==== case_spec（用例文本，唯一输入数据） ====
{case_block}

==== run_context（当前设备 / 通道） ====
{run_brief_json}

==== capability_menu（你能用的所有 capability） ====
{menu_json}

{baseline_block}
请只返回一个 JSON 对象，遵守 system 中的 schema。"""


# ============== Single Step Replan (mid-execution) ==============

SINGLE_STEP_REPLAN_SYSTEM_PROMPT = """你是 MiniOrange 的 AI 回归测试单步重规划器（SINGLE_STEP_REPLAN）。

【触发场景】
之前已经按 PLAN_OVERVIEW_TEXT 出过一份事件序列，在执行某一步时出现了"无法继续/偏离"信号：
  - 上一事件执行失败（如 tap_element 找不到目标）
  - 当前事件的前置条件被破坏（如打开 app 后被系统弹窗劫持）
  - 上一事件结果与 baseline 历史显著不同（疑似回归点）
此时上层会停下后续事件，把上下文丢给你重新规划接下来要执行的事件。

【输入】
- run_context、capability_menu：和 PLAN_OVERVIEW 一致
- completed_events：到目前为止已经成功执行的事件 + 各自状态总结
- failed_event：触发本次重规划的事件（含失败/异常 summary）
- baseline（可选）：本 case 之前执行时这附近的步骤怎么走的
- remaining_events：原计划尚未执行的剩余事件序列（供你参考）

【你的决策范围】
- mode=replan：给出新的 events 接管剩余执行；drop_remaining=true 表示把 remaining_events 全废，按你的新序列走；
  false 表示你的 events 只是插入，后面回到 remaining_events。
- mode=decline：你能看懂但不敢自动改（如必须人工确认），把 needs_human=true 同时给详细 decline_reason。
- mode=give_up：彻底放弃本 case 继续执行（如设备已离线、capability_menu 完全不够用）。

【硬性规则】
1. 同 PLAN_OVERVIEW：只能用菜单里的 capability + executor；强制 ai_reasoning（整体一段 + 每 event 一段）。
2. 不要为了"看起来能继续"硬编步骤；如果你不确定，宁可 decline + needs_human=true。
3. 当 failed_event 是 needs_vlm=true 的视觉事件失败时，常见三种应对（择一）：
   a) 重试同 capability 一次（events=[原事件]，drop_remaining=false）
   b) 插入 wait_screen_ready / 拍照 assert_visual 看清当前是什么页面
   c) 插入 press_key 或 close_app 回到已知状态后再继续
4. 【回已知状态的按键纪律】
   - 需要"回到桌面"时用 press_key=home（keyevent home），一步到位；**禁止靠连按 press_key=back 退出应用**：
     很多应用首页有"再按一次退出"的双击返回拦截，而 back 之间往往夹着 wait_screen_ready（截图+VLM 数秒），
     两次 back 早已超出双击窗口，只会反复弹提示、永远退不出去，最终撞重规划上限卡死。
   - 更重要的是先想清楚"到底要不要回桌面"：若 failed_event 的目标是清缓存/强停/应用设置这类
     **在系统设置内完成**的操作，正确做法是（重新）用 EXEC_SCRIPT / launch_app 直达目标应用的
     「应用详情页」或设置页，在该页按系统 UI 逻辑继续；回桌面既无必要也完不成目标，不要规划回桌面。

【输出 JSON Schema（严格）】
{
  "mode": "replan" | "decline" | "give_up",
  "ai_reasoning": "...",
  "events": [ PlanEvent, ... ],
  "drop_remaining": true,
  "decline_reason": "",
  "needs_human": false
}
mode=replan 时 events 不能为空。
"""


SINGLE_STEP_REPLAN_USER_TEMPLATE = """请基于以下上下文做 SINGLE_STEP_REPLAN。

==== run_context ====
{run_brief_json}

==== capability_menu ====
{menu_json}

==== completed_events（已成功执行）====
{completed_json}

==== failed_event（触发重规划）====
{failed_json}

failure_summary: {failure_summary}

==== remaining_events（原计划剩余）====
{remaining_json}

{baseline_block}
请只返回一个 JSON 对象，遵守 system 中的 schema。"""


# ============== LOCATE_VISION (执行 needs_vlm 事件时定位元素) ==============

LOCATE_VISION_SYSTEM_PROMPT = """你是 MiniOrange 的视觉元素定位器（LOCATE_VISION）。

【输入】
- 一张当前屏幕截图（在 user message 的图像里）
- 截图的像素尺寸（preview_width × preview_height）
- 一段对目标元素的文本描述（如『一键登录按钮』『右上角的关闭 X 图标』『手机号输入框』）
- 可选 ai_hint：上层 PlanEvent 的 reasoning，作为补充线索

【任务】
判断截图上是否存在描述的目标；若存在，给出"可点击区域"的中心坐标。

【规则】
1. 坐标系：左上角 (0,0)，右下角 (preview_width, preview_height)；x、y 必须是整数且 0 ≤ x ≤ preview_width、0 ≤ y ≤ preview_height。
2. 找不到目标时 found=false、x=y=0；不要硬猜。
3. confidence 反映"我对自己定位的把握"，0~1；图里没有相似元素时给 0；明确无歧义给 0.95+。
4. 强制返回 ai_reasoning（你为什么觉得这是目标 + 为什么取这个中心点）；不要泄漏推理过程到 ai_reasoning 之外。
5. 不要返回 markdown、不要返回多个 JSON、不要写中文叙述。

【输出 JSON Schema（严格）】
{
  "found": true | false,
  "x": <int 0..preview_width>,
  "y": <int 0..preview_height>,
  "bbox": [x1, y1, x2, y2]    // 可选；若你能给出更稳定的 box，建议给
  "confidence": 0.0..1.0,
  "label_seen": "<你在截图上看到的目标文案/特征，便于 trace>",
  "ai_reasoning": "<你定位的依据>"
}"""


LOCATE_VISION_USER_TEMPLATE = """请在附图上定位下面这个目标。

==== preview_size ====
preview_width  : {preview_width}
preview_height : {preview_height}

==== 目标描述 ====
{description}

{hint_block}
请只返回一个 JSON。"""


# ============== ASSERT_VISION (校验预期是否成立) ==============

ASSERT_VISION_SYSTEM_PROMPT = """你是 MiniOrange 的视觉断言器（ASSERT_VISION）。

【输入】
- 一张当前屏幕截图（在 user message 的图像里）
- 一段对"预期场景"的文本描述（如『进入首页并出现底部 tab』『支付成功页』『弹出验证码输入框』）

【任务】
判断当前截图是否满足这条预期。

【规则】
1. passed=true 表示预期成立；false 表示不成立。
2. 你的判断是"语义级"的——例如预期『进入手机号验证页』，截图上看到一个手机号输入框 + "获取验证码"按钮，即可视为成立。
3. 强制返回 ai_reasoning + evidence（你在截图上看到的关键元素）；evidence 不能为空字符串。
4. confidence 反映把握程度；模糊不清时给 0.3~0.5。
5. 若 user 提供了【短期记忆】：当前截图是「现在 / 之后」；记忆里是「之前 / 刚做过的事」。
   - 预期是相对变化（数量+1、样式从 A 变 B）时，用记忆中的之前对比当前图，不要因为当前图上看不到变化前而判失败。
   - 预期是找回刚发布/刚操作过的内容时，用记忆中的标题/时间/可见文案对照当前图。
   - 过程态（加载、占位、生成中、切换中）若记忆写明已在中途验证，终态截图上不再出现这些不得判失败。
6. 只根据当前截图判定。图上有的控件/文案就是有。禁止因为「可以关掉所以算不出现」而判通过。
7. 不要返回 markdown / 多个 JSON / 中文叙述外泄；只返回一个 JSON。
8. 若 context 含【壳层】或本步简报：判断导航/选中态时遵守简报。独立入口是否算导航项，以简报为准。简报与屏幕冲突时以屏幕为准。

【输出 JSON Schema（严格）】
{
  "passed": true | false,
  "confidence": 0.0..1.0,
  "ai_reasoning": "<你判断的依据>",
  "evidence": "<截图上看到的关键元素，如『顶部标题 \"验证手机号\" + 11 位手机号输入框 + 灰色"获取验证码"按钮』>"
}"""


ASSERT_VISION_USER_TEMPLATE = """请根据附图判断下面这条预期是否成立。

==== 预期描述 ====
{expectation}

{hint_block}{context_block}请只返回一个 JSON。"""


# ============== HITL_PROMPT_COMPOSER (Step 5) ==============

HITL_PROMPT_COMPOSER_SYSTEM_PROMPT = """你是 MiniOrange 的"问人话术作者"（HITL_PROMPT_COMPOSER）。

【何时触发】
Orchestrator 需要向人采集【可填回自动化流程的数据】（手机号、短信验证码、一段文本、二选一），
调你写出弹框文案。人不会去设备上操作；设备操作由 agent 完成。

【输入】
- hitl_kind：本次交互类型（confirm / input_text / choice_single / choice_multiple / upload_image / acknowledge）
- case_context：当前 case + 当前事件（含 ai_reasoning 解释"为什么需要人工"）
- device_brief：设备信息（model / phone_number / 当前页面 hint，可能为空）
- current_event.params.field：若有，取值 phone / sms_code / text，必须按它采集对应字段

【你的任务】
按 hitl_kind 写出 title + body + 必要的 options/constraints，让前端能直接渲染弹框。

【规则】
1. title：≤30 字，开门见山，写清要的字段（如「请输入11位手机号」「请输入6位验证码」）。
2. body：说明这个数据将由系统填进界面；可换行，但 ≤200 字。禁止写「请到手机上勾选/登录/点同意后回来输入已登录」。
3. options（仅 choice_single / choice_multiple）：[{id, label, hint?}, ...]，
   id 用稳定英文（如 "agree" / "deny"），label 是中文描述，3~6 项为佳。
4. constraints（input_text）必须带 field，并按字段给长度：
   - phone: {field:"phone", regex:"^\\\\d{11}$", min_len:11, max_len:11}
   - sms_code: {field:"sms_code", regex:"^\\\\d{4,8}$", min_len:4, max_len:8}
   - text: {field:"text", min_len:1, max_len:200}
5. constraints（upload_image）：{accept_mime: [...], max_size_kb: int}，
   默认 ["image/png", "image/jpeg"] + max_size_kb=4096。
6. default_timeout_sec：根据交互复杂度给 60~900，验证码常用 300。
7. acknowledge：仅告知，不要指示用户操作设备。
8. confirm：是/否确认一个事实，options 留空（前端默认渲染"是"/"否"）。禁止把 confirm 写成「请去登录」。
9. 若上游事件在让用户操作设备或输入「已登录」口令：改写成采集手机号或验证码（看上下文缺哪个），不要沿用口令。
10. 强制返回 ai_reasoning。只返回一个 JSON 对象，禁止 Markdown / 思考链外泄。

【输出 JSON Schema（严格）】
{
  "title": "请输入11位手机号",
  "body":  "系统会把该号码填进登录页。请只提供号码，不要在设备上自行登录。",
  "options": [],
  "constraints": {"field": "phone", "regex": "^\\\\d{11}$", "min_len": 11, "max_len": 11},
  "default_timeout_sec": 300,
  "ai_reasoning": "..."
}"""


HITL_COMPOSER_USER_TEMPLATE = """请为下面这次 HITL 交互写出弹框话术。

==== hitl_kind ====
{hitl_kind}

==== case_context ====
{case_block}

==== current_event ====
{event_json}

==== device_brief ====
{device_block}

请只返回一个 JSON 对象，遵守 system 中的 schema。"""


def build_hitl_composer_messages(
    *,
    hitl_kind: str,
    case_summary: str,
    event_dict: dict[str, Any],
    device_brief: Optional[dict[str, Any]] = None,
    json_only_emphasis: bool = True,
) -> list[dict[str, str]]:
    """构造 HITL_PROMPT_COMPOSER 的 messages。"""
    case_block = (case_summary or "").strip() or "（无 case 上下文）"
    device_block = (
        json.dumps(device_brief, ensure_ascii=False, indent=2, default=str)
        if device_brief else "（无 device_brief）"
    )
    user_content = HITL_COMPOSER_USER_TEMPLATE.format(
        hitl_kind=hitl_kind or "confirm",
        case_block=case_block,
        event_json=json.dumps(event_dict, ensure_ascii=False, indent=2, default=str),
        device_block=device_block,
    )
    if json_only_emphasis:
        user_content += (
            "\n\n【再次强调】整条回复只能是一个 JSON 对象。"
            "options 仅在 choice_single / choice_multiple 时非空；constraints 仅在 input_text / upload_image 时非空。"
        )
    return [
        {"role": "system", "content": HITL_PROMPT_COMPOSER_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ============== Diff Summarizer (Step 8 占位) ==============

DIFF_SUMMARIZER_SYSTEM_PROMPT = """你是 MiniOrange 的回归 Diff 总结器（DIFF_SUMMARIZER）。
（Step 8 才正式使用，此处保留 skeleton。）"""


# ============== PERSONA_TASK (Step 7) ==============

PERSONA_TASK_SYSTEM_PROMPT = """你是 MiniOrange 的 AI 拟人化任务展开器（PERSONA_TASK）。

【为什么有你】
某些设备/系统能力（清缓存、装包许可、强停应用、修改设置）在当前 Run 没有 adb / 没有 device-owner 权限，
但仍要完成，就必须像真人那样去 UI 上一步步点：长按图标 → 应用信息 → 存储 → 清除数据，
或在弹窗里点"允许此来源安装"。你的工作是把高层任务分解成这种"低级、机器可执行的 UI 操作序列"。

【输入】
- task_description：要完成的系统级目标（"清除 com.miniorange.app 缓存"等）
- device_brief    ：设备指纹（model / os / OEM），不同 OEM 路径差异巨大
- capability_menu ：你能选用的低级 UI capability（tap_element / long_press_element / swipe / press_key /
                    input_text / wait_screen_ready / assert_visual / launch_app 等）。**禁止规划菜单外的能力**。
- current_screen  ：当前屏幕截图（如有），帮助你判断当前在哪
- ai_hint         ：上游调度器给的额外提示（多为失败原因 / 上次尝试摘要）

【硬性规则】
1. 只从 capability_menu 选 capability_id；executor 必须出现在该 capability 的 implementations 中。
2. 每条子事件的 `params` 必须给到下层能直接消费的字段：
     - tap_element / long_press_element：给"selector_text"（目标的视觉/文本特征，VLM 会再定位坐标）
     - input_text：给 selector_text + text
     - press_key：给 keyevent（如 "back" / "home"）
     - launch_app：给 package
     - wait_screen_ready：给 expectation（一句话描述等到什么界面）
3. 不要展开"已经在 Remote/ADB 能一步完成"的事情；如果 device_brief 显示当前其实有 adb 路径，直接 decline。
4. 拟人化路径要严谨：
     - 操作前先 `wait_screen_ready` 或 `assert_visual` 确认到了正确页面
     - 每个不可逆操作（"清除数据"按钮）前安排一次 `assert_visual` 校验
     - 操作完最后一次 `assert_visual` 校验目标态（"应用数据已清除"或"安装完成"）
5. 不要插入 human_* 事件，除非用例真的需要人来回答（验证码、人工授权）。
6. 步骤总数控制在 ≤12 条；超过说明你在硬编路径，应改成 decline 让上层重新规划。
7. 强制返回 ai_reasoning + 每条子事件的 ai_reasoning + label。
8. 不同 OEM 路径差异要写进 ai_reasoning，便于审计。
9. 只返回一个 JSON 对象，禁止 Markdown / 思考链外泄。

【输出 JSON Schema（严格）】
{
  "mode": "expand" | "decline",
  "ai_reasoning": "<整任务为什么这么拆 / 为什么拒绝>",
  "confidence": 0.0~1.0,
  "needs_human": false,
  "sub_events": [
    {
      "seq": 1,
      "case_step_index": null,
      "capability_id": "press_key",
      "event_kind": "press_key",
      "params": { "keyevent": "home" },
      "needs_vlm": false,
      "expected_executor": "remote",
      "fallback_executors": [],
      "ai_reasoning": "先回到桌面，准备长按应用图标",
      "label": "按 Home"
    },
    ...
  ],
  "decline_reason": ""
}

mode=decline 时 sub_events 必须为 []；decline_reason 必填，写明哪里走不通（例如"当前页面识别失败"/
"OEM 路径未知"）。"""


# 名果变体的 system prompt（与通用的差异：聚焦特定场景，给路径范例）
PERSONA_FORCE_STOP_VIA_SETTINGS_SYSTEM_PROMPT = (
    PERSONA_TASK_SYSTEM_PROMPT
    + "\n\n【本次特定任务：强制停止应用 → 必须走系统设置路径】\n"
    "即使 device_brief 显示 adb 可用，也**禁止 decline**；必须展开 UI 步骤完成强制停止。\n"
    "推荐路径（按 MIUI / HyperOS / 原生 Android 在 ai_reasoning 中注明差异）：\n"
    "  1) launch_app 或 press_key home → 回到桌面/设置可进\n"
    "  2) launch_app 设置(com.android.settings) 或 tap_element 『设置』\n"
    "  3) tap_element 『应用』 / 『应用管理』 / 『应用设置』\n"
    "  4) 在列表中找到目标应用（params.package / app_name）；可用 input_text 搜索应用名\n"
    "  5) tap_element 进入『应用信息』\n"
    "  6) tap_element 『强制停止』 / 『强行停止』\n"
    "  7) tap_element 『确定』 / 『强制停止』确认弹窗\n"
    "  8) assert_visual 『已强制停止』或应用信息页显示『强行停止』为灰色不可点\n"
    "禁止仅 press_key home 冒充完成；禁止规划 adb / shell 子步骤。\n"
)

PERSONA_CLEAR_CACHE_VIA_SETTINGS_SYSTEM_PROMPT = (
    PERSONA_TASK_SYSTEM_PROMPT
    + "\n\n【本次特定任务：清空应用存储 → 按当前屏分步推进】\n"
    "即使 device_brief 显示 adb 可用，也**禁止 decline**；必须展开 UI 步骤完成清空存储。\n"
    "【硬性：读 current_screen，只规划后续步骤】\n"
    "  - 先判断截图当前在哪一页，**跳过已完成的阶段**，不要硬套从桌面/设置开头的完整路径。\n"
    "  - 若已在「应用信息」页（可见强制停止、存储、通知等入口），**禁止** launch_app 设置、"
    "禁止从应用列表重新找应用；直接 tap 当前屏可见的「存储空间和缓存」「存储」「存储用量」。\n"
    "  - 若已在存储页且看到「清空存储空间」「清除全部数据」「清除数据」，直接 tap 并处理确认弹窗。\n"
    "  - 这是流程化操作：看到入口就点进去，一步步推进；不是单按钮一步完成，也不要假设必须先到「可直接清缓存」的最终页才动手。\n"
    "参考路径（仅当截图显示尚未到达对应阶段时才使用；OEM 差异写进 ai_reasoning）：\n"
    "  1) launch_app 设置(com.android.settings) 或 tap_element 『设置』\n"
    "  2) tap_element 『应用』 / 『应用管理』\n"
    "  3) 找到目标应用（params.package / app_name）；可用 input_text 搜索\n"
    "  4) tap_element 『应用信息』\n"
    "  5) tap_element 『存储空间和缓存』 / 『存储』 / 『存储用量』\n"
    "  6) tap_element 『清空存储空间』 / 『清除全部数据』 / 『清除数据』\n"
    "  7) tap_element 『确定』 / 『清空』确认弹窗\n"
    "  8) assert_visual 『存储已清空』或成功提示\n"
    "禁止规划 adb pm clear；禁止仅按 Home 冒充完成。\n"
)

PERSONA_ALLOW_INSTALL_SYSTEM_PROMPT = (
    PERSONA_TASK_SYSTEM_PROMPT
    + "\n\n【本次特定任务：处理安装弹窗】\n"
    "推荐路径（按出现顺序，缺则跳过）：\n"
    "  1) wait_screen_ready 『系统询问是否允许此来源』\n"
    "  2) tap_element 『允许』 / 『继续』 / 『设置』\n"
    "  3) tap_element 『允许此来源安装』开关\n"
    "  4) press_key back（回到安装界面）\n"
    "  5) tap_element 『安装』\n"
    "  6) assert_visual 『安装完成』\n"
)


PERSONA_TASK_USER_TEMPLATE = """请把下面的系统任务展开为可由 Remote 执行的拟人化 UI 操作序列。

==== task_description ====
{task_description}

==== device_brief ====
{device_brief_json}

==== capability_menu ====
{menu_json}

==== params（任务自带参数） ====
{params_json}

{hint_block}{screen_note}请只返回一个 JSON 对象。"""


# prompt_template id → system prompt 映射（Implementation.prompt_template 指向这里）
PERSONA_TEMPLATE_REGISTRY: dict[str, str] = {
    "PERSONA_TASK": PERSONA_TASK_SYSTEM_PROMPT,
    "PERSONA_FORCE_STOP_VIA_SETTINGS": PERSONA_FORCE_STOP_VIA_SETTINGS_SYSTEM_PROMPT,
    "PERSONA_CLEAR_CACHE_VIA_SETTINGS": PERSONA_CLEAR_CACHE_VIA_SETTINGS_SYSTEM_PROMPT,
    "PERSONA_ALLOW_INSTALL": PERSONA_ALLOW_INSTALL_SYSTEM_PROMPT,
}


def build_persona_task_messages(
    *,
    task_description: str,
    device_brief: dict[str, Any],
    menu: list[dict[str, Any]],
    params: Optional[dict[str, Any]] = None,
    ai_hint: str = "",
    image_base64: str = "",
    image_mime: str = "image/jpeg",
    template_id: str = "PERSONA_TASK",
    json_only_emphasis: bool = True,
) -> list[dict[str, Any]]:
    """构造 PERSONA_TASK 系列的 messages。

    template_id 决定 system prompt：未知 id 自动 fallback 到 PERSONA_TASK 通用版。
    image_base64 可为空（无截图时模型靠 device_brief + task 推断）。
    """
    system_prompt = PERSONA_TEMPLATE_REGISTRY.get(template_id, PERSONA_TASK_SYSTEM_PROMPT)
    screen_note = (
        "==== current_screen ====（见 user message 中附图）\n\n"
        if image_base64
        else "==== current_screen ====（无截图，请基于 task + device 推断）\n\n"
    )
    user_text = PERSONA_TASK_USER_TEMPLATE.format(
        task_description=(task_description or "").strip() or "（未提供任务描述）",
        device_brief_json=json.dumps(device_brief or {}, ensure_ascii=False, indent=2, default=str),
        menu_json=json.dumps(menu or [], ensure_ascii=False, indent=2, default=str),
        params_json=json.dumps(params or {}, ensure_ascii=False, indent=2, default=str),
        hint_block=_hint_block(ai_hint),
        screen_note=screen_note,
    )
    if json_only_emphasis:
        user_text += "\n【再次强调】整条回复必须是一个 JSON 对象。"
    user_content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if image_base64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_base64}"},
        })
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


# ============== VLM builders ==============


def _hint_block(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    return f"==== ai_hint（上游 reasoning，仅供参考）====\n{text[:600]}\n"


def build_locate_vision_messages(
    *,
    description: str,
    preview_width: int,
    preview_height: int,
    image_base64: str,
    image_mime: str = "image/jpeg",
    ai_hint: str = "",
    json_only_emphasis: bool = True,
) -> list[dict[str, Any]]:
    """构造 LOCATE_VISION 的 messages。image_base64 不带 `data:` 前缀。"""
    user_text = LOCATE_VISION_USER_TEMPLATE.format(
        preview_width=preview_width,
        preview_height=preview_height,
        description=(description or "").strip() or "（未提供描述）",
        hint_block=_hint_block(ai_hint),
    )
    if json_only_emphasis:
        user_text += "\n【再次强调】整条回复只能是一个 JSON 对象，禁止 Markdown / 思考链 / 多个 JSON。"
    user_content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if image_base64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_base64}"},
        })
    return [
        {"role": "system", "content": LOCATE_VISION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def build_assert_vision_messages(
    *,
    expectation: str,
    image_base64: str,
    image_mime: str = "image/jpeg",
    ai_hint: str = "",
    context_block: str = "",
    json_only_emphasis: bool = True,
) -> list[dict[str, Any]]:
    """构造 ASSERT_VISION 的 messages。"""
    ctx = (context_block or "").strip()
    context_fmt = f"{ctx}\n\n" if ctx else ""
    user_text = ASSERT_VISION_USER_TEMPLATE.format(
        expectation=(expectation or "").strip() or "（未提供预期）",
        hint_block=_hint_block(ai_hint),
        context_block=context_fmt,
    )
    if json_only_emphasis:
        user_text += "\n【再次强调】整条回复只能是一个 JSON 对象。"
    user_content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if image_base64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_base64}"},
        })
    return [
        {"role": "system", "content": ASSERT_VISION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ============== Builders ==============


def _case_block(spec: CaseSpec, max_steps: int = 50) -> str:
    """把 CaseSpec 序列化成 LLM 看得舒服的纯文本块。"""
    parts: list[str] = [
        f"case_id: {spec.case_id}",
        f"name   : {spec.name}",
    ]
    if spec.priority:
        parts.append(f"priority: {spec.priority}")
    if spec.tags:
        parts.append(f"tags    : {', '.join(spec.tags)}")
    parts.append("")
    parts.append("preconditions:")
    parts.append(_indent(spec.preconditions.strip() or "（无）"))
    parts.append("")
    parts.append("steps:")
    if not spec.steps:
        parts.append("  （无步骤）")
    for step in spec.steps[:max_steps]:
        head = f"  {step.index}. {step.instruction.strip() or '（无原文）'}"
        parts.append(head)
        if step.expected:
            parts.append(f"     expected: {step.expected.strip()}")
    if len(spec.steps) > max_steps:
        parts.append(f"  …已截断，共 {len(spec.steps)} 步")
    raw_steps = ""
    if isinstance(spec.raw_row, dict):
        raw_steps = str(spec.raw_row.get("steps_raw") or "").strip()
    if raw_steps:
        parts.append("")
        parts.append("steps_raw:")
        parts.append(_indent(raw_steps))
    parts.append("")
    parts.append("overall_expected:")
    parts.append(_indent(spec.expected.strip() or "（无）"))
    return "\n".join(parts)


def _indent(text: str, prefix: str = "  ") -> str:
    if not text:
        return prefix + "（空）"
    return "\n".join(prefix + line for line in text.splitlines())


def _baseline_block(baseline: Optional[BaselineContext]) -> str:
    """格式化 baseline 历史窗口；无 baseline 时返回 ""（builder 会跳过这一段）。"""
    if baseline is None:
        return ""
    lines = ["==== baseline（局部历史窗口，仅供参考，不是脚本）===="]
    if baseline.case_overall_status:
        lines.append(f"case_overall_status: {baseline.case_overall_status}")
    if baseline.notes:
        lines.append(f"notes: {baseline.notes}")
    for label, snip in (
        ("previous", baseline.previous),
        ("current", baseline.current),
        ("next", baseline.next),
    ):
        if snip is None:
            continue
        lines.append(
            f"- {label}: capability={snip.capability_id} status={snip.status} "
            f"executor={snip.executor_used or '-'} summary={snip.summary or '-'}"
        )
        if snip.ai_reasoning:
            lines.append(f"    上次 ai_reasoning: {snip.ai_reasoning[:240]}")
    lines.append("")
    return "\n".join(lines)


def _completed_events_block(events: list[dict[str, Any]], max_items: int = 30) -> str:
    if not events:
        return "[]"
    trimmed = events[-max_items:]  # 只看最近 N 条
    return json.dumps(trimmed, ensure_ascii=False, indent=2, default=str)


def _events_to_serialize(events: Optional[list[Any]], max_items: int = 30) -> list[dict[str, Any]]:
    if not events:
        return []
    out: list[dict[str, Any]] = []
    for ev in events[:max_items]:
        if isinstance(ev, PlanEvent):
            out.append(ev.model_dump(exclude_none=True))
        elif isinstance(ev, dict):
            out.append(ev)
    return out


# ----- Public builders -----


def build_plan_overview_messages(
    *,
    case_spec: CaseSpec,
    run_brief: dict[str, Any],
    menu: list[dict[str, Any]],
    baseline: Optional[BaselineContext] = None,
    baseline_overview_text: str = "",
    json_only_emphasis: bool = True,
) -> list[dict[str, str]]:
    """构造 PLAN_OVERVIEW_TEXT 的 messages（OpenAI-compatible 格式）。

    baseline 注入优先级（择一）：
      1. baseline_overview_text 非空 → 渲染"上次执行总览"块（Step 6 推荐）
      2. baseline BaselineContext → 退化到局部三段窗口（旧链路兼容）
      3. 都无 → "本 case 首次执行" 占位
    """
    if baseline_overview_text and baseline_overview_text.strip():
        baseline_block = (
            "==== baseline（上次执行总览，仅供结构参考，不是脚本）====\n"
            f"{baseline_overview_text.strip()}\n"
        )
    else:
        baseline_block = (
            _baseline_block(baseline)
            or "==== baseline ====\n（本 case 首次执行，无历史可参考）\n"
        )

    user_content = PLAN_OVERVIEW_USER_TEMPLATE.format(
        case_block=_case_block(case_spec),
        run_brief_json=json.dumps(run_brief, ensure_ascii=False, indent=2, default=str),
        menu_json=json.dumps(menu, ensure_ascii=False, indent=2, default=str),
        baseline_block=baseline_block,
    )
    if json_only_emphasis:
        user_content += (
            "\n\n【再次强调】整条回复有且只能是一个合法 JSON 对象（{ 开头 } 结尾），"
            "禁止 Markdown、禁止思考链、禁止多个 JSON。"
        )
    return [
        {"role": "system", "content": PLAN_OVERVIEW_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


def build_single_step_replan_messages(
    *,
    run_brief: dict[str, Any],
    menu: list[dict[str, Any]],
    completed_events: list[Any],
    failed_event: Any,
    failure_summary: str,
    remaining_events: Optional[list[Any]] = None,
    baseline: Optional[BaselineContext] = None,
    json_only_emphasis: bool = True,
) -> list[dict[str, str]]:
    """构造 SINGLE_STEP_REPLAN 的 messages。"""
    completed_serial = _events_to_serialize(completed_events)
    remaining_serial = _events_to_serialize(remaining_events)
    failed_serial: dict[str, Any]
    if isinstance(failed_event, PlanEvent):
        failed_serial = failed_event.model_dump(exclude_none=True)
    elif isinstance(failed_event, dict):
        failed_serial = failed_event
    else:
        failed_serial = {"raw": str(failed_event)}

    user_content = SINGLE_STEP_REPLAN_USER_TEMPLATE.format(
        run_brief_json=json.dumps(run_brief, ensure_ascii=False, indent=2, default=str),
        menu_json=json.dumps(menu, ensure_ascii=False, indent=2, default=str),
        completed_json=json.dumps(completed_serial, ensure_ascii=False, indent=2, default=str),
        failed_json=json.dumps(failed_serial, ensure_ascii=False, indent=2, default=str),
        failure_summary=(failure_summary or "").strip() or "（未提供详细失败说明）",
        remaining_json=json.dumps(remaining_serial, ensure_ascii=False, indent=2, default=str),
        baseline_block=_baseline_block(baseline) or "==== baseline ====\n（无 baseline 可参考）\n",
    )
    if json_only_emphasis:
        user_content += (
            "\n\n【再次强调】整条回复有且只能是一个合法 JSON 对象。"
            "mode=replan 时 events 不能为空；mode=decline / give_up 时 events 可以为空。"
        )
    return [
        {"role": "system", "content": SINGLE_STEP_REPLAN_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


# ============== Estimation utility ==============


def estimate_message_size(messages: list[dict[str, str]]) -> int:
    """粗算 prompt 字节数（UTF-8 大约 1 字符 ≈ 3 字节中文）。"""
    return sum(len((m.get("content") or "").encode("utf-8")) for m in messages)


# ============================================================================
# Agent 执行引擎（目标导向闭环，仅 adb 通道）—— GOAL_EXTRACT + AGENT_DECIDE
# ============================================================================

GOAL_EXTRACT_SYSTEM_PROMPT = """你是资深移动端测试分析师。把一条测试用例转成【目标 + 有序检查点】，供 agent 逐步执行时判断进度与成功。

只返回一个 JSON 对象：
{
  "goal": "整条用例要达成的总体目标（自然语言，一句话）",
  "checkpoints": [
    {"id": "cp1", "kind": "process 或 terminal", "description": "可在屏幕上直接观测到的里程碑"}
  ],
  "success_criteria": "最终判定用例成功的可视化标准（只描述完成后的稳定屏）",
  "ai_reasoning": "你的抽取思路"
}

要求：
- 检查点必须来自用例「预期 / overall_expected / 各步 expected」，保留原文，不要改写、不要合并、不要扩写。
- 禁止把测试步骤改写成检查点（不要写「已进入首页」「已点击底部导航」这类过程态，除非预期原文就是这么写的）。
- 没有预期时，才允许根据用例名给 1 个终态检查点。
- kind=process：只在过程中出现的状态（加载占位、生成中、切换中、进度条、转圈）。必须在该画面还在时中途验证，不要写进 success_criteria。
- kind=terminal：完成后仍留在屏幕上的稳定状态。
- success_criteria 只写终态：完成后的稳定界面上能看见什么。禁止把加载/占位/生成中/切换中写进最终成功标准。
- 空态必须写清对象：信息流/列表空、个人作品空、未登录是三种不同状态，禁止写成「退出登录后社区为空」。
- 忽略"清缓存/启动应用"等前置（由系统前置条件处理），聚焦用例主体目标。
- 禁止 Markdown、禁止多个 JSON。"""

GOAL_EXTRACT_USER_TEMPLATE = """==== 用例 ====
{case_block}

请抽取 goal + checkpoints + success_criteria，只返回一个 JSON。"""


def build_goal_extract_messages(*, case_spec: "CaseSpec") -> list[dict[str, Any]]:
    user_text = GOAL_EXTRACT_USER_TEMPLATE.format(case_block=_case_block(case_spec))
    return [
        {"role": "system", "content": GOAL_EXTRACT_SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]


AGENT_DO_SYSTEM_PROMPT = """你是操控一台移动设备的自动化 agent（通过当前可用通道执行：adb / 远程节点 / iOS WDA）。你会看到【当前屏幕截图】，只负责【当前步骤的操作】。校验由系统在操作结束后单独做，你不要验、不要为预期去点。

只返回一个 JSON 对象：
{
  "thought": "先描述当前这屏是什么，再说为什么选下面这一步",
  "status": "continue | done | give_up | ask_human",
  "action": {"capability_id": "菜单里的能力", "params": { ... }},
  "expected_after": "执行后界面大概会变成什么样（给自己看，不是校验结论）",
  "confidence": 0.0~1.0,
  "remember": ["本步要记住、后面还要用的事实"],
  "checkpoint_ids": [],
  "subflow": "none 或 create_publish",
  "published": null,
  "knowledge_ids": []
}

铁律：
1. 下一步动作优先用 function call（工具名 = capability id）。一次只调一个工具。
   本步操作结束 → signal_done；客观做不到 → signal_give_up；要人填能进输入框的信息 → signal_ask_human。
   没有 tools 时仍只返回上面的 JSON，capability_id 必须来自 capability_menu。
   菜单里没有 assert / 校验能力。禁止臆造工具名。
2. 坐标一律用【0-1000 归一化整数】：x=横向千分比、y=纵向千分比。屏幕正中央 = x:500,y:500。
   - tap_element: params={"x":0-1000,"y":0-1000}
   - multi_tap: params={"x":0-1000,"y":0-1000,"count":6,"interval_ms":80}  连点彩蛋用这条，禁止拆成多次 tap_element
   - swipe_element_to_element: params={"from_x","from_y","to_x","to_y"（均 0-1000）,"duration_ms"}
   - input_text: params={"text":str,"x":0-1000,"y":0-1000}(先点输入框再输入)
   - launch_app/close_app: params={"package":str}
   - get_app_version: params={"package":str}  读安装包 versionName，禁止看图猜版本
   - get_foreground_app: params={}  读当前前台包名，禁止看图猜是不是目标 App
   - press_key: params={"key":"back|home|..."}
   - wait_ms: params={"ms":int}
3. 你能看图：遇到未预期的隐私协议/权限申请/更新提示/广告弹窗等，主动点同意/允许/关闭/稍后，不要卡住。
★【启动应用只能启动被测目标应用】：launch_app/open_app 的 package 必须用下方「目标应用」给定的包名。
★【版本 / 前台】：客户端版本和是不是目标 App 在前台，用 get_app_version / get_foreground_app，禁止看图猜。
4. **只做当前步骤的操作**。status="done" 只表示【本步操作结束】，不是整案完成，也不是校验通过。
   - 禁止去做后面的步骤。
   - 禁止自己校验预期、禁止输出 assert 类能力。
   - 禁止为了让后面的预期成立而关闭/隐藏/返回。
5. 客观无法完成 → status="give_up"。加载/占位用 wait_ms。
6. 需要人提供【能填进界面的信息】→ status="ask_human"。禁止让人去设备上点。
   - 资源网关已租账号时：当前屏是登录/手机号输入 → 必须 input_text 并带 field=phone，text 可写占位。
   - 当前屏在问一次性口令 → 必须 input_text 并带 field=sms_code。值由系统填入。
   - 禁止 ask_human 要号或码，禁止把口令写进 thought。
   - human_input_text: params={"question":"...","field":"sms_code|phone|text"}
7. 不要连续多次点同一位置无效动作；不要盲目连按返回键退出应用。
   需要连点触发调试面板/版本号彩蛋时，必须用 multi_tap 一次发出，禁止拆成多次 tap_element。
8. 已执行动作历史和【短期记忆】会给你。后面要对比变化时先写入 remember。
9. 当前屏明显在加载/转圈时，用 wait_ms，禁止乱点。
10. 【本步简报】是编译结果，不是某条原文。与当前屏幕冲突时以屏幕为准。还需某条原文时把 id 写入 knowledge_ids。
11. 每个应用的登录、退出、业务路径都不同。只按本应用简报和当前截图操作，禁止套用其它 App 的界面结构。
禁止 Markdown、禁止思考链、禁止多个 JSON。"""


AGENT_DECIDE_SYSTEM_PROMPT = """你是操控一台移动设备的自动化 agent（通过当前可用通道执行：adb / 远程节点 / iOS WDA）。你会看到【当前屏幕截图】，要朝着【目标】推进，每次只决定并输出【下一步一个动作】。

只返回一个 JSON 对象：
{
  "thought": "先描述当前这屏是什么，再说为什么选下面这一步",
  "status": "continue | done | give_up | ask_human",
  "action": {"capability_id": "菜单里的能力", "params": { ... }},
  "expected_after": "执行后预期出现的状态（供下一步自检）",
  "confidence": 0.0~1.0,
  "remember": ["本步要记住、后面还要用的事实"],
  "checkpoint_ids": ["本步正在验证的检查点 id，可空"],
  "subflow": "none 或 create_publish",
  "published": null,
  "knowledge_ids": []
}

铁律：
1. capability_id 必须来自 capability_menu（菜单只有 id 和一句 summary），禁止臆造。
2. 坐标一律用【0-1000 归一化整数】：x=横向千分比、y=纵向千分比（与分辨率无关，系统会按真实屏幕尺寸换算成像素）。例如屏幕正中央 = x:500,y:500；右下角 ≈ x:950,y:950。
   - tap_element: params={"x":0-1000,"y":0-1000}
   - multi_tap: params={"x":0-1000,"y":0-1000,"count":6,"interval_ms":80}  连点彩蛋用这条，禁止拆成多次 tap_element
   - swipe_element_to_element: params={"from_x","from_y","to_x","to_y"（均 0-1000）,"duration_ms"}
   - input_text: params={"text":str,"x":0-1000,"y":0-1000}(先点输入框再输入)
   - launch_app/close_app: params={"package":str}
   - get_app_version: params={"package":str}  读安装包 versionName，禁止看图猜版本
   - get_foreground_app: params={}  读当前前台包名，禁止看图猜是不是目标 App
   - press_key: params={"key":"back|home|..."}
   - wait_ms: params={"ms":int}
   - assert_visual: params={"expectation":"当前屏上应看到的客观状态"}
3. 你能看图，所以【自己判断当前页面】：遇到未预期的隐私协议/权限申请/更新提示/广告弹窗等，主动决定如何越过它（点同意/允许/关闭/稍后），不要卡住。
★【启动应用只能启动被测目标应用】：launch_app/open_app 的 package 必须用下方「目标应用」给定的包名，禁止凭屏幕图标或记忆猜其它包名。目标应用已在前置步骤启动时，通常无需再 launch。
★【版本 / 前台】：客户端版本和是不是目标 App 在前台，用 get_app_version / get_foreground_app，禁止看图猜。
4. **严格按步骤编号执行**：每次只做「当前步骤」的操作；做完后才校验同号预期。禁止跳到后面的步骤，禁止提前验后面的检查点。
   - 操作阶段：status="done" 只表示【本步操作结束】，不是整案完成。
   - 校验阶段：只能 wait_ms（仍在加载）或 assert_visual。禁止点击/关闭/返回/滑动来让预期成立。屏幕上现在是什么就按什么判。
   - 预期写「不出现 X」时，若 X 在当前屏上，就是没过。禁止关掉 X 再验。
5. 客观无法完成（反复卡死、缺少必要条件）→ status="give_up"，thought 写清原因。加载/占位/生成中属于过程，用 wait_ms，不要用过程态去填成功标准。
6. 需要人提供【系统下一步能填进界面的信息】→ status="ask_human"。只允许向人要数据，禁止让人去设备上点选/登录/勾协议。
   - 资源网关已租账号时：当前屏是登录/手机号输入 → 必须 input_text 并带 field=phone，text 可写占位，禁止 ask_human 再要手机号。
   - 当前屏在问一次性口令 → 必须 input_text 并带 field=sms_code。值由系统填入，禁止再问人，禁止写出口令。
   - 仅当资源网关也没有对应字段时才 human_input_text。
   - human_input_text: params={"question":"请输入短信验证码","field":"sms_code"}
     field 必须明确：phone=手机号，sms_code=短信验证码，text=其它要填的字符串。拿到后由你自己 input_text/tap 填入。
   - human_confirm: params={"question":"..."} 仅确认一个事实（是/否），不是「请你去登录」。
   - human_choice_single: params={"question":"...","choices":["A","B"]}
   禁止 human_acknowledge / 禁止让用户输入「已登录」这类操作口令。设备操作必须由你完成。
7. 操作纪律：不要连续多次点同一位置无效动作；能一步到位就别绕；不要盲目连按返回键退出应用。
   需要连点触发调试面板/版本号彩蛋时，必须用 multi_tap 一次发出，禁止拆成多次 tap_element。
8. 已执行动作历史和【短期记忆】会给你。后面要对比变化（点赞前数量/样式）或找回刚做的内容时，先写入 remember，禁止丢了再去别处猜。
9. 当前屏明显在加载/转圈/进度未完成时，用 wait_ms 等待即可。禁止在加载页乱点或反复返回。
10. 短期记忆：操作前把计数、样式、对象名称写入 remember；发布成功当屏必须把可找回该内容的指纹写入 published（title/when/note 用屏幕上可见的文案，不要编造）并把 subflow 设回 none。
11. 【本步简报】是编译结果，不是某条原文。与当前屏幕冲突时以屏幕为准。还需某条原文时把 id 写入 knowledge_ids。禁止臆造简报出处里没有的 id。
12. 每个应用的登录、退出、业务路径都是它自己的。只按本步简报和当前截图操作；禁止套用其它 App 的界面结构。
禁止 Markdown、禁止思考链、禁止多个 JSON。"""

AGENT_WEB_CHANNEL_ADDENDUM = """

【网页通道 playwright】这是本机浏览器页，不是手机。
- expected_executor 用 playwright；禁止 adb / remote / ios_wda。
- tap_element 优先 params.selector_text 或 description（按钮/链接上的可见文字），坐标只作兜底。
- input_text：params.text + 输入框名字（label / placeholder）。
- launch_app 的 url 用「目标应用」里的网址，不要填 Android 包名。
- press_key 的 back 等于 Escape。不要规划清缓存、授权弹窗、杀进程。"""


def _playwright_brief(device_brief: dict[str, Any]) -> bool:
    flags = (device_brief or {}).get("flags") or {}
    if flags.get("playwright"):
        return True
    channels = (device_brief or {}).get("channels") or {}
    return str(channels.get("playwright") or "") in ("available", "connected")


AGENT_DECIDE_USER_TEMPLATE = """==== 目标 ====
{goal}

==== 本步要完成的操作（做完 status=done；不要自己校验）====
{success_criteria}

==== 目标应用（launch/open 必须用此标识：App 包名或网页网址）====
{target_app}

==== 会话观察（点过身份页/登录页之后才有；首页看不出登录态时为空，不要为此问人）====
{session_block}

==== 已租测试资源（公开信息；口令不在这里，登录页 field=phone / 口令页 field=sms_code，值由网关填）====
{accounts_brief}

==== 检查点 / 步骤指针（只做标记为当前的那一步）====
{checkpoints_block}

==== 设备/通道 ====
{device_brief_json}

==== 当前屏幕 screen_size ====
width={width}, height={height}

==== 可用能力 capability_menu ====
{menu_json}

==== 已执行动作历史（最近在后）====
{history_block}

==== 短期记忆（后面找内容 / 对比变化时用这些，不要丢掉）====
{memory_block}
{knowledge_block}{hierarchy_block}
请看【下方截图】决定下一步一个操作。优先 function call；没有 tools 时只返回一个 JSON 对象。"""


def build_agent_do_messages(
    *,
    goal: str,
    checkpoints_block: str,
    device_brief: dict[str, Any],
    menu: list[dict[str, Any]],
    history_block: str,
    width: int,
    height: int,
    image_base64: str,
    image_mime: str = "image/png",
    hierarchy_text: str = "",
    target_package: str = "",
    target_app_name: str = "",
    success_criteria: str = "",
    memory_block: str = "",
    knowledge_hint: str = "",
    session_block: str = "",
    accounts_brief: str = "",
) -> list[dict[str, Any]]:
    messages = build_agent_decide_messages(
        goal=goal,
        checkpoints_block=checkpoints_block,
        device_brief=device_brief,
        menu=menu,
        history_block=history_block,
        width=width,
        height=height,
        image_base64=image_base64,
        image_mime=image_mime,
        hierarchy_text=hierarchy_text,
        target_package=target_package,
        target_app_name=target_app_name,
        success_criteria=success_criteria,
        memory_block=memory_block,
        knowledge_hint=knowledge_hint,
        session_block=session_block,
        accounts_brief=accounts_brief,
    )
    messages[0] = {"role": "system", "content": AGENT_DO_SYSTEM_PROMPT + (
        AGENT_WEB_CHANNEL_ADDENDUM if _playwright_brief(device_brief) else ""
    )}
    return messages


def build_agent_decide_messages(
    *,
    goal: str,
    checkpoints_block: str,
    device_brief: dict[str, Any],
    menu: list[dict[str, Any]],
    history_block: str,
    width: int,
    height: int,
    image_base64: str,
    image_mime: str = "image/png",
    hierarchy_text: str = "",
    target_package: str = "",
    target_app_name: str = "",
    success_criteria: str = "",
    memory_block: str = "",
    knowledge_hint: str = "",
    session_block: str = "",
    accounts_brief: str = "",
) -> list[dict[str, Any]]:
    hierarchy_block = ""
    if hierarchy_text and hierarchy_text.strip():
        hierarchy_block = (
            "\n==== 本页可点元素（仅辅助定位，不是登录/业务结论）====\n"
            f"{hierarchy_text.strip()[:4000]}\n"
        )
    knowledge_block = ""
    if knowledge_hint and knowledge_hint.strip():
        knowledge_block = (
            "\n==== 本步简报（编译结果，不是某条原文；与屏幕冲突时以屏幕为准。"
            "还需某条原文时填 knowledge_ids）====\n"
            f"{knowledge_hint.strip()[:4000]}\n"
        )
    user_text = AGENT_DECIDE_USER_TEMPLATE.format(
        goal=(goal or "").strip() or "（未提供目标）",
        success_criteria=(success_criteria or "").strip() or "（未提供，凭目标自行判断）",
        target_app=(f"{target_app_name}（{target_package}）" if target_package else "（未指定，谨慎启动应用）"),
        checkpoints_block=checkpoints_block or "（无）",
        device_brief_json=json.dumps(device_brief, ensure_ascii=False, indent=2, default=str),
        width=width,
        height=height,
        menu_json=json.dumps(menu, ensure_ascii=False, default=str),
        history_block=history_block or "（这是第一步）",
        memory_block=(memory_block or "").strip() or "（暂无）",
        session_block=(session_block or "").strip() or "（尚未观察；首页/信息流看不出登录态属正常，继续按目标操作）",
        accounts_brief=(accounts_brief or "").strip() or "（未租到账号；登录所需手机号只能问人）",
        knowledge_block=knowledge_block,
        hierarchy_block=hierarchy_block,
    )
    user_content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if image_base64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_base64}"},
        })
    return [
        {"role": "system", "content": AGENT_DECIDE_SYSTEM_PROMPT + (
            AGENT_WEB_CHANNEL_ADDENDUM if _playwright_brief(device_brief) else ""
        )},
        {"role": "user", "content": user_content},
    ]


AGENT_RESTART_SYSTEM_PROMPT = """你在为一条新的自动化用例做开场判断。请看【当前屏幕截图】，决定是否需要先强制关闭并重新打开【目标应用】，再开始本条用例。

只返回一个 JSON 对象：
{
  "restart": true 或 false,
  "thought": "当前这屏是什么，以及为什么重启或不重启"
}

需要重启（restart=true）的典型情况（通用，不要假设某个 App 的页面名）：
- 当前不在目标应用内（桌面、其它 App、系统页）
- 明显是上一条用例残留（与本条目标入口无关的详情/结果/弹窗/卡死加载）
- 应用无响应、白屏、或无法用返回回到本条起点

不需要重启（restart=false）：
- 已在目标应用内，且当前页可以作为本条的合理起点（允许先返回/导航到入口）
- 仅缺登录或权限弹窗，用页面操作即可，不必杀进程

禁止 Markdown、禁止多个 JSON。"""

AGENT_RESTART_USER_TEMPLATE = """==== 本条用例目标 ====
{goal}

==== 前置条件 ====
{preconditions}

==== 目标应用 ====
{target_app}

请看【下方截图】判断是否重启目标应用，只返回一个 JSON 对象。"""


def build_restart_decide_messages(
    *,
    goal: str,
    preconditions: str = "",
    target_package: str = "",
    target_app_name: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
) -> list[dict[str, Any]]:
    target_app = (
        f"{target_app_name}（{target_package}）" if target_package else "（未指定）"
    )
    user_text = AGENT_RESTART_USER_TEMPLATE.format(
        goal=(goal or "").strip() or "（未提供目标）",
        preconditions=(preconditions or "").strip() or "（未提供）",
        target_app=target_app,
    )
    user_content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if image_base64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_base64}"},
        })
    return [
        {"role": "system", "content": AGENT_RESTART_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


CASE_SCENE_JSON_SCHEMA: dict[str, Any] = {
    "title": "case_scene",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "session_prep", "required_session", "auth_under_test",
        "device_need", "platform", "prep_items", "reason",
    ],
    "properties": {
        "session_prep": {"type": "string", "enum": ["relogin", "logout", "skip"]},
        "required_session": {"type": "string", "enum": ["logged_in", "guest", "any"]},
        "auth_under_test": {"type": "boolean"},
        "device_need": {"type": "string", "enum": ["app", "web", "app_web", "ab_pair"]},
        "platform": {"type": "string", "enum": ["android", "ios", "web", "any"]},
        "prep_items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["text", "kind", "phase"],
                "properties": {
                    "text": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": [
                            "clear_cache", "check_sim", "check_wechat", "check_no_wechat",
                            "check_ios_device", "check_android_device", "check_logged_in",
                            "check_not_logged_in", "keep_permission_prompt",
                            "check_app_foreground", "check_app_version", "web_config",
                            "remote_config", "backend_data", "sms_live",
                            "external_channel", "device_mock", "unknown",
                        ],
                    },
                    "phase": {"type": "string", "enum": ["before_launch", "after_launch"]},
                },
            },
        },
        "reason": {"type": "string"},
    },
}

CASE_SCENE_SYSTEM_PROMPT = """你在为一条自动化用例做开场场景理解。只读用例原文，不看截图，不规划点击。

只返回一个 JSON 对象：
{
  "session_prep": "relogin | logout | skip",
  "required_session": "logged_in | guest | any",
  "auth_under_test": true,
  "device_need": "app | web | app_web | ab_pair",
  "platform": "android | ios | web | any",
  "prep_items": [{"text": "...", "kind": "...", "phase": "before_launch | after_launch"}],
  "reason": "一句话"
}

session_prep 决定前置要不要动登录态：
- relogin：业务用例。前置退出再登录已租账号。名称/步骤不是在测登录、退出、注册本身。
- logout：测登录/注册，或前置明确要游客/未登录。前置只退出，不要自动登录。
- skip：测退出登录、切换账号，或步骤本身就是「先退出再登录」。登录态留给用例自己做。

required_session：
- guest：前置写了未登录/游客。
- logged_in：前置写了已登录/保持登录（这只是环境，不是登录测试）。
- any：没写登录要求。

device_need / platform 决定这趟要占哪类设备（按整条用例理解，不要因为出现某几个字就加减端）：
- app：这趟只在手机 App 上操作。
- web：这趟要在网页/管理端/浏览器里操作。
- app_web：这趟既要操作 App，也要操作网页。
- ab_pair：这趟需要两台手机同时在场（互发、主客号、双机）。
- platform：android / ios / web / any。没写系统就 android。
占设备看「这趟会不会真的点到那块屏」，不看词表。前置里写的环境（包括运营配置、开关状态）如果本趟步骤不会去那个端上操作，就不要为它多占一台；如果步骤就是要去那个端上做，就必须占。

prep_items：把「前置条件」每条拆成可执行检查。kind 只能用枚举：
- clear_cache / check_sim / check_wechat / check_no_wechat
- check_ios_device / check_android_device
- check_logged_in / check_not_logged_in
- keep_permission_prompt
- check_app_version（客户端版本 ≥x）
- check_app_foreground（当前已打开 App）
- web_config（运营后台/远程开关类环境；占不占网页由 device_need 决定，不由这个 kind 决定）
- remote_config / backend_data / sms_live / external_channel / device_mock
- unknown（运营配置、无法自动化的环境描述）
phase：清缓存/SIM/微信/设备/权限询问/版本/前台/运营配置 → before_launch；已登录/未登录 → after_launch。

硬性规则：
- 「登录后看到…」「已登录用户打开首页」是业务 → relogin。
- 「需求上线前注册的用户」是用户标签，不是注册测试 → relogin。
- 「手机号登录 / 验证码登录 / 点击登录 / 登录页」出现在名称或步骤里，是在测登录 → logout。
- 「退出登录 / 切换账号」是在测退出 → skip。
- 不要因为预期写了「不应出现登录页」就把业务用例判成登录测试。看名称和步骤，不看预期里的否定句。
- 拿不准时 session_prep=skip，不要猜成 relogin（误自动登录会把登录用例做掉）。
- 禁止 Markdown、禁止多个 JSON。"""

CASE_SCENE_USER_TEMPLATE = """==== 用例名称 ====
{name}

==== 前置条件 ====
{precondition}

==== 测试步骤 ====
{steps}

==== 预期 ====
{expected}

根据以上原文判断登录闸门、要占什么设备、前置每条是什么 kind，只返回一个 JSON 对象。"""


def build_case_scene_messages(
    *,
    name: str = "",
    precondition: str = "",
    steps: str = "",
    expected: str = "",
) -> list[dict[str, Any]]:
    user_text = CASE_SCENE_USER_TEMPLATE.format(
        name=(name or "").strip() or "（未写名称）",
        precondition=(precondition or "").strip() or "（未写前置）",
        steps=(steps or "").strip() or "（未写步骤）",
        expected=(expected or "").strip() or "（未写预期）",
    )
    return [
        {"role": "system", "content": CASE_SCENE_SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]


INSPECT_SESSION_SYSTEM_PROMPT = """你在为一条自动化用例观察【当前屏幕】上的登录会话。只判断，不要规划点击。

只返回一个 JSON 对象：
{
  "session": "logged_out | logged_in | unknown",
  "identity": "match | mismatch | unknown",
  "seen": "屏幕上用来判断的文案（登录按钮 / 昵称 / 手机尾号），没有就空",
  "probe": false,
  "next": "keep | logout | login | switch | human",
  "reason": "一句话"
}

规则：
- 每个应用的登录入口、已登录特征、退出路径都不同。只根据【本应用说明书/知识】和当前截图判断。禁止套用其它 App 的底栏、我的页、登录弹窗。
- 禁止用「底栏是否齐全」「有没有首页 Tab」「桌面图标」当登录态。
- 当前截图是系统桌面/启动器/其它 App → session=logged_out 或 unknown。不要把桌面图标（含微信）当成微信登录页。
- 登录页、一键登录、验证码登录、手机号登录、访客浏览 → session=logged_out。
- 知识写了已登录特征且当前屏命中（昵称/手机号/退出）→ session=logged_in。
- 游客和登录共用底栏时，不能只因为有底栏就判已登录。
- 当前屏是首页/信息流/内容页，没有登录按钮也没有昵称/手机号/退出 → session=unknown，identity=unknown，next=keep。这不是失败，不要 next=human。
- 当前屏已经是登录页或身份页但仍看不清、且不是在问可填字段 → next=human。
- 口令/验证码输入页是登录流程：session=logged_out，next=login。禁止因为要填口令就 next=human。口令由资源网关提供。
- 用例要求游客且已登录 → next=logout。
- 用例要求指定账号且当前屏已能看出对不上 → next=switch。
- 只有本应用屏幕上「只能微信登录、没有手机号入口」才算微信不可测；桌面上的微信图标不算。
- 禁止编造昵称。禁止 Markdown、禁止多个 JSON。"""

INSPECT_SESSION_USER_TEMPLATE = """==== 本条用例要的会话 ====
{required_session}

==== 本步相关知识 ====
{knowledge_hint}

==== 已租账号公开信息（尾号 / 标签，不含口令）====
{accounts_brief}

请看【下方截图】判断当前登录态，只返回一个 JSON 对象。"""


def build_inspect_session_messages(
    *,
    required_session: str = "",
    knowledge_hint: str = "",
    accounts_brief: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
) -> list[dict[str, Any]]:
    user_text = INSPECT_SESSION_USER_TEMPLATE.format(
        required_session=(required_session or "").strip() or "（未写明，记录看到的即可）",
        knowledge_hint=(knowledge_hint or "").strip() or "（本步未命中知识）",
        accounts_brief=(accounts_brief or "").strip() or "（未租到账号）",
    )
    user_content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if image_base64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_base64}"},
        })
    return [
        {"role": "system", "content": INSPECT_SESSION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


INSPECT_ENV_SYSTEM_PROMPT = """你在为一次回归任务观察【当前屏幕】上的客户端环境。只判断，不要规划点击。

只返回一个 JSON 对象：
{
  "env": "dev | test | pre | prod | unknown",
  "seen": "屏幕上用来判断环境的文案（角标、设置页、关于页、环境名），没有就空",
  "reason": "一句话"
}

规则：
- env 必须是本趟环境表里的 key：dev=开发、test=测试、pre=预发、prod=正式。看不清就 unknown。
- 只根据【当前截图】和【本应用说明书/知识】判断。禁止用网页域名猜 App 环境。
- 禁止套用其它 App 的设置路径。禁止编造没看见的角标。
- 当前是系统桌面/启动器/其它 App → env=unknown。
- 登录页、启动页、没有角标/环境名的普通业务页 → env=unknown。unknown 只表示这屏看不出来，不是「环境不对」。
- 禁止 Markdown、禁止多个 JSON。"""

INSPECT_ENV_USER_TEMPLATE = """==== 本趟要的环境 ====
{wanted_env}

==== 本应用如何识别/切换环境 ====
{knowledge_hint}

请看【下方截图】判断当前客户端环境，只返回一个 JSON 对象。"""


def build_inspect_env_messages(
    *,
    wanted_env: str = "",
    wanted_label: str = "",
    knowledge_hint: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
) -> list[dict[str, Any]]:
    want = (wanted_env or "").strip()
    label = (wanted_label or "").strip()
    wanted = f"{want}（{label}）" if want and label and label != want else (label or want or "（未指定）")
    user_text = INSPECT_ENV_USER_TEMPLATE.format(
        wanted_env=wanted,
        knowledge_hint=(knowledge_hint or "").strip() or "（没有切换说明，只根据屏幕判断）",
    )
    user_content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    if image_base64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_base64}"},
        })
    return [
        {"role": "system", "content": INSPECT_ENV_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


KNOWLEDGE_CAPTURE_SYSTEM = """你是移动端测试知识管理员。根据一条（或一批）用例的执行结果，往事实层提草案，不要再堆散文。

只返回 JSON：
{
  "items": [
    {
      "proposal_kind": "align|conflict|new_fact",
      "title": "短标题",
      "category": "应用基础逻辑|业务逻辑|UI导航|登录注册|Tab切换|交互规范|其他",
      "tags": ["标签"],
      "content": "可操作的一条事实",
      "question": "需要用户确认时的提问，可空",
      "facet": "chrome|server|hybrid|exception",
      "situation": {"need": "fill|judge_selected|judge|howto", "slot": "", "surface": "app|web", "lane": "prep|step|expect"},
      "bind": {"slot": "identity.otp|identity.phone|identity.password", "value": "", "env": "test|staging|prod", "surface": "app|web"},
      "conflicts_with": "冲突时填写已有知识 id，可空"
    }
  ]
}

规则：
- proposal_kind：align=印证已有事实；conflict=同一槽/同一壳层规则和已通过条目打架；new_fact=新边。
- 口令/验证码/密码只允许写在 bind，不要写进 content。
- 1~3 条，宁缺毋滥。没有值得沉淀的事实就返回 {"items": []}。
- 失败且说不清正确操作时，用 new_fact + question 请用户补，不要编造控件。
- 禁止编造没在记录里出现的控件。禁止 Markdown。"""


def build_knowledge_capture_messages(*, context: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": KNOWLEDGE_CAPTURE_SYSTEM},
        {"role": "user", "content": context.strip() or "（无上下文）"},
    ]


LOGIN_LEARN_SYSTEM = """你是移动端测试知识管理员。当前用例因登录态不满足而停止，需要把「这个 App 怎么登录」写成待审核草稿。

只返回 JSON：
{
  "items": [
    {
      "title": "如何登录",
      "category": "登录注册",
      "tags": ["登录"],
      "content": "可操作的登录说明",
      "question": "需要用户确认的问题，可空"
    }
  ]
}

规则：
- 根据截图/屏文描述：登录入口、可见方式（手机号+验证码 / 微信 / 一键登录）、关键按钮文案、登录页特征。
- 微信/第三方无法自动完成时，写明「需人工在设备外完成」以及屏上看到的入口文案。
- 禁止编造没看到的控件。1~2 条即可。标题优先用「如何登录」或「登录页特征」。
- 禁止 Markdown。"""


def build_login_learn_messages(
    *,
    context: str,
    image_base64: str = "",
    image_mime: str = "image/png",
) -> list[dict[str, Any]]:
    user_content: list[dict[str, Any]] = [
        {"type": "text", "text": (context or "").strip() or "（无上下文）"},
    ]
    if image_base64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_mime};base64,{image_base64}"},
        })
    return [
        {"role": "system", "content": LOGIN_LEARN_SYSTEM},
        {"role": "user", "content": user_content},
    ]
