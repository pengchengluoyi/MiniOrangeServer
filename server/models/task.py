# !/usr/bin/env python
# -*-coding:utf-8 -*-
import uuid
from sqlalchemy import Column, String, Integer, DateTime
from datetime import datetime
from server.core.database import Base

class Task(Base):
    __tablename__ = "tasks"
    
    id = Column(String, primary_key=True)
    uid = Column(String, default=lambda: str(uuid.uuid4()), unique=True)
    app_id = Column(String, index=True, nullable=False)
    name = Column(String, nullable=False)
    type = Column(String, nullable=False)
    status = Column(String, default="pending")
    progress = Column(Integer, default=0)
    pass_rate = Column(Integer, default=0)
    completed_count = Column(Integer, default=0)
    total_count = Column(Integer, default=0)
    start_time = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)