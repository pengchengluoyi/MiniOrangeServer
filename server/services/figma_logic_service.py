# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
将 Figma 设计稿结构写入应用逻辑：图谱节点 + 应用知识库。

无需 Figma Developer OAuth 应用；使用普通账号的 Personal Access Token（file_content:read）即可。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from script.log import SLog
from server.models.AppGraph.app_structure import AppGraph, AppNode, AppEdge
from server.models.AppGraph.app_component import AppComponent
from server.models.AppGraph.app_types import NodeType
from server.models.project import App
from server.services import app_automation_service as aas
from server.services import figma_service as fs
from server.services.system_settings_service import list_testing_knowledge, save_testing_knowledge

TAG = "FigmaLogic"

FIGMA_NODE_PREFIX = "figma_page_"


def _tab_labels() -> list:
    """主导航 tab 文案来自应用画像（底栏 + 顶栏分段），不写死某个被测应用。"""
    from server.services.ai import app_profile as ap

    return list(ap.current().home_tabs())


def _slug(name: str) -> str:
    s = re.sub(r"\s+", "_", (name or "").strip())
    s = re.sub(r"[^\w\u4e00-\u9fff-]", "", s)
    return s[:48] or uuid.uuid4().hex[:8]


def ensure_app_graph(session: Session, app: App) -> AppGraph:
    graph = (
        session.query(AppGraph)
        .filter(AppGraph.app_id == app.id)
        .order_by(AppGraph.created_at.desc())
        .first()
    )
    if graph:
        return graph
    graph = AppGraph(
        app_id=app.id,
        name=f"{app.name or app.id} · Figma",
        desc="由 Figma 设计稿自动生成的应用逻辑图谱",
        icon="🎨",
    )
    session.add(graph)
    session.flush()
    SLog.i(TAG, f"Created AppGraph id={graph.id} for app={app.id}")
    return graph


def _upsert_page_node(
    session: Session,
    graph: AppGraph,
    page: Dict[str, Any],
    *,
    col: int,
) -> Tuple[AppNode, int, int]:
    figma_id = page.get("figma_id") or page.get("id") or ""
    label = (page.get("name") or "未命名页面").strip()
    node_id = f"{FIGMA_NODE_PREFIX}{_slug(label)}_{str(figma_id)[-6:]}"
    node = (
        session.query(AppNode)
        .filter(AppNode.graph_id == graph.id, AppNode.node_id == node_id)
        .first()
    )
    x = float(col * 320)
    y = 80.0
    if not node:
        node = AppNode(
            graph_id=graph.id,
            node_id=node_id,
            type=NodeType.PAGE,
            label=label,
            x=x,
            y=y,
        )
        session.add(node)
        session.flush()
    else:
        node.label = label

    dom_payload = {
        "source": "figma",
        "figma_id": figma_id,
        "frames": page.get("frames") or [],
        "texts": page.get("texts") or [],
        "keywords": page.get("keywords") or [],
    }
    node.dom_tree = json.dumps(dom_payload, ensure_ascii=False)
    anchors_added = _sync_anchor_components(session, graph, node, page)
    return node, 1, anchors_added


def _sync_anchor_components(
    session: Session,
    graph: AppGraph,
    node: AppNode,
    page: Dict[str, Any],
) -> int:
    """把 Figma 文案写入图谱 anchor 组件，供 Feedback / 断言引用。"""
    texts = list(dict.fromkeys(page.get("texts") or []))[:24]
    if not texts:
        return 0
    existing = {
        (c.label or "").strip()
        for c in session.query(AppComponent)
        .filter(AppComponent.graph_id == graph.id, AppComponent.node_id == node.id)
        .all()
    }
    added = 0
    for i, text in enumerate(texts):
        if text in existing:
            continue
        comp = AppComponent(
            graph_id=graph.id,
            node_id=node.id,
            uid=f"figma_anchor_{uuid.uuid4().hex[:10]}",
            label=text,
            category="anchor",
            sub_type="text",
            x=40.0,
            y=40.0 + i * 28,
            width=120.0,
            height=24.0,
            rules={"source": "figma"},
        )
        session.add(comp)
        added += 1
    return added


