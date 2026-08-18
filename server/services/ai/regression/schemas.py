# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""AI 回归 Plan/Replan/Baseline 的数据契约。

设计原则：
  - 强制 ai_reasoning：所有 AI 输出必须带"为什么这么做"，便于复盘
  - decline 模式：AI 不强行规划，宁可拒绝
  - capability_id 必须出现在 RunContext 的可用菜单里（由 planner 校验）
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ============== 用例输入 ==============


class CaseStep(BaseModel):
    """用例表里的一条操作步骤。"""

    model_config = ConfigDict(extra="allow")

    index: int = Field(..., description="1-based 步骤序号")
    instruction: str = Field("", description="步骤原文，如「点击一键登录」")
    expected: str = Field("", description="该步骤的预期结果（可空，整 case 可能只在最后给整体预期）")
    raw: dict[str, Any] = Field(default_factory=dict, description="原始飞书行，便于追溯")


class CaseSpec(BaseModel):
    """整条用例的纯文本规约（喂给 PLAN_OVERVIEW_TEXT 的全部素材）。"""

    model_config = ConfigDict(extra="allow")

    case_id: str
    name: str
    preconditions: str = ""
    steps: list[CaseStep] = Field(default_factory=list)
    expected: str = Field("", description="整条用例的最终预期")
    tags: list[str] = Field(default_factory=list)
    priority: str = ""
    source: str = Field("feishu", description="数据来源标签：feishu / manual / api ...")
    raw_row: dict[str, Any] = Field(default_factory=dict)


# ============== Baseline（首次执行后存到 case memory，复用时回灌） ==============


class BaselineSnippet(BaseModel):
    """单个事件的历史执行片段（previous / current / next 各传一段）。"""

    model_config = ConfigDict(extra="allow")

    seq: int = 0
    capability_id: str = ""
    event_kind: str = ""
    status: str = Field("unknown", description="上次执行状态：pass / fail / skipped / unknown")
    params: dict[str, Any] = Field(default_factory=dict)
    summary: str = Field("", description="上次的执行总结（如『成功点中 一键登录』）")
    executor_used: str = Field("", description="上次实际选用的 executor，如 adb / remote / vlm")
    ai_reasoning: str = Field("", description="上次 AI 的 reasoning（如能找到）")
    elapsed_ms: int = 0


class BaselineContext(BaseModel):
    """喂给 prompt 的"局部历史窗口"。

    用法：当 AI 在规划"当前 case 第 N 步"或重规划单步时，喂三段：
      - previous：上一步执行结果（成败/总结）
      - current ：本步的上次记录（参考，但 AI 可以推翻）
      - next    ：下一步的上次记录（避免规划早了/晚了）

    这只是"经验"，不是脚本；AI 必须根据当前实时上下文做决策。
    """

    model_config = ConfigDict(extra="allow")

    previous: Optional[BaselineSnippet] = None
    current: Optional[BaselineSnippet] = None
    next: Optional[BaselineSnippet] = None
    case_overall_status: str = Field(
        "", description="上次整 case 是否 pass / fail / partial，让 AI 评估 baseline 可信度"
    )
    notes: str = Field("", description="补充提示：如『上次因图标位置变化失败』")


# ============== AI Plan 输出 ==============


class PlanEvent(BaseModel):
    """规划出的单个事件，对应 plugins/capabilities 里的一项。"""

    model_config = ConfigDict(extra="allow")

    seq: int = Field(..., description="本事件在 plan 中的执行顺序（1-based）")
    case_step_index: Optional[int] = Field(
        None, description="挂回哪条用例步骤的 index；前置/收尾步骤可为 null"
    )
    capability_id: str = Field(..., description="必须出现在 RunContext 的可用菜单中")
    event_kind: str = Field("", description="冗余字段（=capability.event_kind），便于 trace 检索")
    params: dict[str, Any] = Field(default_factory=dict)
    needs_vlm: bool = Field(False, description="是否需要在执行阶段附截图给 VLM；由 capability 元数据决定")
    expected_executor: str = Field(
        "", description="AI 选定的首选 executor（来自菜单 implementations[].executor）"
    )
    fallback_executors: list[str] = Field(default_factory=list)
    ai_reasoning: str = Field(..., description="必填：AI 为什么选这条 capability + 这个 executor")
    label: str = Field("", description="审计文案，例：『点击一键登录按钮』")


class PlanResult(BaseModel):
    """整条 case 的规划结果。"""

    model_config = ConfigDict(extra="allow")

    mode: Literal["plan", "decline"] = "plan"
    case_id: str = ""
    ai_reasoning: str = Field(..., description="AI 对整条 case 的总体思考")
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    events: list[PlanEvent] = Field(default_factory=list)
    decline_reason: str = ""
    open_questions: list[str] = Field(default_factory=list, description="AI 想问人的问题（不阻塞）")

    # ---- 加载/校验阶段填充，不写入 prompt ----
    raw_llm: dict[str, Any] = Field(default_factory=dict, description="原始 LLM JSON，便于 trace")
    parse_warnings: list[str] = Field(default_factory=list)


