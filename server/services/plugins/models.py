# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Plugin 系统的 pydantic 数据模型。"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class Implementation(BaseModel):
    """Capability 下的一个执行实现路径。"""

    model_config = ConfigDict(extra="allow")

    id: str
    display_name: str = ""
    executor: str
    requires_caps: list[str] = Field(default_factory=list)
    needs_vlm: bool = False
    locate_prompt: Optional[str] = None
    prompt_template: Optional[str] = None
    low_level: dict[str, Any] = Field(default_factory=dict)
    cost: int = 5
    expands_to_events: bool = False
    description: str = ""


class CapabilityUI(BaseModel):
    """Skills 页展示元数据。"""

    shown_in_settings: bool = True
    examples: list[str] = Field(default_factory=list)


class Capability(BaseModel):
    """一个事件类型 = 一个 Capability。"""

    model_config = ConfigDict(extra="allow")

    id: str
    display_name: str
    event_kind: str
    category: str = "uncategorized"
    description: str = ""
    platforms: list[str] = Field(default_factory=list)
    trigger_phrases: list[str] = Field(default_factory=list)
    needs_vlm: bool = False
    implementations: list[Implementation] = Field(default_factory=list)
    ui: CapabilityUI = Field(default_factory=CapabilityUI)

    # 可见域：决定该能力进哪个 prompt 菜单。
    #   case   业务用例决策 agent 可见
    #   system L0 系统层处置 agent 可见
    # 缺省两者都可见（向后兼容：老 yaml 不写这个字段行为不变）。
    visible_to: list[str] = Field(default_factory=lambda: ["case", "system"])
    # 可选：OpenAI tools 的参数表。缺省时 tool_schema.PARAM_DEFAULTS 补。
    params: list[dict[str, Any]] = Field(default_factory=list)

    # ↓ 加载器填充，不写入 yaml
    source_path: str = ""


class Executor(BaseModel):
    """执行器：声明实现了哪些抽象能力。"""

    model_config = ConfigDict(extra="allow")

    id: str
    display_name: str
    description: str = ""
    available_when: str = ""
    provides: list[str] = Field(default_factory=list)
    conditional_provides: list[dict[str, Any]] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    probe: Optional[dict[str, Any]] = None

    source_path: str = ""


class AbstractCap(BaseModel):
    """抽象能力定义（abstract_caps.yaml）。"""

    id: str
    description: str = ""
    note: str = ""


class RecoveryMatch(BaseModel):
    """恢复规则的触发条件（全部为「与」关系；留空即不约束）。"""

    model_config = ConfigDict(extra="allow")

    # 设备事实：键值需与 recovery.collect_evidence() 的输出一致
    # 例：{"awake": "no"} / {"locked": "yes"} / {"target_alive": "no"}
    evidence: dict[str, str] = Field(default_factory=dict)
    # 屏上文案（层级里的 text / content-desc）命中任一即算
    screen_text_any: list[str] = Field(default_factory=list)
    # 顶层窗口所属包名前缀命中任一即算
    top_window_pkg_prefix: list[str] = Field(default_factory=list)


class RecoveryAction(BaseModel):
    """一个处置动作：直接引用已有 capability，不引入新的执行语义。"""

    model_config = ConfigDict(extra="allow")

    capability: str
    params: dict[str, Any] = Field(default_factory=dict)
    # 语义锚点（见 regression/hierarchy.py），比坐标稳
    target: dict[str, str] = Field(default_factory=dict)
    fallback_xy: list[int] = Field(default_factory=list)


class RecoveryForbid(BaseModel):
    """安全护栏：命中即拒绝执行该动作（避免误点「拒绝/清除数据」这类不可逆项）。"""

    text_any: list[str] = Field(default_factory=list)


class RecoveryRule(BaseModel):
    """一条系统层恢复规则（kind=recovery）。

    两种形态共用一个 schema：
      mode=deterministic  命中即按 actions 执行，不问模型（省钱，适合高置信场景）
      mode=advise         不给动作，只把 prompt_snippet 交给 SystemAgent 参考（长尾场景）
    """

    model_config = ConfigDict(extra="allow")

    id: str
    kind: str = "recovery"
    title: str = ""
    enabled: bool = True
    # 归属与维护元数据：谁提供、谁负责（对应 docs/plan-skill-packs-and-console.md §1.5）
    provider: str = "platform"
    owner: str = ""
    lifecycle: str = "active"      # draft | review | active | deprecated
    priority: int = 0              # 多条命中时大者先执行

    when: str = ""                 # 人可读的触发描述
    match: RecoveryMatch = Field(default_factory=RecoveryMatch)

    mode: str = "advise"           # deterministic | advise
    actions: list[RecoveryAction] = Field(default_factory=list)
    verify: RecoveryMatch = Field(default_factory=RecoveryMatch)
    forbid: RecoveryForbid = Field(default_factory=RecoveryForbid)
    prompt_snippet: str = ""
    max_attempts: int = 1

    platforms: list[str] = Field(default_factory=list)
    evidence_notes: list[str] = Field(default_factory=list)

    source_path: str = ""


class PackScope(BaseModel):
    """包的作用域：决定这批条目对谁生效。"""

    model_config = ConfigDict(extra="allow")

    app_ids: list[str] = Field(default_factory=list)     # 空 = 全部应用
    platforms: list[str] = Field(default_factory=list)   # 空 = 不限
    app_versions: str = ""                               # 被测应用版本范围，如 ">=2.0.0 <3.0.0"
    device_models: list[str] = Field(default_factory=list)


class PackReview(BaseModel):
    required: bool = False
    approved_by: str = ""
    approved_at: str = ""


class PackManifest(BaseModel):
    """pack.yaml：一个可整体启停 / 同步 / 评审的单元。

    provider=learned/doc/third_party 时强制 review.required（学习产出必须人工确认）。
    """

    model_config = ConfigDict(extra="allow")

    id: str
    display_name: str = ""
    kind_hint: str = ""
    version: int = 1
    provider: str = "app_qa"
    owner: str = ""
    lifecycle: str = "active"          # draft | review | active | deprecated
    scope: PackScope = Field(default_factory=PackScope)
    review: PackReview = Field(default_factory=PackReview)

    # 加载器填充
    root: str = ""                    # app | team | builtin | learned
    dir_path: str = ""
    app_id: str = ""                  # 仅 app 根：该包属于哪个应用


class LoadError(BaseModel):
    """加载失败的条目，便于前端展示。"""

    path: str
    kind: str  # "capability" | "executor" | "abstract_caps" | "recovery"
    message: str
