# server/models/AppGraph/app_component.py
from sqlalchemy import Column, Integer, String, Boolean, JSON, ForeignKey, Float
from sqlalchemy.orm import relationship
from server.core.database import Base
from server.models.AppGraph.app_types import ComponentCategory, InputType, ComponentStateType


class AppComponent(Base):
    __tablename__ = "app_components"

    id = Column(Integer, primary_key=True, index=True)
    graph_id = Column(Integer, ForeignKey("app_graph.id"), index=True)
    node_id = Column(Integer, ForeignKey("app_nodes.id"), index=True)  # 必须挂载在某个截图(Node)下

    uid = Column(String, index=True, unique=True)  # 唯一标识
    label = Column(String)
    name = Column(String, nullable=True)

    category = Column(String, default=ComponentCategory.INPUT)
    sub_type = Column(String, default=InputType.TEXT)

    # 🔥 核心资产：规则 (验证/硬件参数/跳转目标)
    rules = Column(JSON, default={})

    # 🔥 核心资产：多维定位 (Android/iOS/Web)
    locators = Column(JSON, default={})

    default_value = Column(String, nullable=True)
    is_disabled = Column(Boolean, default=False)

    # 🔥 相对坐标 (相对于 Node.screenshot 的像素值)
    # 只需要存这 4 个值，不再存小图文件
    x = Column(Float)
    y = Column(Float)
    width = Column(Float)
    height = Column(Float)

    # 🔥 新增：组件骨架识别配置 (e.g. {"mask_url": "...", "images": [...]})
    skeleton_config = Column(JSON, default={})

    graph = relationship("AppGraph", back_populates="components")
    node = relationship("AppNode", back_populates="components")
    states = relationship("AppComponentState", back_populates="component", cascade="all, delete-orphan")


class AppComponentState(Base):
    """
    组件的多态性存储：
    同一个组件(AppComponent)在不同状态下(点击、悬浮、有消息)可能有不同的视觉表现
    """
    __tablename__ = "app_component_states"

    id = Column(Integer, primary_key=True, index=True)
    component_id = Column(Integer, ForeignKey("app_components.id"), index=True)

    state_type = Column(String, default=ComponentStateType.DEFAULT) # 状态类型: hover, pressed, etc.
    
    image_url = Column(String, nullable=True)  # 该状态下的组件截图
    attributes = Column(JSON, default={})      # 该状态下的特定属性 (如: {"color": "#FF0000", "text": "Loading..."})
    
    # 🔥 新增：该状态下的骨架配置 (支持多态骨架)
    skeleton_config = Column(JSON, default={})
    
    description = Column(String, nullable=True) # 描述: "点击后的红色高亮状态"

    component = relationship("AppComponent", back_populates="states")