# ============== AI Replan 输出 ==============


class ReplanResult(BaseModel):
    """单步重规划的结果（执行失败 / 与 baseline 偏离时）。"""

    model_config = ConfigDict(extra="allow")

    mode: Literal["replan", "decline", "give_up"] = "replan"
    ai_reasoning: str = Field(..., description="AI 为什么这样改 / 拒绝 / 放弃")
    events: list[PlanEvent] = Field(default_factory=list, description="新的事件队列（覆盖剩余事件）")
    drop_remaining: bool = Field(
        True,
        description="True=丢弃 baseline 中尚未执行的所有事件，按 events 走；False=只插入 events 然后回到原 baseline",
    )
    decline_reason: str = ""
    needs_human: bool = Field(False, description="是否建议触发 HITL 介入")
    raw_llm: dict[str, Any] = Field(default_factory=dict)
    parse_warnings: list[str] = Field(default_factory=list)


# ============== VLM 子流程结果 ==============


class LocateResult(BaseModel):
    """LOCATE_VISION 输出：在截图上定位某个元素。"""

    model_config = ConfigDict(extra="allow")

    found: bool = Field(..., description="是否在截图上找到目标")
    x: int = Field(0, description="目标中心点 x（preview 像素或归一化 0~1000，由 coord_mode 标记）")
    y: int = Field(0, description="目标中心点 y")
    coord_mode: Literal["preview_pixels", "normalized_1000"] = "preview_pixels"
    bbox: list[int] = Field(
        default_factory=list,
        description="可选 bounding box [x1, y1, x2, y2]，找不到时为空",
    )
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    ai_reasoning: str = Field(..., description="VLM 给出的判断依据")
    label_seen: str = Field("", description="VLM 在截图上观察到的文案/特征")
    raw_llm: dict[str, Any] = Field(default_factory=dict)
    parse_warnings: list[str] = Field(default_factory=list)


class HitlComposerResult(BaseModel):
    """HITL_PROMPT_COMPOSER 输出：AI 写出的"问人话术 + 渲染数据"。"""

    model_config = ConfigDict(extra="allow")

    title: str = Field(..., description="弹框标题（≤30 字）")
    body: str = Field(..., description="详细问句正文，可换行")
    options: list[dict[str, Any]] = Field(
        default_factory=list,
        description="单/多选时的选项 [{id, label, hint?}, ...]；其它类型为空数组",
    )
    constraints: dict[str, Any] = Field(
        default_factory=dict,
        description="约束条件：input_text 用 regex/min_len/max_len；upload_image 用 accept_mime/max_size_kb",
    )
    default_timeout_sec: int = Field(300, ge=1, le=3600, description="默认超时（秒）；prompt 建议 60~900")
    ai_reasoning: str = Field(..., description="AI 为什么这么问，便于审计")
    raw_llm: dict[str, Any] = Field(default_factory=dict)
    parse_warnings: list[str] = Field(default_factory=list)


class PersonaExpandResult(BaseModel):
    """PERSONA_TASK 输出：把一个高层系统任务展开为多步拟人化子事件。"""

    model_config = ConfigDict(extra="allow")

    mode: Literal["expand", "decline"] = "expand"
    ai_reasoning: str = Field(..., description="为什么这么展开 / 为什么拒绝")
    sub_events: list[PlanEvent] = Field(
        default_factory=list,
        description="展开后的子事件序列（capability_id 必须出现在菜单中）",
    )
    confidence: float = Field(0.5, ge=0.0, le=1.0)
    decline_reason: str = ""
    needs_human: bool = Field(False, description="是否需要 HITL 介入；展开时无法独立完成时为 true")
    raw_llm: dict[str, Any] = Field(default_factory=dict)
    parse_warnings: list[str] = Field(default_factory=list)


class AssertResult(BaseModel):
    """ASSERT_VISION 输出：判断预期是否成立。"""

    model_config = ConfigDict(extra="allow")

    passed: bool = Field(..., description="预期是否达成")
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    ai_reasoning: str = Field(..., description="判断依据")
    evidence: str = Field("", description="截图中观察到的关键证据描述")
    raw_llm: dict[str, Any] = Field(default_factory=dict)
    parse_warnings: list[str] = Field(default_factory=list)


# ============== Run / Event 执行态 ==============


class EventStatus(str, Enum):
    """事件执行结果的终态分类。"""

    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    BLOCKED = "blocked"  # 等待 HITL / 需要人工介入；orchestrator 应暂停
    DECLINED = "declined"  # AI 主动拒绝（如 persona 展开失败）


