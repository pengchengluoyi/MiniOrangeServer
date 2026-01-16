# !/usr/bin/env python
# -*-coding:utf-8 -*-

import json
import uuid
import difflib
from fastapi.encoders import jsonable_encoder
from server.core.database import SessionLocal
from server.models.AppGraph.app_structure import AppGraph, AppNode, AppEdge, AppSOP
from server.models.AppGraph.app_component import AppComponent
from server.models.AppGraph.app_types import NodeType
from server.models.workflow import Workflow
# 复用 HTTP 路由中的 Pydantic 模型，确保参数一致性
from server.routers.rAppGraph import AppGraphCreate, NodeSaveDetail, GraphLayoutSave, EmptyNodeCreate, SOPCreate, SOPUpdate, SOPDelete
from script.log import SLog


async def handle_app_graph_list(websocket, data: dict):
    session = SessionLocal()
    try:
        app_id = data.get("app_id")
        query = session.query(AppGraph)
        if app_id:
            query = query.filter(AppGraph.app_id == app_id)
        result = query.order_by(AppGraph.created_at.desc()).all()
        return {"code": 200, "data": jsonable_encoder(result)}
    except Exception as e:
        SLog.e("WAppGraph", f"List error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_app_graph_create(websocket, data: dict):
    session = SessionLocal()
    try:
        item = AppGraphCreate(**data)
        app = AppGraph(
            name=item.name,
            desc=item.desc,
            app_id=item.app_id,
            variables=item.variables
        )
        session.add(app)
        session.commit()
        session.refresh(app)
        return {"code": 200, "msg": "创建成功", "data": jsonable_encoder(app)}
    except Exception as e:
        session.rollback()
        SLog.e("WAppGraph", f"Create error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_app_graph_detail(websocket, data: dict):
    session = SessionLocal()
    try:
        graph_id = data.get("graph_id")
        if not graph_id:
            return {"code": 400, "msg": "Missing graph_id"}
        
        graph_id = int(graph_id)
        db_nodes = session.query(AppNode).filter(AppNode.graph_id == graph_id).all()
        db_comps = session.query(AppComponent).filter(AppComponent.graph_id == graph_id).all()
        db_sops = session.query(AppSOP).filter(AppSOP.graph_id == graph_id).all()

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
            dom_json = json.loads(n.dom_tree) if n.dom_tree else {}
            natural_size = dom_json.get('naturalSize')

            nodes_data.append({
                "id": n.node_id,
                "type": n.type,
                "parentNode": n.parent_node_id,
                "extent": "parent" if n.parent_node_id else None,
                "position": {"x": n.x, "y": n.y},
                "data": {
                    "label": n.label,
                    "screenshot": n.screenshot,
                    "domTree": dom_json,
                    "naturalSize": natural_size,
                    "isBlocking": n.is_blocking, # 🔥 返回给前端，用于可视化 (比如画个红框)
                    "interactions": comp_map.get(n.id, [])
                },
                "style": {"zIndex": 100} if n.type != NodeType.PAGE else {}
            })

        db_edges = session.query(AppEdge).filter(AppEdge.graph_id == graph_id).all()
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
                "logic_rules": s.logic_rules,
                "workflows": [{"id": w.id, "name": w.name} for w in s.workflows]
            })

        return {"code": 200, "data": {"nodes": nodes_data, "edges": edges_data, "sops": sops_data}}
    except Exception as e:
        SLog.e("WAppGraph", f"Detail error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_save_node_detail(websocket, data: dict):
    session = SessionLocal()
    try:
        # 兼容处理：前端可能传 parentNode，但模型定义是 parent_node_id
        if 'parentNode' in data and 'parent_node_id' not in data:
            data['parent_node_id'] = data['parentNode']
            
        item = NodeSaveDetail(**data)
        
        # 1. 查找或创建 Node
        node = session.query(AppNode).filter(AppNode.graph_id == item.graph_id, AppNode.node_id == item.node_id).first()
        if not node:
            node = AppNode(
                graph_id=item.graph_id,
                node_id=item.node_id,
                type=item.type,
                parent_node_id=item.parent_node_id
            )
            session.add(node)
            session.flush()

        # 2. 更新属性
        node.label = item.label
        node.screenshot = item.screenshot
        node.is_blocking = item.is_blocking # 🔥 保存阻塞属性
        
        # 🔥 修复：持久化存储 naturalSize (存入 dom_tree 字段)
        current_dom = {}
        if node.dom_tree:
            try:
                current_dom = json.loads(node.dom_tree)
            except:
                current_dom = {}
        if 'naturalSize' in data:
            current_dom['naturalSize'] = data['naturalSize']
        
        # 如果前端传了新的 dom_tree，则合并更新
        if item.dom_tree:
            if isinstance(item.dom_tree, dict):
                current_dom.update(item.dom_tree)
        
        node.dom_tree = json.dumps(current_dom, ensure_ascii=False)

        # 3. 更新组件
        session.query(AppComponent).filter(AppComponent.node_id == node.id).delete()
        new_comps = []
        for c in item.components:
            uid = c.uid if c.uid else f"c-{uuid.uuid4()}"
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
                x=r.get('x', 0), y=r.get('y', 0), width=r.get('w', 0), height=r.get('h', 0)
            ))
        if new_comps:
            session.add_all(new_comps)
        session.commit()
        return {"code": 200, "msg": "saved"}
    except Exception as e:
        session.rollback()
        SLog.e("WAppGraph", f"Save node error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_sync_layout(websocket, data: dict):
    session = SessionLocal()
    try:
        item = GraphLayoutSave(**data)
        frontend_node_ids = [n['id'] for n in item.nodes]
        
        # 删除未提交的节点
        nodes_to_delete = session.query(AppNode).filter(
            AppNode.graph_id == item.graph_id,
            AppNode.node_id.notin_(frontend_node_ids)
        ).all()
        for node in nodes_to_delete:
            session.delete(node)
            
        # 更新坐标
        for n in item.nodes:
            session.query(AppNode).filter(
                AppNode.graph_id == item.graph_id,
                AppNode.node_id == n['id']
            ).update({
                "x": n['position']['x'],
                "y": n['position']['y']
            }, synchronize_session=False)
            
        # 重建连线
        session.query(AppEdge).filter(AppEdge.graph_id == item.graph_id).delete()
        new_edges = []
        for e in item.edges:
            new_edges.append(AppEdge(
                graph_id=item.graph_id, edge_id=e['id'], source=e['source'], target=e['target'],
                source_handle=e.get('sourceHandle'), label=e.get('label'), trigger=e.get('trigger')
            ))
        if new_edges:
            session.add_all(new_edges)
        session.commit()
        return {"code": 200, "msg": "layout synced with deletions"}
    except Exception as e:
        session.rollback()
        SLog.e("WAppGraph", f"Sync layout error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_add_empty_node(websocket, data: dict):
    session = SessionLocal()
    try:
        item = EmptyNodeCreate(**data)
        session.add(AppNode(graph_id=item.graph_id, type=item.type, node_id=item.node_id, x=item.x, y=item.y, label="新节点"))
        session.commit()
        return {"code": 200, "msg": "ok"}
    except Exception as e:
        session.rollback()
        SLog.e("WAppGraph", f"Add empty node error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_sop_create(websocket, data: dict):
    session = SessionLocal()
    try:
        item = SOPCreate(**data)
        
        # 查找关联的 Nodes (通过 VueFlow ID)
        nodes = []
        if item.node_ids:
            nodes = session.query(AppNode).filter(
                AppNode.graph_id == item.graph_id,
                AppNode.node_id.in_(item.node_ids)
            ).all()

        sop = AppSOP(
            graph_id=item.graph_id,
            name=item.name,
            type=item.type,
            desc=item.desc,
            priority=item.priority,
            variables=item.variables,
            nodes=nodes,
            logic_rules=item.logic_rules
        )
        session.add(sop)
        session.commit()
        session.refresh(sop)
        
        # 构造返回数据
        res_data = jsonable_encoder(sop)
        res_data['nodes'] = [n.node_id for n in sop.nodes]
        return {"code": 200, "msg": "SOP created", "data": res_data}
    except Exception as e:
        session.rollback()
        SLog.e("WAppGraph", f"SOP create error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_sop_update(websocket, data: dict):
    session = SessionLocal()
    try:
        item = SOPUpdate(**data)
        sop = session.query(AppSOP).filter(AppSOP.id == item.sop_id).first()
        if not sop:
            return {"code": 404, "msg": "SOP not found"}

        if item.name is not None: sop.name = item.name
        if item.type is not None: sop.type = item.type
        if item.desc is not None: sop.desc = item.desc
        if item.priority is not None: sop.priority = item.priority
        if item.variables is not None: sop.variables = item.variables
        if item.logic_rules is not None: sop.logic_rules = item.logic_rules
        
        if item.workflows is not None:
            if item.workflows:
                sop.workflows = session.query(Workflow).filter(Workflow.id.in_(item.workflows)).all()
            else:
                sop.workflows = []
        
        if item.node_ids is not None:
            nodes = session.query(AppNode).filter(
                AppNode.graph_id == sop.graph_id,
                AppNode.node_id.in_(item.node_ids)
            ).all()
            sop.nodes = nodes

        session.commit()
        session.refresh(sop)
        
        res_data = jsonable_encoder(sop)
        res_data['nodes'] = [n.node_id for n in sop.nodes]
        return {"code": 200, "msg": "SOP updated", "data": res_data}
    except Exception as e:
        session.rollback()
        SLog.e("WAppGraph", f"SOP update error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_sop_delete(websocket, data: dict):
    session = SessionLocal()
    try:
        item = SOPDelete(**data)
        sop = session.query(AppSOP).filter(AppSOP.id == item.sop_id).first()
        if sop:
            session.delete(sop)
            session.commit()
        return {"code": 200, "msg": "SOP deleted"}
    except Exception as e:
        session.rollback()
        SLog.e("WAppGraph", f"SOP delete error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_match_solution(websocket, data: dict):
    """
    🔥 核心算法：基于群体智慧的解决方案推荐
    输入：当前页面的 DOM 结构 / 文本特征
    输出：推荐的操作 (Action)
    """
    session = SessionLocal()
    try:
        graph_id = data.get("graph_id")
        current_texts = set(data.get("texts", [])) # 当前屏幕上的关键文本集合
        
        if not current_texts:
            return {"code": 400, "msg": "No text features provided"}

        # 1. 获取该图谱下的所有节点 (作为记忆库)
        # 优化：生产环境应使用向量数据库 (Vector DB) 或倒排索引，这里使用内存遍历演示逻辑
        all_nodes = session.query(AppNode).filter(AppNode.graph_id == graph_id).all()
        
        best_node = None
        max_score = 0.0
        is_blocking_match = False

        # 2. 计算相似度 (Similarity Calculation)
        # 这里使用简单的 Jaccard 相似度：交集 / 并集
        for node in all_nodes:
            if not node.dom_tree: continue
            
            try:
                node_dom = json.loads(node.dom_tree)
                # 提取节点存储的文本特征 (假设 dom_tree 里存了或者实时解析)
                # 简化处理：这里假设 node.label 或 dom_tree 里的某些字段代表了特征
                # 实际项目中，建议在 AppNode 表增加 feature_vector 字段
                node_texts = set() 
                # 简单模拟：从 label 和 dom 提取一些文本
                node_texts.add(node.label)
                
                # 计算 Jaccard 相似度
                intersection = current_texts.intersection(node_texts)
                union = current_texts.union(node_texts)
                
                if not union: continue
                score = len(intersection) / len(union)
                
                # 阈值过滤 (比如相似度 > 0.3 才认为是同一个界面)
                if score > 0.3 and score > max_score:
                    max_score = score
                    best_node = node
                    # 🔥 如果匹配到了阻塞节点，这本身就是一个极强的信号
                    if node.is_blocking:
                        is_blocking_match = True
                        # 阻塞节点的匹配通常不需要太高的文本相似度 (因为弹窗字少)，可以适当加权
                        max_score += 0.2 
            except:
                continue

        if not best_node:
            return {"code": 404, "msg": "No matching state found in knowledge base"}

        # 3. 预测下一步 (Predict Next Step)
        # 找到这个相似节点的所有“出边” (即历史上在这个界面执行过的动作)
        edges = session.query(AppEdge).filter(AppEdge.source == best_node.node_id).all()
        
        recommendations = []
        for edge in edges:
            # 找到触发这个边的组件
            comp = session.query(AppComponent).filter(AppComponent.uid == edge.source_handle).first()
            if comp:
                # 🔥 数据闭环：计算权重 (Wealth of WorkflowRun)
                # 这里模拟从 WorkflowRun 统计出的成功率
                # 实际逻辑：query(WorkflowRun).filter(step_node_id == best_node.id, action_comp_id == comp.uid).count()
                # 假设我们有一个字段 usage_count 存储在 Edge 或 Component 上
                historical_weight = 1.0 
                
                # 如果是阻塞节点，任何"出边"（通常是关闭/确认）的优先级都极高
                priority_boost = 2.0 if is_blocking_match else 1.0
                
                final_score = max_score * historical_weight * priority_boost

                recommendations.append({
                    "score": final_score, 
                    "action": "click",
                    "type": "blocking_resolution" if is_blocking_match else "navigation", # 告诉 Driver 这是在"排雷"还是"赶路"
                    "component": jsonable_encoder(comp),
                    "target_node_id": edge.target # 这个动作通向哪里
                })

        # 按分数排序返回 (高分在前)
        recommendations.sort(key=lambda x: x["score"], reverse=True)
        return {"code": 200, "data": recommendations}

    except Exception as e:
        SLog.e("WAppGraph", f"Match solution error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()