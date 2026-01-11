from sqlalchemy import Column, String, Boolean, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from server.core.database import Base
import uuid
from datetime import datetime


class ScheduledTask(Base):
    __tablename__ = "scheduled_tasks"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=True)  # Task Name for display
    app_id = Column(String, nullable=True)  # Group by Application
    cron_expression = Column(String, nullable=False)  # e.g., "*/5 * * * *"
    flow_id = Column(String, nullable=False)  # Target Workflow ID
    target_sn = Column(String, nullable=True)  # Target Device SN
    is_active = Column(Boolean, default=True)  # Enable/Disable
    skip_nodes = Column(Text, nullable=True)  # JSON list of node IDs to skip

    last_run_time = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class ScheduledTaskHistory(Base):
    __tablename__ = "scheduled_task_history"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    schedule_id = Column(String, ForeignKey("scheduled_tasks.id"), nullable=False)
    run_id = Column(String, nullable=False)
    status = Column(String, default="pending")  # pending, dispatched, failed
    details = Column(Text, nullable=True)  # Error message or success info
    created_at = Column(DateTime, default=datetime.now)