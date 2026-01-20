# server/models/AppGraph/app_structure.py
from sqlalchemy import Column, String, Text, ForeignKey, DateTime, Integer, Float, Boolean, JSON, Table
from sqlalchemy.orm import relationship
import uuid
from sqlalchemy.orm import relationship
from datetime import datetime
from server.core.database import Base
from server.models.AppGraph.app_types import NodeType, TriggerType, SnapshotType, SOPType

# 关联表：SOP 与 Node 的多对多关系
# 用来表示：这个 SOP 逻辑覆盖了哪些页面 (即你提到的"深色背景"圈选的范围)
sop_node_association = Table(
    "sop_node_association",
    Base.metadata,
    Column("sop_id", Integer, ForeignKey("app_sops.id")),
    Column("node_id", Integer, ForeignKey("app_nodes.id")),
)

class AppGraph(Base):
    __tablename__ = "app_graph"
    # 修正：改回 Integer 类型以匹配现有数据，启用自增
    id = Column(Integer, primary_key=True, autoincrement=True)
    # 新增：业务唯一标识 (UUID)，用于导出和防冲突
    uid = Column(String, default=lambda: str(uuid.uuid4()), unique=True)
    name = Column(String, index=True)
    desc = Column(String, nullable=True)
    app_id = Column(String, ForeignKey("apps.id"), index=True)
    # 补充报错日志中出现的字段，防止丢失数据或再次报错
    icon = Column(String, default="📱")
    
    # 1. Graph 级变量: 全局环境配置 (e.g. {"base_url": "https://dev.api.com", "env": "test"})
    variables = Column(JSON, default={})

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    nodes = relationship("AppNode", back_populates="graph", cascade="all, delete-orphan")
    edges = relationship("AppEdge", back_populates="graph", cascade="all, delete-orphan")
    components = relationship("AppComponent", back_populates="graph", cascade="all, delete-orphan")
    sops = relationship("AppSOP", back_populates="graph", cascade="all, delete-orphan")


class AppNode(Base):
    __tablename__ = "app_nodes"
    id = Column(Integer, primary_key=True, index=True)
    graph_id = Column(Integer, ForeignKey("app_graph.id"), index=True)

    node_id = Column(String, index=True)  # Vue Flow ID
    type = Column(String, default=NodeType.PAGE)
    parent_node_id = Column(String, nullable=True)  # 归属关系

    label = Column(String)
    screenshot = Column(String, nullable=True)  # [Legacy] 主图 URL，保留用于缩略图展示

    x = Column(Float, default=0.0)
    y = Column(Float, default=0.0)
    width = Column(Float, nullable=True)
    height = Column(Float, nullable=True)

    # 🔥 新增：标记该节点是否为阻塞节点 (如弹窗、遮罩、广告)
    # 语义：如果匹配到此节点，Driver 应暂停主线，优先寻找"出边"将其关闭
    is_blocking = Column(Boolean, default=False)

    # 🔥 新增：骨架识别配置
    # 存储由多张快照计算出的蒙版数据或忽略区域 (e.g. {"ignore_rects": [[0, 100, 500, 800]], "threshold": 0.8})
    skeleton_config = Column(JSON, default={})

    dom_tree = Column(Text, nullable=True)  # 完整 DOM 结构
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    graph = relationship("AppGraph", back_populates="nodes")
    components = relationship("AppComponent", back_populates="node", cascade="all, delete-orphan")
    snapshots = relationship("AppNodeSnapshot", back_populates="node", cascade="all, delete-orphan")
    sops = relationship("AppSOP", secondary=sop_node_association, back_populates="nodes")


class AppNodeSnapshot(Base):
    """
    页面的多态性存储：
    一个页面(AppNode)可能有多种形态：原始截图、骨架图(Skeleton)、线框图(Wireframe)
    或者不同加载阶段的截图 (Loading态, 只有骨架时, 内容填充后)
    """
    __tablename__ = "app_node_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    node_id = Column(Integer, ForeignKey("app_nodes.id"), index=True)

    type = Column(String, default=SnapshotType.SCREENSHOT) # screenshot, skeleton, etc.
    image_url = Column(String)                             # 图片地址
    meta_data = Column(JSON, default={})                   # 识别算法提取的元数据 (如骨架坐标数据)

    node = relationship("AppNode", back_populates="snapshots")


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


class AppSOP(Base):
    """
    应用 SOP (Standard Operating Procedure) / 说明书
    定义应用的逻辑流，如：启动流程、弹窗逻辑、页面加载逻辑
    """
    __tablename__ = "app_sops"

    id = Column(Integer, primary_key=True, index=True)
    graph_id = Column(Integer, ForeignKey("app_graph.id"), index=True)

    name = Column(String)  # e.g., "冷启动流程", "首页加载SOP"
    type = Column(String, default=SOPType.BUSINESS) # startup, loading, system...
    desc = Column(String, nullable=True)
    
    # 2. SOP 级变量: 业务测试数据集 (e.g. {"accounts": [{"user": "admin", "pwd": "123"}, ...]})
    variables = Column(JSON, default={})

    # 3. 优先级: 当多个 SOP 同时满足触发条件时，优先执行哪一个
    # 场景: "系统异常弹窗"(100) > "业务引导气泡"(50) > "普通业务流程"(0)
    priority = Column(Integer, default=0)

    # 核心逻辑定义
    # 可以存储步骤列表，或者触发条件
    # e.g. { "trigger": "app_launch", "steps": ["show_splash", "wait_3s", "click_skip"] }
    logic_rules = Column(JSON, default={}) 

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    graph = relationship("AppGraph", back_populates="sops")
    workflows = relationship("Workflow", back_populates="sop") # 1 SOP -> N Workflows (Cases)
    nodes = relationship("AppNode", secondary=sop_node_association, back_populates="sops") # SOP 包含的页面集合
