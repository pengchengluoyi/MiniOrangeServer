# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""图谱命名对齐别名表：人审过的「脑图里叫 X = 图谱里的 Y」落盘，下次自动命中。"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, JSON, String, UniqueConstraint

from server.core.database import Base


class MAtlasAlias(Base):
    __tablename__ = "m_atlas_alias"
    __table_args__ = (UniqueConstraint("app_id", "alias_norm", name="uq_atlas_alias_app_norm"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    app_id = Column(String(64), index=True, default="")
    alias = Column(String(128), nullable=False)
    alias_norm = Column(String(128), index=True, default="")
    target_id = Column(String(64), index=True, default="")
    target_kind = Column(String(16), default="module")  # module | feature
    target_path = Column(JSON, default=list)
    source = Column(String(16), default="import")  # import | llm | human | case
    review_status = Column(String(16), default="pending", index=True)  # pending | approved | rejected
    hits = Column(Integer, default=0)
    score = Column(Integer, default=0)
    note = Column(String(512), default="")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
