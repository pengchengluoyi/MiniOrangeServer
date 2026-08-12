# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""HITL 数据契约：HitlRequest / HitlReply / HitlPending。

注意：HitlComposerResult 属于 LLM 输出层，放在 ai/regression/schemas.py。
这里只放"会跨进程边界（WS / HTTP）传输"的数据形状。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


VALID_HITL_KINDS = {
    "confirm",
    "input_text",
    "choice_single",
    "choice_multiple",
    "upload_image",
    "acknowledge",
}


class HitlRequest(BaseModel):
    """从 server 推送给桌面端的"请人介入"请求。"""

    model_config = ConfigDict(extra="allow")

    request_id: str = Field(..., description="唯一 ID（短 uuid）")
    sn: Optional[str] = Field(None, description="设备 sn；为 None 表示与设备无关")
    run_id: Optional[str] = Field(None, description="回归 run id")
    case_id: Optional[str] = Field(None, description="飞书用例 ID")
    event_seq: Optional[int] = Field(None, description="对应 PlanEvent.seq")
    capability_id: str = Field(..., description="例如 human_input_text")
    kind: str = Field(..., description=f"交互类型，必须属于 {sorted(VALID_HITL_KINDS)}")

    # 渲染数据（来自 HitlComposerResult）
    title: str
    body: str
    options: list[dict[str, Any]] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)

    # 时序
    created_at: float = Field(default_factory=lambda: time.time())
    timeout_sec: int = Field(300, ge=1, le=3600)
    deadline_at: float = Field(0.0, description="created_at + timeout_sec；用于前端倒计时")

    # 审计
    ai_reasoning: str = ""
    screenshot_path: Optional[str] = None
    composer_warnings: list[str] = Field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """转为 WS / HTTP 推送的 JSON 体（不含敏感内部字段）。"""
        return self.model_dump(mode="json")


class HitlReply(BaseModel):
    """桌面端回传的人工答案。"""

    model_config = ConfigDict(extra="allow")

    request_id: str
    kind: str = Field(..., description="必须与原 HitlRequest.kind 一致")
    answer: Any = Field(
        None,
        description=(
            "因 kind 不同含义不同：\n"
            "  confirm           → bool（True=确认 / False=拒绝）\n"
            "  acknowledge       → True（用户已知悉）\n"
            "  input_text        → str\n"
            "  choice_single     → str（选项 id）\n"
            "  choice_multiple   → list[str]\n"
            "  upload_image      → {'path': '/static/uploads/xxx.png', 'mime': 'image/png'} 或 base64\n"
        ),
    )
    skipped: bool = Field(False, description="True 时表示用户主动跳过，不应视为成功")
    extra: dict[str, Any] = Field(default_factory=dict, description="桌面端附加元数据")
    replied_at: float = Field(default_factory=lambda: time.time())
    replied_by: Optional[str] = Field(None, description="操作员（可空）")


class HitlPending(BaseModel):
    """GET /hitl/pending 列表项 = HitlRequest 的精简快照 + 状态。"""

    model_config = ConfigDict(extra="allow")

    request_id: str
    sn: Optional[str] = None
    run_id: Optional[str] = None
    case_id: Optional[str] = None
    event_seq: Optional[int] = None
    capability_id: str
    kind: str
    title: str
    created_at: float
    deadline_at: float
    waiting_ms: int = 0
