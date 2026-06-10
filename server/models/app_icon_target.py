# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""应用无字图标目标（独立表，支持大量条目与截图模板）。"""
import uuid
from datetime import datetime

from sqlalchemy import Column, String, Integer, Text, JSON, DateTime, ForeignKey

from server.core.database import Base


class AppIconTarget(Base):
    __tablename__ = "app_icon_targets"

    id = Column(String(32), primary_key=True, default=lambda: uuid.uuid4().hex[:16])
    app_id = Column(String, ForeignKey("apps.id"), index=True, nullable=False)
    name = Column(String(128), nullable=False, index=True)
    aliases = Column(JSON, default=list)
    x = Column(Integer, default=0)
    y = Column(Integer, default=0)
    w = Column(Integer, default=0)
    h = Column(Integer, default=0)
    image_url = Column(String(512), default="")
    clip_embedding = Column(JSON, nullable=True)
    clip_model = Column(String(128), default="")
    region_hint = Column(String(32), default="")
    graph_id = Column(Integer, nullable=True)
    component_uid = Column(String(64), nullable=True)
    page_node_id = Column(String(64), nullable=True)
    note = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
