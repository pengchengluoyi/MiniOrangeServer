# !/usr/bin/env python
# -*-coding:utf-8 -*-

import json
import cv2
import os
import uuid
import difflib
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import joinedload
from server.core.database import SessionLocal, APP_DATA_DIR
from server.models.AppGraph.app_structure import AppGraph, AppNode, AppEdge, AppSOP
from server.models.AppGraph.app_component import AppComponent, AppComponentState
from server.models.AppGraph.app_types import NodeType
from server.models.workflow import Workflow
# 复用 HTTP 路由中的 Pydantic 模型，确保参数一致性
from server.routers.rAppGraph import AppGraphCreate, NodeSaveDetail, GraphLayoutSave, EmptyNodeCreate, SOPCreate, SOPUpdate, SOPDelete
from server.core.vision.skeleton_algo import SkeletonAlgo
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
        graph = session.query(AppGraph).filter(AppGraph.id == graph_id).first()
        if not graph:
            return {"code": 404, "msg": "Graph not found"}

        db_nodes = session.query(AppNode).filter(AppNode.graph_id == graph_id).all()
        # 使用 joinedload 预加载 states，避免 N+1 查询
        db_comps = session.query(AppComponent).filter(AppComponent.graph_id == graph_id).options(
            joinedload(AppComponent.states)).all()
        db_sops = session.query(AppSOP).filter(AppSOP.graph_id == graph_id).all()

        comp_map = {}
        for c in db_comps:
            if c.node_id not in comp_map: comp_map[c.node_id] = []
            
            # 序列化组件状态 (多态)
            states_data = []
            for s in c.states:
                states_data.append({
                    "state_type": s.state_type,
                    "image_url": s.image_url,
                    "attributes": s.attributes,
                    "description": s.description,
                    "skeleton_config": s.skeleton_config # 🔥 返回状态骨架配置
                })

            rules = c.rules if isinstance(c.rules, dict) else {}
            comp_map[c.node_id].append({
                "id": c.uid,
                "label": c.label,
                "category": c.category,
                "sub_type": c.sub_type,
                "rules": rules,
                "locators": c.locators,
                "component_type": rules.get("component_type", "custom"),
                "shared_region": rules.get("shared_region", ""),
                "needs_confirmation": rules.get("needs_confirmation", False),
                "action": rules.get("action", "click"),
                "skeleton_config": c.skeleton_config, # 🔥 返回组件骨架配置
                "x": c.x, "y": c.y, "w": c.width, "h": c.height,
                "states": states_data  # 🔥 返回多态数据
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
                    "skeleton_config": n.skeleton_config, # 🔥 返回骨架配置
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

        return {
            "code": 200,
            "data": {
                "graph_id": graph.id,
                "app_id": graph.app_id,
                "variables": graph.variables or {},
                "nodes": nodes_data,
                "edges": edges_data,
                "sops": sops_data,
            },
        }
    except Exception as e:
        SLog.e("WAppGraph", f"Detail error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_app_graph_update(websocket, data: dict):
    session = SessionLocal()
    try:
        graph_id = data.get("graph_id")
        if not graph_id:
            return {"code": 400, "msg": "Missing graph_id"}
        graph = session.query(AppGraph).filter(AppGraph.id == int(graph_id)).first()
        if not graph:
            return {"code": 404, "msg": "Graph not found"}
        if "variables" in data and data["variables"] is not None:
            graph.variables = data["variables"]
        if data.get("name") is not None:
            graph.name = data["name"]
        if data.get("desc") is not None:
            graph.desc = data["desc"]
        session.commit()
        return {"code": 200, "msg": "Graph updated", "data": {"variables": graph.variables or {}}}
    except Exception as e:
        session.rollback()
        SLog.e("WAppGraph", f"Graph update error: {e}")
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
        node.skeleton_config = item.skeleton_config # 🔥 保存骨架配置
        
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
        # 优化：先查询再删除，确保触发 ORM 的级联删除 (cascade) 清理关联的 states
        existing_comps = session.query(AppComponent).filter(AppComponent.node_id == node.id).all()
        for ec in existing_comps:
            session.delete(ec)
        session.flush()
            
        new_comps = []
        raw_components = data.get("components", [])
        
        for i, c in enumerate(item.components):
            uid = c.uid if c.uid else f"c-{uuid.uuid4()}"
            r = c.rect if c.rect else {"x": 0, "y": 0, "w": 0, "h": 0}
            rules = dict(c.rules or {}) if isinstance(c.rules, dict) else {}
            if i < len(raw_components) and isinstance(raw_components[i], dict):
                raw = raw_components[i]
                for key in ("component_type", "shared_region", "needs_confirmation", "action"):
                    if raw.get(key) is not None:
                        rules[key] = raw[key]
            
            comp = AppComponent(
                graph_id=item.graph_id,
                node_id=node.id,
                uid=uid,
                label=c.label,
                category=c.category,
                sub_type=c.sub_type,
                rules=rules,
                locators=c.locators,
                x=r.get('x', 0), y=r.get('y', 0), width=r.get('w', 0), height=r.get('h', 0),
                skeleton_config=c.skeleton_config # 🔥 保存组件骨架配置
            )
            
            # 🔥 保存组件多态 (States)
            # 增强鲁棒性：如果 Pydantic 模型 ComponentItem 缺少 states 字段，尝试从 raw data 获取
            states_data = []
            if hasattr(c, 'states') and c.states:
                states_data = c.states
            elif i < len(raw_components) and isinstance(raw_components[i], dict):
                states_data = raw_components[i].get('states', [])

            for s in states_data:
                # 统一处理对象或字典
                if isinstance(s, dict):
                    s_type = s.get('state_type')
                    s_img = s.get('image_url')
                    s_attr = s.get('attributes')
                    s_desc = s.get('description')
                    s_skeleton = s.get('skeleton_config', {})
                else:
                    s_type = s.state_type
                    s_img = s.image_url
                    s_attr = s.attributes
                    s_desc = s.description
                    s_skeleton = s.skeleton_config
                
                # 处理 attributes 可能是 JSON 字符串的情况
                if isinstance(s_attr, str):
                    try:
                        s_attr = json.loads(s_attr)
                    except:
                        pass # 解析失败则保持原样或设为 {}

                comp.states.append(AppComponentState(
                    state_type=s_type,
                    image_url=s_img,
                    attributes=s_attr,
                    description=s_desc,
                    skeleton_config=s_skeleton # 🔥 保存状态骨架配置
                ))
            
            new_comps.append(comp)
            
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
            update_values = {
                "x": n['position']['x'],
                "y": n['position']['y']
            }
            
            # 🔥 同时也更新 data 中的关键配置，防止 sync_layout 覆盖或丢失数据
            if 'data' in n:
                if 'is_blocking' in n['data']:
                    update_values['is_blocking'] = n['data']['is_blocking']
                if 'skeleton_config' in n['data']:
                    update_values['skeleton_config'] = n['data']['skeleton_config']

            session.query(AppNode).filter(
                AppNode.graph_id == item.graph_id,
                AppNode.node_id == n['id']
            ).update(update_values, synchronize_session=False)
            
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


async def handle_get_component_states(websocket, data: dict):
    """
    获取组件的所有多态截图 (热区内的小截图)
    Payload: {"component_id": "c-xxxx"}
    """
    session = SessionLocal()
    try:
        component_id = data.get("component_id")
        if not component_id:
            return {"code": 400, "msg": "Missing component_id"}

        comp = session.query(AppComponent).filter(AppComponent.uid == component_id).options(
            joinedload(AppComponent.states)
        ).first()

        if not comp:
            return {"code": 404, "msg": "Component not found"}

        states_data = []
        for s in comp.states:
            if s.image_url:
                states_data.append({
                    "state_type": s.state_type,
                    "image_url": s.image_url
                })

        return {"code": 200, "data": states_data}

    except Exception as e:
        SLog.e("WAppGraph", f"Get component states error: {e}")
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


async def handle_train_skeleton(websocket, data: dict):
    """
    触发骨骼识别
    Payload: {
        "image_names": ["1.png", "2.png"], 
        "threshold": 10,
        "graph_id": 1,      # 可选：提供ID则自动保存和合并旧图片
        "node_id": "node-1", # 可选
        "component_id": "c-1", # 可选：提供组件ID则针对组件训练
        "state_type": "hover" # 可选：如果提供了组件ID，还可以指定针对哪个状态训练
    }
    """
    session = SessionLocal()
    try:
        image_names = data.get("image_names", [])
        threshold = data.get("threshold", 10)
        graph_id = data.get("graph_id")
        node_id = data.get("node_id")
        component_id = data.get("component_id")
        state_type = data.get("state_type")
        
        all_images = image_names
        node = None
        comp = None
        target_state = None

        # 1. 如果提供了节点信息，尝试从数据库获取历史图片并合并
        if component_id:
            comp = session.query(AppComponent).filter(AppComponent.uid == component_id).first()
            if comp:
                # 如果指定了 state_type，则针对特定状态训练
                if state_type:
                    target_state = session.query(AppComponentState).filter(
                        AppComponentState.component_id == comp.id,
                        AppComponentState.state_type == state_type
                    ).first()
                    if target_state:
                        config = target_state.skeleton_config or {}
                    else:
                        # 状态不存在，可能需要前端先保存状态，或者这里暂不处理历史图片
                        config = {}
                else:
                    # 针对组件本身 (默认状态)
                    config = comp.skeleton_config or {}

                existing_images = config.get("images", [])
                
                for img in image_names:
                    if img not in existing_images:
                        existing_images.append(img)
                
                all_images = existing_images
        elif graph_id and node_id:
            node = session.query(AppNode).filter(
                AppNode.graph_id == graph_id,
                AppNode.node_id == node_id
            ).first()
            
            if node:
                config = node.skeleton_config or {}
                existing_images = config.get("images", [])
                
                # 合并新旧图片 (保持顺序，去重)
                for img in image_names:
                    if img not in existing_images:
                        existing_images.append(img)
                
                all_images = existing_images

        mask, err, system_bars = SkeletonAlgo.train_skeleton(all_images, threshold)
        if err:
            return {"code": 500, "msg": err}
        
        # 保存 Mask 图片
        filename = f"skeleton_{uuid.uuid4().hex}.png"
        upload_dir = os.path.join(APP_DATA_DIR, "uploads")
        if not os.path.exists(upload_dir):
            os.makedirs(upload_dir)
            
        save_path = os.path.join(upload_dir, filename)
        cv2.imwrite(save_path, mask)
        
        mask_url = f"/static/{filename}"
        master_path = all_images[0] if all_images else None

        def _persist_skeleton_config(config):
            config = dict(config or {})
            config["mask_url"] = mask_url
            config["filename"] = filename
            config["images"] = all_images
            if master_path:
                config["master_path"] = master_path
            if system_bars:
                config["system_bars"] = system_bars
            return config

        # 2. 持久化保存到节点配置中
        if target_state:
            # 保存到组件状态
            target_state.skeleton_config = _persist_skeleton_config(target_state.skeleton_config)
            session.commit()
        elif comp:
            # 保存到组件
            comp.skeleton_config = _persist_skeleton_config(comp.skeleton_config)
            session.commit()
        elif node:
            node.skeleton_config = _persist_skeleton_config(node.skeleton_config)
            if master_path:
                node.screenshot = f"/static/{master_path}"
            session.commit()

        from server.core.vision.component_detector import ComponentDetector

        natural_w = natural_h = None
        if node and node.dom_tree:
            try:
                dom_json = json.loads(node.dom_tree)
                ns = dom_json.get("naturalSize") or {}
                natural_w = ns.get("w")
                natural_h = ns.get("h")
            except Exception:
                pass
        if mask is not None:
            mh, mw = mask.shape[:2]
            if not natural_w or not natural_h:
                natural_w, natural_h = mw, mh

        screenshot_path = master_path or (all_images[0] if all_images else None)

        shared_components: list = []
        if graph_id and node:
            graph = session.query(AppGraph).filter(AppGraph.id == int(graph_id)).first()
            if graph and graph.variables:
                shared_components = (graph.variables or {}).get("shared_components") or []

        detected_components = ComponentDetector.detect_for_page(
            mask_path=mask_url,
            screenshot_path=screenshot_path,
            img_w=natural_w,
            img_h=natural_h,
            system_bars=system_bars,
            mask=mask,
            node_id=node.node_id if node else None,
            shared_components=shared_components,
        )

        return {
            "code": 200, 
            "msg": "Skeleton generated", 
            "data": {
                "filename": filename,
                "url": mask_url,
                "images": all_images,
                "system_bars": system_bars,
                "master_path": master_path,
                "detected_components": detected_components,
                "hotspots": detected_components,
            }
        }
    except Exception as e:
        session.rollback()
        SLog.e("WAppGraph", f"Train skeleton error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_crawl_save_page(websocket, data: dict):
    from server.services import crawl_persistence as cp

    try:
        nid = cp.ensure_page_node(
            int(data["graph_id"]),
            data["node_id"],
            data.get("label") or "新页面",
            screenshot=data.get("screenshot"),
            natural_size=data.get("natural_size"),
            x=int(data.get("x") or 0),
            y=int(data.get("y") or 0),
        )
        return {"code": 200, "data": {"id": nid}}
    except Exception as e:
        return {"code": 500, "msg": str(e)}


async def handle_crawl_save_edge(websocket, data: dict):
    from server.services import crawl_persistence as cp

    try:
        cp.ensure_edge(
            int(data["graph_id"]),
            data["source_id"],
            data["target_id"],
            data.get("trigger") or {},
            source_handle=data.get("source_handle"),
            label=data.get("label") or "",
        )
        return {"code": 200, "msg": "ok"}
    except Exception as e:
        return {"code": 500, "msg": str(e)}


async def handle_crawl_train_skeleton(websocket, data: dict):
    from server.services import crawl_persistence as cp

    try:
        sk = cp.train_skeleton_for_node(
            int(data["graph_id"]),
            data["node_id"],
            data.get("images") or [],
            threshold=int(data.get("threshold") or 10),
        )
        return {"code": 200, "data": sk}
    except Exception as e:
        return {"code": 500, "msg": str(e)}


async def handle_crawl_app(websocket, data: dict):
    """
    跑图（前端触发）：下发到已连接的设备节点执行，自动写回图谱。
    data: graph_id, sn/target_sn, package?, platform?, max_pages?, max_sim?, min_sim?
    """
    import uuid
    from server.websocket.device_manager import DeviceManager
    from server.services.crawl_job_manager import wait_result

    graph_id = data.get("graph_id")
    sn = data.get("sn") or data.get("target_sn")
    if not graph_id or not sn:
        return {"code": 400, "msg": "请选择设备节点"}

    req_id = data.get("req_id") or f"crawl-{uuid.uuid4().hex[:12]}"
    dm = DeviceManager()
    if sn not in dm.active_connections:
        return {
            "code": 404,
            "msg": "设备节点未在线。请打开「设备管理」确认节点已连接后再跑图。",
        }

    params = dict(data)
    params["req_id"] = req_id
    params["target_sn"] = sn
    if not params.get("platform"):
        params["platform"] = "android"
    ok = await dm.send_command(str(sn), "crawl_app", params)
    if not ok:
        return {"code": 500, "msg": "指令下发失败"}

    SLog.i("WAppGraph", f"Crawl dispatched req_id={req_id} sn={sn} graph={graph_id}")
    result = await wait_result(req_id, timeout=float(data.get("timeout") or 3600))
    return result


async def handle_identify_page(websocket, data: dict):
    """
    根据骨架蒙版，判断上传/指定截图最像图谱中的哪个页面。
    入参: graph_id, content(base64) 或 image_name/filename, min_score(可选), top_k(可选)
    """
    session = SessionLocal()
    try:
        graph_id = data.get("graph_id")
        if not graph_id:
            return {"code": 400, "msg": "Missing graph_id"}

        target_gray, err = SkeletonAlgo.load_image_from_payload(data)
        if target_gray is None:
            return {"code": 400, "msg": err or "Failed to load image"}

        min_score = float(data.get("min_score") or 0.55)
        top_k = int(data.get("top_k") or 12)

        graph = session.query(AppGraph).filter(AppGraph.id == int(graph_id)).first()
        if not graph:
            return {"code": 404, "msg": "Graph not found"}

        db_nodes = session.query(AppNode).filter(
            AppNode.graph_id == int(graph_id),
            AppNode.type == NodeType.PAGE,
        ).all()

        candidates: list = []
        skipped: list = []
        for n in db_nodes:
            sk = n.skeleton_config or {}
            master_path, mask_path, ignored_areas = SkeletonAlgo.skeleton_config_paths(sk)
            if not master_path or not mask_path:
                skipped.append({
                    "node_id": n.node_id,
                    "label": n.label,
                    "reason": "no_skeleton",
                })
                continue
            candidates.append({
                "node_id": n.node_id,
                "label": n.label,
                "master_path": master_path,
                "mask_path": mask_path,
                "ignored_areas": ignored_areas,
                "screenshot": n.screenshot,
            })

        if not candidates:
            return {
                "code": 400,
                "msg": "No page with trained skeleton in this graph. Train skeleton on pages first.",
                "data": {"skipped_pages": skipped},
            }

        rankings = SkeletonAlgo.rank_page_candidates(target_gray, candidates, top_k=top_k)
        best = rankings[0] if rankings else None
        best_score = best.get("score", 0.0) if best else 0.0
        matched = bool(best and best_score >= min_score)

        SLog.i(
            "WAppGraph",
            f"Identify page graph={graph_id}: matched={matched} "
            f"best={best.get('label') if best else '-'} score={best_score:.3f}",
        )

        return {
            "code": 200,
            "data": {
                "matched": matched,
                "node_id": best.get("node_id") if matched else None,
                "label": best.get("label") if matched else None,
                "score": best_score,
                "min_score": min_score,
                "rankings": [
                    {
                        "node_id": r.get("node_id"),
                        "label": r.get("label"),
                        "score": r.get("score"),
                        "screenshot": r.get("screenshot"),
                    }
                    for r in rankings
                ],
                "skipped_pages": skipped,
                "candidates_count": len(candidates),
            },
        }
    except Exception as e:
        SLog.e("WAppGraph", f"Identify page error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_detect_page_components(websocket, data: dict):
    """从已有骨架蒙版识别页面组件候选。"""
    from server.core.vision.component_detector import ComponentDetector

    session = SessionLocal()
    try:
        graph_id = data.get("graph_id")
        node_id = data.get("node_id")
        if not graph_id or not node_id:
            return {"code": 400, "msg": "Missing graph_id or node_id"}

        node = session.query(AppNode).filter(
            AppNode.graph_id == int(graph_id),
            AppNode.node_id == node_id,
        ).first()
        if not node:
            return {"code": 404, "msg": "Node not found"}

        sk = node.skeleton_config or {}
        mask_path = sk.get("mask_url") or sk.get("filename")
        screenshot_path = node.screenshot
        if not mask_path and not screenshot_path:
            return {"code": 400, "msg": "No skeleton mask or screenshot on this page."}

        graph = session.query(AppGraph).filter(AppGraph.id == int(graph_id)).first()
        shared_components = []
        if graph and graph.variables:
            shared_components = (graph.variables or {}).get("shared_components") or []

        natural_w = natural_h = None
        if node.dom_tree:
            try:
                dom_json = json.loads(node.dom_tree)
                ns = dom_json.get("naturalSize") or {}
                natural_w = ns.get("w")
                natural_h = ns.get("h")
            except Exception:
                pass
        if (not natural_w or not natural_h) and mask_path:
            from server.core.vision.skeleton_algo import SkeletonAlgo
            probe = SkeletonAlgo._fetch_remote_image(mask_path)
            if probe is not None:
                natural_h, natural_w = probe.shape[:2]

        detected = ComponentDetector.detect_for_page(
            mask_path=mask_path,
            screenshot_path=screenshot_path,
            img_w=natural_w,
            img_h=natural_h,
            system_bars=sk.get("system_bars"),
            node_id=node_id,
            shared_components=shared_components,
        )
        return {
            "code": 200,
            "data": {
                "detected_components": detected,
                "hotspots": detected,
            },
        }
    except Exception as e:
        SLog.e("WAppGraph", f"Detect page components error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_detect_shared_components(websocket, data: dict):
    """跨页面共有组件相似度检测。"""
    from server.core.vision.shared_component_detector import SharedComponentDetector

    session = SessionLocal()
    try:
        graph_id = data.get("graph_id")
        if not graph_id:
            return {"code": 400, "msg": "Missing graph_id"}

        detail = await handle_app_graph_detail(websocket, {"graph_id": graph_id})
        if detail.get("code") != 200:
            return detail

        graph_data = detail.get("data") or {}
        nodes = graph_data.get("nodes") or []
        variables = graph_data.get("variables") or {}
        existing = variables.get("shared_components") or []

        min_similarity = float(data.get("min_similarity") or 0.72)
        region_hints = data.get("region_hints") or ["bottom_tab"]
        result = SharedComponentDetector.detect(
            nodes,
            region_hints=region_hints,
            min_similarity=min_similarity,
        )
        result["shared_components"] = SharedComponentDetector._merge_with_existing(
            result.get("clusters") or [], existing
        )
        return {"code": 200, "data": result}
    except Exception as e:
        SLog.e("WAppGraph", f"Detect shared components error: {e}")
        return {"code": 500, "msg": str(e)}
    finally:
        session.close()


async def handle_save_shared_components(websocket, data: dict):
    """保存图谱级共有组件定义到 graph.variables.shared_components。"""
    session = SessionLocal()
    try:
        graph_id = data.get("graph_id")
        shared = data.get("shared_components")
        if not graph_id or shared is None:
            return {"code": 400, "msg": "Missing graph_id or shared_components"}

        graph = session.query(AppGraph).filter(AppGraph.id == int(graph_id)).first()
        if not graph:
            return {"code": 404, "msg": "Graph not found"}

        variables = dict(graph.variables or {})
        variables["shared_components"] = shared
        graph.variables = variables
        session.commit()
        return {"code": 200, "msg": "Saved", "data": {"shared_components": shared}}
    except Exception as e:
        session.rollback()
        SLog.e("WAppGraph", f"Save shared components error: {e}")
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