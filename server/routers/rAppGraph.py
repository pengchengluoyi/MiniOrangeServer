# server/routers/rAppGraph.py
import os
import sys
import json
import uuid
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session
from pydantic import BaseModel

from server.core.database import get_db, APP_DATA_DIR
# 请确保你的模型路径正确
from server.models.AppGraph.app_structure import AppGraph, AppNode, AppEdge, AppSOP
from server.models.AppGraph.app_component import AppComponent
from server.models.AppGraph.app_types import NodeType

router = APIRouter(prefix="/app_graph", tags=["App Graph Engine"])

# 🔥 统一路径逻辑：使用用户数据目录
BASE_DIR = APP_DATA_DIR
UPLOAD_DIR = os.path.join(APP_DATA_DIR, "uploads")


# --- Pydantic Models (🔥 核心修复：宽容模式) ---

class ComponentStateItem(BaseModel):
    state_type: str = "default"
    image_url: Optional[str] = None
    attributes: Optional[Any] = {} # 允许 dict 或 str
    description: Optional[str] = None
    skeleton_config: Optional[Dict] = {} # 🔥 新增

class ComponentItem(BaseModel):
    uid: Optional[str] = None
    label: Optional[str] = "New Component"
    category: Optional[str] = "action"
    sub_type: Optional[str] = "click"
    rules: Optional[Dict] = {}
    locators: Optional[Dict] = {}
    # 允许 rect 为空，提供默认值
    rect: Optional[Dict] = {"x": 0, "y": 0, "w": 0, "h": 0}
    skeleton_config: Optional[Dict] = {} # 🔥 新增
    states: List[ComponentStateItem] = [] # 🔥 明确定义 states 结构


class NodeSaveDetail(BaseModel):
    graph_id: int
    node_id: str
    type: str = NodeType.PAGE
    parent_node_id: Optional[str] = None
    label: str = "新节点"
    screenshot: Optional[str] = None
    # 允许 dom_tree 为 None
    dom_tree: Optional[Any] = None
    components: List[ComponentItem] = []
    is_blocking: bool = False # 🔥 新增
    skeleton_config: Optional[Dict] = {}


class GraphLayoutSave(BaseModel):
    graph_id: int
    nodes: List[Dict]
    edges: List[Dict]


class AppGraphCreate(BaseModel):
    name: str
    desc: Optional[str] = None
    app_id: str
    variables: Optional[Dict] = {}

class SOPCreate(BaseModel):
    graph_id: int
    name: str
    type: str = "business"
    desc: Optional[str] = None
    priority: int = 0
    variables: Optional[Dict] = {}
    node_ids: List[str] = []  # 关联的节点 VueFlow ID 列表
    logic_rules: Optional[Dict] = {} # 🔥 新增：触发规则 (诊断书)
    workflows: Optional[List[str]] = None

class SOPUpdate(BaseModel):
    sop_id: int
    name: Optional[str] = None
    type: Optional[str] = None
    desc: Optional[str] = None
    priority: Optional[int] = None
    variables: Optional[Dict] = None
    node_ids: Optional[List[str]] = None
    logic_rules: Optional[Dict] = None # 🔥 新增
    workflows: Optional[List[str]] = None

class SOPDelete(BaseModel):
    sop_id: int

# --- Routes ---