def _build_knowledge_rows(app_id: str, logic_pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for page in logic_pages:
        label = (page.get("name") or "").strip()
        if not label:
            continue
        texts = page.get("texts") or []
        frames = [f.get("name") for f in (page.get("frames") or []) if f.get("name")]
        keywords = page.get("keywords") or []
        content_lines = [
            f"设计稿页面「{label}」。",
            f"主要 Frame：{'、'.join(frames[:8])}" if frames else "",
            f"界面文案示例：{'、'.join(texts[:12])}" if texts else "",
        ]
        ops = _operational_hints(page)
        if ops:
            content_lines.append(f"操作建议：{'；'.join(ops[:5])}")
        content_lines.append(
            "页面识别：执行时用 OCR/层级文案与此页关键词匹配；"
            "若不在目标页，可先 Tab 切换或关闭协议弹层后再断言。"
        )
        rows.append(
            {
                "id": f"figma_{_slug(label)}",
                "title": f"Figma · {label}",
                "category": "UI导航",
                "tags": list(dict.fromkeys(keywords + [label] + frames[:4]))[:12],
                "content": "\n".join(x for x in content_lines if x),
                "app_ids": [str(app_id)],
                "enabled": True,
            }
        )
    return rows


def _operational_hints(page: Dict[str, Any]) -> List[str]:
    hints: List[str] = []
    for frame in page.get("frames") or []:
        name = (frame.get("name") or "").strip()
        if not name:
            continue
        if any(k in name for k in ("按钮", "登录", "一键", "Tab", "导航", "navbar", "Navbar")):
            hints.append(f"点击「{name}」")
        for t in frame.get("texts") or []:
            if any(k in t for k in ("登录", "同意", "一键", "确认")):
                hints.append(f"可点「{t}」")
    return list(dict.fromkeys(hints))[:8]


def _upsert_nav_edge(
    session: Session,
    graph_id: int,
    source_id: str,
    target_id: str,
    *,
    label: str,
) -> None:
    if not source_id or not target_id or source_id == target_id:
        return
    trigger = json.dumps({"type": "click", "label": label}, ensure_ascii=False)
    existing = (
        session.query(AppEdge)
        .filter(
            AppEdge.graph_id == graph_id,
            AppEdge.source == source_id,
            AppEdge.target == target_id,
        )
        .first()
    )
    if existing:
        existing.trigger = trigger
        existing.label = label
    else:
        session.add(
            AppEdge(
                graph_id=graph_id,
                edge_id=f"e-figma-{source_id}-{target_id}-{uuid.uuid4().hex[:6]}",
                source=source_id,
                target=target_id,
                label=label,
                trigger=trigger,
            )
        )


def _infer_tab_navigation_edges(
    session: Session,
    graph: AppGraph,
    logic_pages: List[Dict[str, Any]],
) -> int:
    """为底栏 Tab 类 Figma 页面生成互导航边。"""
    tab_nodes: List[Tuple[str, str]] = []
    for page in logic_pages:
        name = (page.get("name") or "").strip()
        node_id = page.get("node_id") or ""
        if not node_id:
            continue
        for tab in _tab_labels():
            if tab in name:
                tab_nodes.append((tab, node_id))
                break
    count = 0
    for _tab_a, id_a in tab_nodes:
        for tab_b, id_b in tab_nodes:
            if id_a == id_b:
                continue
            _upsert_nav_edge(session, graph.id, id_a, id_b, label=tab_b)
            count += 1
    if count:
        SLog.i(TAG, f"Inferred {count} tab navigation edges")
    return count


def _merge_knowledge(app_id: str, new_rows: List[Dict[str, Any]]) -> int:
    if not new_rows:
        return 0
    existing = list_testing_knowledge()
    by_id = {str(r.get("id")): r for r in existing if r.get("id")}
    for row in new_rows:
        by_id[str(row["id"])] = row
    save_testing_knowledge(list(by_id.values()))
    return len(new_rows)


def apply_figma_logic(
    app: App,
    session: Session,
    figma_payload: Dict[str, Any],
    *,
    write_knowledge: bool = True,
    write_graph: bool = True,
) -> Dict[str, Any]:
    """
    将 sync_figma_file 返回的结构写入 AppGraph + 知识库，并持久化 figma.logic。
    """
    logic_pages = figma_payload.get("logic", {}).get("pages") or []
    if not logic_pages:
        raise ValueError("Figma 同步结果为空，请先确认 Token 有 file_content:read 且文件链接正确")

    graph_id = None
    nodes_upserted = 0
    anchors_added = 0
    edges_added = 0

    if write_graph:
        graph = ensure_app_graph(session, app)
        graph_id = graph.id
        for i, page in enumerate(logic_pages):
            _, n, a = _upsert_page_node(session, graph, page, col=i)
            nodes_upserted += n
            anchors_added += a
        edges_added = _infer_tab_navigation_edges(session, graph, logic_pages)
        session.flush()

    knowledge_written = 0
    if write_knowledge:
        knowledge_written = _merge_knowledge(app.id, _build_knowledge_rows(app.id, logic_pages))

    cfg = aas.get_automation_config(app)
    figma_cfg = dict(cfg.get("figma") or {})
    figma_cfg.update(
        {
            "file_url": figma_payload.get("file_url") or figma_cfg.get("file_url") or "",
            "file_key": figma_payload.get("file_key") or figma_cfg.get("file_key") or "",
            "last_sync_at": figma_payload.get("last_sync_at") or figma_cfg.get("last_sync_at") or "",
            "pages_summary": figma_payload.get("pages_summary") or figma_cfg.get("pages_summary") or [],
            "logic": figma_payload.get("logic") or {},
            "logic_applied_at": figma_payload.get("logic", {}).get("synced_at"),
        }
    )
    aas.save_automation_config(app, {"figma": figma_cfg})
    session.commit()

    login_icons: Dict[str, Any] = {}
    try:
        from server.core.database import SessionLocal
        from server.services.figma_icon_service import seed_login_icons_from_figma

        with SessionLocal() as seed_db:
            app_row = seed_db.query(App).filter(App.id == app.id).first()
            if app_row:
                login_icons = seed_login_icons_from_figma(
                    seed_db,
                    app_row,
                    file_url=figma_payload.get("file_url") or "",
                    file_key=figma_payload.get("file_key") or "",
                    document=figma_payload.get("raw_document"),
                )
                seed_db.commit()
    except Exception as e:
        SLog.w(TAG, f"seed login icons from figma failed: {e}")
        login_icons = {"ok": False, "msg": str(e)}

    return {
        "graph_id": graph_id,
        "pages": len(logic_pages),
        "nodes_upserted": nodes_upserted,
        "anchors_added": anchors_added,
        "edges_added": edges_added,
        "knowledge_written": knowledge_written,
        "login_icons": login_icons,
        "figma": figma_cfg,
    }


def sync_and_apply_figma_logic(
    app: App,
    session: Session,
    *,
    file_url: str = "",
    file_key: str = "",
    write_knowledge: bool = True,
    write_graph: bool = True,
) -> Dict[str, Any]:
    synced = fs.sync_figma_file(
        file_url=file_url,
        file_key=file_key,
        depth=8,
        include_raw_document=True,
    )
    return apply_figma_logic(
        app,
        session,
        synced,
        write_knowledge=write_knowledge,
        write_graph=write_graph,
    )


def load_figma_logic_for_app(app: App) -> Optional[Dict[str, Any]]:
    cfg = aas.get_automation_config(app)
    logic = (cfg.get("figma") or {}).get("logic")
    if not logic or not logic.get("pages"):
        return None
    return logic


def _token_hits_screen(token: str, screen_text: str) -> bool:
    t = (token or "").strip()
    if len(t) < 2:
        return False
    blob = screen_text or ""
    blob_l = blob.lower()
    tl = t.lower()
    if tl in blob_l or t in blob:
        return True
    if len(t) >= 4:
        return t[: max(2, len(t) // 2)] in blob
    return False


def score_page_by_screen_text(screen_text: str, page: Dict[str, Any]) -> float:
    """用 Figma 页关键词与当前屏幕 OCR/层级文案做重叠打分。"""
    blob = screen_text or ""
    if not blob.strip():
        return 0.0

    weighted: List[Tuple[float, str]] = []
    name = (page.get("name") or "").strip()
    if name:
        weighted.append((3.0, name))
    for kw in page.get("keywords") or []:
        weighted.append((2.5, str(kw)))
    for t in (page.get("texts") or [])[:16]:
        weighted.append((1.5, str(t)))
    for frame in page.get("frames") or []:
        fn = (frame.get("name") or "").strip()
        if fn:
            weighted.append((1.8, fn))
        for t in (frame.get("texts") or [])[:8]:
            weighted.append((1.0, str(t)))

    seen = set()
    total_w = 0.0
    hit_w = 0.0
    for weight, raw in weighted:
        t = (raw or "").strip()
        if len(t) < 2 or t in seen:
            continue
        seen.add(t)
        total_w += weight
        if _token_hits_screen(t, blob):
            hit_w += weight

    if total_w <= 0:
        return 0.0
    return hit_w / total_w


def identify_page_from_figma_logic(
    screen_text: str,
    logic: Dict[str, Any],
    *,
    min_score: float = 0.22,
) -> Dict[str, Any]:
    pages = logic.get("pages") or []
    if not pages or not (screen_text or "").strip():
        return {"matched": False, "method": "figma_text", "score": 0.0}

    ranked = []
    for page in pages:
        score = score_page_by_screen_text(screen_text, page)
        if score > 0:
            ranked.append((score, page))
    ranked.sort(key=lambda x: x[0], reverse=True)

    if not ranked:
        return {"matched": False, "method": "figma_text", "score": 0.0, "rankings": []}

    best_score, best = ranked[0]
    matched = best_score >= min_score
    return {
        "matched": matched,
        "node_id": best.get("node_id"),
        "label": best.get("name"),
        "score": best_score,
        "method": "figma_text",
        "rankings": [
            {"label": p.get("name"), "node_id": p.get("node_id"), "score": s}
            for s, p in ranked[:5]
        ],
    }
