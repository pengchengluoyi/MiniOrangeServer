# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""应用回归执行记录（飞书等）。"""
from datetime import datetime

from sqlalchemy import Column, String, Text, JSON, DateTime, ForeignKey, Float

from server.core.database import Base


class AppRegressionRun(Base):
    __tablename__ = "app_regression_runs"

    run_id = Column(String(32), primary_key=True, index=True)
    app_id = Column(String, ForeignKey("apps.id"), index=True, nullable=False)
    run_type = Column(String(32), default="feishu", index=True)
    sn = Column(String(128), default="")
    platform = Column(String(32), default="android")
    status = Column(String(32), default="running")
    total = Column(Float, default=0)
    passed = Column(Float, default=0)
    failed = Column(Float, default=0)
    payload = Column(JSON, default=dict)
    started_at = Column(DateTime, default=datetime.now)
    finished_at = Column(DateTime, nullable=True)