@router.get("/list")
def get_list(app_id: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(AppGraph)
    if app_id:
        query = query.filter(AppGraph.app_id == app_id)
    return {"code": 200, "data": query.order_by(AppGraph.created_at.desc()).all()}


@router.post("/create")
def create_app(item: AppGraphCreate, db: Session = Depends(get_db)):
    app = AppGraph(
        name=item.name,
        desc=item.desc,
        app_id=item.app_id,
        variables=item.variables
    )
    db.add(app)
    db.commit()
    db.refresh(app)
    return {"code": 200, "msg": "创建成功", "data": app}


@router.get("/detail/{graph_id}")
def get_graph_detail(graph_id: int, db: Session = Depends(get_db)):
    db_nodes = db.query(AppNode).filter(AppNode.graph_id == graph_id).all()
    db_comps = db.query(AppComponent).filter(AppComponent.graph_id == graph_id).all()
    db_sops = db.query(AppSOP).filter(AppSOP.graph_id == graph_id).all()

    comp_map = {}
    for c in db_comps:
        if c.node_id not in comp_map: comp_map[c.node_id] = []
        comp_map[c.node_id].append({
            "id": c.uid,
            "label": c.label,
            "category": c.category,
            "sub_type": c.sub_type,
            "rules": c.rules,
            "locators": c.locators,
            "x": c.x, "y": c.y, "w": c.width, "h": c.height
        })

    nodes_data = []
    for n in db_nodes:
        nodes_data.append({
            "id": n.node_id,
            "type": n.type,
            "parentNode": n.parent_node_id,
            "extent": "parent" if n.parent_node_id else None,
            "position": {"x": n.x, "y": n.y},
            "data": {
                "label": n.label,
                "screenshot": n.screenshot,
                "domTree": json.loads(n.dom_tree) if n.dom_tree else None,
                "interactions": comp_map.get(n.id, [])
            },
            "style": {"zIndex": 100} if n.type != NodeType.PAGE else {}
        })

    db_edges = db.query(AppEdge).filter(AppEdge.graph_id == graph_id).all()
    edges_data = []
    for e in db_edges:
        edges_data.append({
            "id": e.edge_id, "source": e.source, "target": e.target,
            "sourceHandle": e.source_handle, "label": e.label,
            "data": {"trigger": e.trigger}
        })

    sops_data = []
    for s in db_sops:
        sops_data.append({
            "id": s.id,
            "name": s.name,
            "type": s.type,
            "desc": s.desc,
            "priority": s.priority,
            "variables": s.variables,
            "nodes": [n.node_id for n in s.nodes],
            # 🔥 返回核心诊断信息
            "logic_rules": s.logic_rules,
            # 🔥 返回该 SOP 下可用的方案 (Workflows)
            "workflows": [{"id": w.id, "name": w.name} for w in s.workflows]
        })

    return {"code": 200, "data": {"nodes": nodes_data, "edges": edges_data, "sops": sops_data}}


# 🔥 核心修复：增加 Try/Except 捕获，防止 500 崩溃
@router.post("/save_node_detail")
def save_node_detail(item: NodeSaveDetail, db: Session = Depends(get_db)):
    try:
        # 1. 查找或创建 Node
        node = db.query(AppNode).filter(AppNode.graph_id == item.graph_id, AppNode.node_id == item.node_id).first()
        if not node:
            node = AppNode(
                graph_id=item.graph_id,
                node_id=item.node_id,
                type=item.type,
                parent_node_id=item.parentNode
            )
            db.add(node)
            db.flush()  # 获取 ID

        # 2. 更新属性
        node.label = item.label
        node.screenshot = item.screenshot

        # 处理 dom_tree
        if item.dom_tree:
            node.dom_tree = json.dumps(item.dom_tree, ensure_ascii=False)
        else:
            node.dom_tree = None

        # 3. 更新组件
        db.query(AppComponent).filter(AppComponent.node_id == node.id).delete()

        new_comps = []
        for c in item.components:
            uid = c.uid if c.uid else f"c-{uuid.uuid4()}"

            # 安全获取 rect 属性
            r = c.rect if c.rect else {"x": 0, "y": 0, "w": 0, "h": 0}

            new_comps.append(AppComponent(
                graph_id=item.graph_id,
                node_id=node.id,
                uid=uid,
                label=c.label,
                category=c.category,
                sub_type=c.sub_type,
                rules=c.rules,
                locators=c.locators,
                x=r.get('x', 0),
                y=r.get('y', 0),
                width=r.get('w', 0),
                height=r.get('h', 0)
            ))

        if new_comps:
            db.add_all(new_comps)

        db.commit()
        return {"code": 200, "msg": "saved"}

    except Exception as e:
        db.rollback()
        print(f"❌ Save Error: {e}")
        # 返回 500 但带有错误信息，方便前端调试
        return Response(status_code=500, content=f"Server Error: {str(e)}")


# server/routers/rAppGraph.py (局部修改)

@router.post("/sync_layout")
def sync_layout(item: GraphLayoutSave, db: Session = Depends(get_db)):
    try:
        # 1. 获取前端提交的所有节点 ID 列表
        frontend_node_ids = [n['id'] for n in item.nodes]

        # 2. 🔥 核心修复：删除数据库中有，但前端没有提交的节点 (即用户在画布上删除的节点)
        # 注意：这里使用 synchronize_session=False 配合 delete 可能会有级联问题，
        # 建议先查询出对象再删除，或者依赖数据库的外键级联 (Cascade Delete)
        # 这里演示严谨的手动删除逻辑：

        # 找到该图谱下所有不在 frontend_node_ids 里的节点
        nodes_to_delete = db.query(AppNode).filter(
            AppNode.graph_id == item.graph_id,
            AppNode.node_id.notin_(frontend_node_ids)
        ).all()

        for node in nodes_to_delete:
            db.delete(node)  # SQLAlchemy 会自动处理级联删除 components (如果在模型里配了 cascade)

        # 3. 更新剩余节点的坐标
        for n in item.nodes:
            db.query(AppNode).filter(
                AppNode.graph_id == item.graph_id,
                AppNode.node_id == n['id']
            ).update({
                "x": n['position']['x'],
                "y": n['position']['y']
            }, synchronize_session=False)

        # 4. 重建连线 (保持原有逻辑)
        db.query(AppEdge).filter(AppEdge.graph_id == item.graph_id).delete()
        new_edges = []
        for e in item.edges:
            new_edges.append(AppEdge(
                graph_id=item.graph_id, edge_id=e['id'], source=e['source'], target=e['target'],
                source_handle=e.get('sourceHandle'), label=e.get('label'), trigger=e.get('trigger')
            ))
        if new_edges:
            db.add_all(new_edges)

        db.commit()
        return {"code": 200, "msg": "layout synced with deletions"}
    except Exception as e:
        db.rollback()
        print(f"Sync Error: {e}")
        return Response(status_code=500, content=str(e))

# server/routers/rAppGraph.py

# 1. 定义模型
class EmptyNodeCreate(BaseModel):
    graph_id: int
    node_id: str
    type: str
    x: float
    y: float

# 2. 修改接口使用模型接收 Body
@router.post("/add_empty_node")
def add_empty_node(item: EmptyNodeCreate, db: Session = Depends(get_db)):
    # 注意这里使用 item.graph_id 等
    db.add(AppNode(graph_id=item.graph_id, type=item.type, node_id=item.node_id, x=item.x, y=item.y, label="新节点"))
    db.commit()
    return {"code": 200, "msg": "ok"}



@router.get("/component/{comp_uid}/image")
def get_component_image(comp_uid: str, db: Session = Depends(get_db)):
    comp = db.query(AppComponent).filter(AppComponent.uid == comp_uid).first()
    if not comp: return Response(status_code=404)
    node = db.query(AppNode).filter(AppNode.id == comp.node_id).first()
    if not node or not node.screenshot: return Response(status_code=404)

    filename = os.path.basename(node.screenshot)
    image_path = os.path.join(UPLOAD_DIR, filename)

    if not os.path.exists(image_path): return Response(status_code=404)

    try:
        # 🚀 [Perf] 懒加载：只有在需要处理图片时才导入 PIL，启动速度提升 ~3s
        from PIL import Image
        from io import BytesIO
        with Image.open(image_path) as img:
            crop_area = (int(comp.x), int(comp.y), int(comp.x + comp.width), int(comp.y + comp.height))
            cropped = img.crop(crop_area)
            buf = BytesIO()
            cropped.save(buf, format="PNG")
            return Response(content=buf.getvalue(), media_type="image/png")
    except Exception as e:
        return Response(status_code=500, content=str(e))
