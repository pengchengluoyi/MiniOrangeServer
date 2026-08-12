# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""回归用例的 baseline / run trace 持久化模型（Step 6）。

两张表：
  - m_case_baseline  : 每个 (case_id, device_signature) 当前生效的 baseline；只留 1 份
  - m_case_run_trace : 每次 run 的完整 trace；保留全部历史；可手工 promote 到 baseline

字段全部用 JSON 装载结构化 payload，避免随业务调整频繁 ALTER。
ORM 关系：MCaseBaseline.run_id ↔ MCaseRunTrace.run_id（不建外键约束，跨表只用 id 引用，
方便手工备份 / 删历史 trace 而不破坏 baseline）。
"""
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, JSON, String, UniqueConstraint

from server.core.database import Base


class MCaseRunTrace(Base):
    """单次 case 执行的完整 trace。"""

    __tablename__ = "m_case_run_trace"

    run_id = Column(String(64), primary_key=True, index=True)
    case_id = Column(String(128), index=True, nullable=False)
    device_signature = Column(String(256), index=True, default="")
    sn = Column(String(128), default="")
    platform = Column(String(32), default="android")
    ai_provider_id = Column(String(64), default="")

    overall_status = Column(String(16), default="unknown", index=True)
    total_events = Column(Integer, default=0)
    passed = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    skipped = Column(Integer, default=0)
    blocked = Column(Integer, default=0)
    declined = Column(Integer, default=0)
    replan_count = Column(Integer, default=0)
    elapsed_ms = Column(Integer, default=0)

    # 完整结构化数据
    plan_payload = Column(JSON, default=dict, comment="PlanResult.model_dump")
    report_payload = Column(JSON, default=dict, comment="RunReport.model_dump（不含 events 大体）")
    final_plan_events = Column(JSON, default=list, comment="最终执行序列 [PlanEvent.model_dump]")
    event_results = Column(JSON, default=list, comment="逐条 EventResult.model_dump")
    run_context = Column(JSON, default=dict, comment="RunContext.to_dict 的快照（设备/通道）")

    is_baseline = Column(Boolean, default=False, index=True, comment="是否被 promote 为当前 baseline")

    started_at = Column(DateTime, default=datetime.now)
    finished_at = Column(DateTime, nullable=True)


class MCaseBaseline(Base):
    """每个 (case_id, device_signature) 的当前 baseline 指针。"""

    __tablename__ = "m_case_baseline"
    __table_args__ = (
        UniqueConstraint("case_id", "device_signature", name="uq_case_device_baseline"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String(128), index=True, nullable=False)
    device_signature = Column(String(256), index=True, default="")

    # 指向当前 baseline 对应的 run trace
    baseline_run_id = Column(String(64), nullable=False)
    overall_status = Column(String(16), default="pass")

    # 冗余事件简表，避免每次 plan 都去 join trace
    events_brief = Column(
        JSON,
        default=list,
        comment="精简 events: [{seq, case_step_index, capability_id, event_kind, executor_used, status, summary, params}]",
    )
    ai_reasoning_overview = Column(String(2048), default="", comment="上次 plan 的总体 reasoning")

    blessed_at = Column(DateTime, default=datetime.now)
    blessed_by = Column(String(64), default="auto", comment="auto / 用户名 / system")
    notes = Column(String(512), default="")
