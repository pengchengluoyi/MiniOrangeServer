# server/models/AppGraph/app_structure.py
from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey, Float
from sqlalchemy.orm import relationship
from datetime import datetime
from server.core.database import Base
from server.models.AppGraph.app_types import NodeType, TriggerType


class AppGraph(Base):
    __tablename__ = "app_graph"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    desc = Column(String, nullable=True)
    app_id = Column(String, ForeignKey("apps.id"), index=True)
    icon = Column(String, default="📱")
    status = Column(String, default="active")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    nodes = relationship("AppNode", back_populates="graph", cascade="all, delete-orphan")
    edges = relationship("AppEdge", back_populates="graph", cascade="all, delete-orphan")
    components = relationship("AppComponent", back_populates="graph", cascade="all, delete-orphan")


class AppNode(Base):
    __tablename__ = "app_nodes"
    id = Column(Integer, primary_key=True, index=True)
    graph_id = Column(Integer, ForeignKey("app_graph.id"), index=True)

    node_id = Column(String, index=True)  # Vue Flow ID
    type = Column(String, default=NodeType.PAGE)
    parent_node_id = Column(String, nullable=True)  # 归属关系

    label = Column(String)
    screenshot = Column(String, nullable=True)  # 大图 URL

    x = Column(Float, default=0.0)
    y = Column(Float, default=0.0)
    width = Column(Float, nullable=True)
    height = Column(Float, nullable=True)

    dom_tree = Column(Text, nullable=True)  # 完整 DOM 结构
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # 🔥🔥 修复点在这里：之前是 "Appgraph"，改为 "AppGraph" (大写G)
    graph = relationship("AppGraph", back_populates="nodes")
    components = relationship("AppComponent", back_populates="node", cascade="all, delete-orphan")


class AppEdge(Base):
    __tablename__ = "app_edges"
    id = Column(Integer, primary_key=True, index=True)
    graph_id = Column(Integer, ForeignKey("app_graph.id"), index=True)

    edge_id = Column(String)
    source = Column(String)
    target = Column(String)
    source_handle = Column(String, nullable=True)  # 对应 Component UID

    trigger = Column(String, default=TriggerType.CLICK)
    label = Column(String, nullable=True)

    # 这里的引用是对的，不需要改，确认一下即可
    graph = relationship("AppGraph", back_populates="edges")
