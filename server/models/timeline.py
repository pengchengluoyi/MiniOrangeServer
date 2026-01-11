# !/usr/bin/env python
# -*-coding:utf-8 -*-

from sqlalchemy import Column, Integer, String, Text, BigInteger
from server.core.database import Base

class TaskTimeline(Base):
    __tablename__ = "task_timeline"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(64), index=True, comment="任务运行ID")
    timestamp = Column(BigInteger, index=True, comment="事件时间戳")
    event_type = Column(String(32), comment="事件类型: screenshot/click等")
    event_data = Column(Text, comment="事件数据: 图片URL或坐标JSON")
    
    # 可选: 关联 flow_id 或 device_sn