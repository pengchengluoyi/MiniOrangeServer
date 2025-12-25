# !/usr/bin/env python
# -*-coding:utf-8 -*-
import uuid
from sqlalchemy import Column, String, Text, JSON, ForeignKey
from sqlalchemy.orm import relationship
from server.core.database import Base

class Project(Base):
    __tablename__ = "projects"

    id = Column(String, primary_key=True)
    uid = Column(String, default=lambda: str(uuid.uuid4()), unique=True)
    name = Column(String, nullable=False)
    description = Column(Text)

    apps = relationship("App", back_populates="project")

class App(Base):
    __tablename__ = "apps"

    id = Column(String, primary_key=True)
    uid = Column(String, default=lambda: str(uuid.uuid4()), unique=True)
    name = Column(String, nullable=False)
    description = Column(Text)
    platforms = Column(String)
    env = Column(JSON, default={})

    project_id = Column(String, ForeignKey("projects.id"))
    project = relationship("Project", back_populates="apps")