class EventResult(BaseModel):
    """单事件执行后的结果，按时间序列推进 trace。"""

    model_config = ConfigDict(extra="allow")

    seq: int
    capability_id: str
    event_kind: str = ""
    status: EventStatus
    executor_used: str = Field("", description="实际跑这事件的 executor id（adb / remote / vlm / hitl / ai_persona）")
    elapsed_ms: int = 0
    summary: str = Field("", description="一句话总结，写入 UI / trace")
    error: str = Field("", description="非 PASS 时的错误原文")

    ai_reasoning: str = Field("", description="原 PlanEvent 的 reasoning，便于一并审计")
    plan_event: dict[str, Any] = Field(default_factory=dict, description="原 PlanEvent 序列化")

    # 视觉子流程附产物
    vlm_meta: dict[str, Any] = Field(default_factory=dict, description="LOCATE/ASSERT 的细节，含坐标/证据")
    screenshot_path: str = Field("", description="若抓了截图，本地路径")
    thumb: str = Field("", description="步骤缩略图 JPEG base64（无 data: 前缀），供时间线展示")

    # 执行器原始返回（保留 debug 用）
    raw_response: dict[str, Any] = Field(default_factory=dict)

    # 时间戳
    started_at: str = ""
    finished_at: str = ""


class RunReport(BaseModel):
    """一次完整 Run 的汇总。"""

    model_config = ConfigDict(extra="allow")

    run_id: str
    case_id: str = ""
    sn: str = ""
    overall_status: Literal["pass", "fail", "partial", "declined", "blocked"] = "pass"

    total_events: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    blocked: int = 0
    declined: int = 0

    replan_count: int = 0
    replans: list[dict[str, Any]] = Field(
        default_factory=list,
        description="每次 replan 的摘要：触发事件、AI 决策、产生的新事件数",
    )

    events: list[EventResult] = Field(default_factory=list)
    final_plan_events: list[PlanEvent] = Field(
        default_factory=list,
        description="最终执行序列（含 replan 插入的部分），用于落 case_memory baseline",
    )

    elapsed_ms: int = 0
    started_at: str = ""
    finished_at: str = ""

    decline_reason: str = ""
    blocked_reason: str = ""


# ============== Agent 执行态（D1–D6 改造：目标导向闭环） ==============


class CaseCheckpoint(BaseModel):
    """用例的一个可观测里程碑（软锚点，用于判进度/成功，不是硬脚本）。"""

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., description="稳定短 id，如 cp1")
    description: str = Field(..., description="可在屏幕上观测的状态，如『进入一键登录页』")
    kind: str = Field(
        "terminal",
        description="process=加载/占位/切换中等过程态，须中途验证；terminal=完成后的稳定屏",
    )
    done: bool = Field(False, description="运行时标记是否已达成")


class CaseGoal(BaseModel):
    """用例 → 目标 + 检查点（D1：替代固定事件序列）。"""

    model_config = ConfigDict(extra="allow")

    case_id: str = ""
    goal: str = Field(..., description="整条用例要达成的总体目标（自然语言）")
    checkpoints: list[CaseCheckpoint] = Field(default_factory=list)
    success_criteria: str = Field("", description="最终成功判定标准，供 VLM 断言")
    ai_reasoning: str = Field("", description="抽取思路")
    raw_llm: dict[str, Any] = Field(default_factory=dict)
    parse_warnings: list[str] = Field(default_factory=list)


class AgentAction(BaseModel):
    """agent 单步要执行的一个能力动作。"""

    model_config = ConfigDict(extra="allow")

    capability_id: str = Field("", description="必须来自 capability_menu")
    params: dict[str, Any] = Field(default_factory=dict, description="含绝对像素坐标等")


class AgentDecision(BaseModel):
    """decide_next_action 的单步决策（D2：看图直接出坐标；D3：每步）。"""

    model_config = ConfigDict(extra="allow")

    thought: str = Field("", description="当前屏幕分析 + 为什么选这一步")
    action: Optional[AgentAction] = Field(None, description="status=continue/ask_human 时必填")
    expected_after: str = Field("", description="执行后预期出现的状态，供下一步自检")
    status: Literal["continue", "done", "give_up", "ask_human"] = "continue"
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    remember: list[str] = Field(default_factory=list, description="本步要写入短期记忆的事实")
    checkpoint_ids: list[str] = Field(default_factory=list, description="本步正在验证/已达成的检查点 id")
    subflow: str = Field("none", description="none | create_publish，创作发布子流程中不占主预算")
    published: dict[str, Any] = Field(default_factory=dict, description="发布成功时的内容指纹")
    raw_llm: dict[str, Any] = Field(default_factory=dict)
    parse_warnings: list[str] = Field(default_factory=list)
