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


class LoadError(BaseModel):
    """加载失败的条目，便于前端展示。"""

    path: str
    kind: str  # "capability" | "executor" | "abstract_caps"
    message: str
