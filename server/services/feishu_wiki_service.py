# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""飞书 Wiki：读空间、列节点、建调试页。正式按版本落副本还没接到流程上。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
import re
import uuid

import requests

from server.services.feishu_service import _FEISHU_BASE, get_tenant_access_token

_WIKI_URL = "https://www.feishu.cn/wiki/{token}"


def _bot_id() -> Optional[str]:
    from server.services.system_settings_service import list_feishu_bots

    bots = [b for b in list_feishu_bots() if b.get("configured")]
    if not bots:
        raise RuntimeError("还没有可用的飞书机器人。先到「连接」里配好 App ID / Secret。")
    return str(bots[0].get("id") or "") or None


def _call(method: str, path: str, *, params: dict | None = None, payload: dict | None = None) -> dict:
    token = get_tenant_access_token(_bot_id())
    resp = requests.request(
        method,
        f"{_FEISHU_BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or None,
        json=payload,
        timeout=30,
    )
    try:
        body = resp.json()
    except Exception as e:
        raise RuntimeError(f"飞书 Wiki 返回了无法解析的内容（HTTP {resp.status_code}）") from e
    if body.get("code") != 0:
        raise RuntimeError(_friendly_error(body))
    data = body.get("data")
    return data if isinstance(data, dict) else {}


def _friendly_error(body: dict) -> str:
    msg = str(body.get("msg") or body.get("message") or "请求失败").strip()
    code = body.get("code")
    text = f"{msg}" + (f"（{code}）" if code not in (None, 0) else "")
    low = msg.lower()
    if "131006" in text or "wiki space permission denied" in low:
        return (
            "空间 ID 已经读到了，但机器人进不去这个知识空间（131006）。"
            "到飞书开放平台给应用开通 Wiki 权限并发布版本；再到该知识空间设置里，把这个应用/机器人加为成员或管理员。"
        )
    if "permission" in low or "denied" in low or "access" in low or "99991663" in text or "99991664" in text:
        return f"{text}。应用需要开通 Wiki 权限，并把机器人加入这个知识空间。"
    if "not found" in low or "131005" in text or "131002" in text:
        return f"{text}。核对知识空间 ID / 根节点 token，或换一个机器人有权访问的空间。"
    return text


def _node_url(token: str) -> str:
    tid = str(token or "").strip()
    return _WIKI_URL.format(token=tid) if tid else ""


def _public_node(raw: dict | None) -> dict:
    row = raw if isinstance(raw, dict) else {}
    token = str(row.get("node_token") or row.get("obj_token") or "").strip()
    return {
        "title": str(row.get("title") or "").strip() or "未命名",
        "node_token": str(row.get("node_token") or "").strip(),
        "obj_token": str(row.get("obj_token") or "").strip(),
        "obj_type": str(row.get("obj_type") or "").strip() or "node",
        "space_id": str(row.get("space_id") or "").strip(),
        "parent_node_token": str(row.get("parent_node_token") or "").strip(),
        "url": _node_url(token),
    }


def get_space(space_id: str) -> dict:
    sid = str(space_id or "").strip()
    if not sid:
        raise RuntimeError("请填写知识空间 ID")
    data = _call("GET", f"/wiki/v2/spaces/{sid}")
    space = data.get("space") if isinstance(data.get("space"), dict) else data
    return {
        "space_id": str(space.get("space_id") or sid).strip(),
        "name": str(space.get("name") or "").strip() or sid,
        "description": str(space.get("description") or "").strip(),
    }


def get_node(node_token: str) -> dict:
    token = str(node_token or "").strip()
    if not token:
        raise RuntimeError("缺少节点 token")
    data = _call("GET", "/wiki/v2/spaces/get_node", params={"token": token})
    return _public_node(data.get("node") if isinstance(data.get("node"), dict) else data)


def list_spaces(page_size: int = 20) -> list[dict]:
    data = _call("GET", "/wiki/v2/spaces", params={"page_size": max(1, min(int(page_size or 20), 50))})
    rows = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "space_id": str(item.get("space_id") or "").strip(),
                "name": str(item.get("name") or "").strip() or "未命名空间",
                "description": str(item.get("description") or "").strip(),
            }
        )
    return rows


def list_nodes(space_id: str, parent_node_token: str = "", page_size: int = 50) -> list[dict]:
    sid = str(space_id or "").strip()
    if not sid:
        raise RuntimeError("请填写知识空间 ID")
    params: Dict[str, Any] = {"page_size": max(1, min(int(page_size or 50), 50))}
    parent = str(parent_node_token or "").strip()
    if parent:
        params["parent_node_token"] = parent
    data = _call("GET", f"/wiki/v2/spaces/{sid}/nodes", params=params)
    out = []
    for item in data.get("items") or []:
        if isinstance(item, dict):
            out.append(_public_node(item))
    return out


def create_node(space_id: str, title: str, *, parent_node_token: str = "", obj_type: str = "docx") -> dict:
    sid = str(space_id or "").strip()
    name = str(title or "").strip()
    if not sid:
        raise RuntimeError("请填写知识空间 ID")
    if not name:
        raise RuntimeError("标题不能为空")
    payload = {
        "obj_type": str(obj_type or "docx").strip() or "docx",
        "node_type": "origin",
        "title": name,
    }
    parent = str(parent_node_token or "").strip()
    if parent:
        payload["parent_node_token"] = parent
    data = _call("POST", f"/wiki/v2/spaces/{sid}/nodes", payload=payload)
    node = data.get("node") if isinstance(data.get("node"), dict) else data
    return _public_node(node)


def update_node_title(space_id: str, node_token: str, title: str) -> None:
    sid = str(space_id or "").strip()
    token = str(node_token or "").strip()
    name = str(title or "").strip()
    if not sid or not token or not name:
        return
    _call("POST", f"/wiki/v2/spaces/{sid}/nodes/{token}/update_title", payload={"title": name})


def preview_path(*, folder_pattern: str, children: list | None = None, project: str = "MiniOrange", version: str = "调试") -> dict:
    pattern = str(folder_pattern or "{project}/版本/{version}").strip() or "{project}/版本/{version}"
    path = (
        pattern.replace("{project}", project)
        .replace("{app}", project)
        .replace("{version}", version)
    )
    segs = [p for p in path.split("/") if p.strip()]
    kids = [str(x).strip() for x in (children or []) if str(x).strip()]
    return {
        "pattern": pattern,
        "path": path,
        "segments": segs,
        "children": kids,
        "preview": " / ".join(segs + (kids[:1] or [])),
    }


def resolve_space(*, space_id: str = "", root_node_token: str = "") -> dict:
    sid = str(space_id or "").strip()
    root = str(root_node_token or "").strip()
    note = ""
    if sid:
        try:
            space = get_space(sid)
            return {**space, "root_node_token": root, "note": note}
        except RuntimeError:
            node = get_node(sid)
            resolved = str(node.get("space_id") or "").strip()
            if not resolved:
                raise RuntimeError("这个 ID 既不是知识空间，也解析不出所属空间。")
            note = "填写的是节点 token，已解析到所属知识空间。"
            if not root:
                root = str(node.get("node_token") or sid).strip()
            space = get_space(resolved)
            return {**space, "root_node_token": root, "note": note}
    if root:
        node = get_node(root)
        resolved = str(node.get("space_id") or "").strip()
        if not resolved:
            raise RuntimeError("根节点 token 解析不出知识空间。")
        space = get_space(resolved)
        return {**space, "root_node_token": root, "note": "用根节点定位到知识空间。"}
    spaces = list_spaces()
    if not spaces:
        raise RuntimeError("这个机器人还看不到任何知识空间。把机器人拉进空间后再试。")
    first = spaces[0]
    return {**first, "root_node_token": "", "note": f"未填空间 ID，先用机器人能看到的「{first.get('name')}」。", "spaces": spaces}


def _find_child(nodes: list[dict], title: str, obj_type: str = "") -> dict | None:
    want = str(title or "").strip()
    typed = str(obj_type or "").strip()
    for row in nodes:
        if str(row.get("title") or "").strip() != want:
            continue
        if typed and str(row.get("obj_type") or "").strip() != typed:
            continue
        return row
    return None


def ensure_child(space_id: str, title: str, parent_node_token: str = "") -> tuple[dict, bool]:
    kids = list_nodes(space_id, parent_node_token)
    found = _find_child(kids, title)
    if found:
        return found, False
    return create_node(space_id, title, parent_node_token=parent_node_token), True


def _items_from_nodes(nodes: list[dict]) -> list[dict]:
    return [
        {
            "label": row.get("title") or "未命名",
            "meta": row.get("obj_type") or "node",
            "url": row.get("url") or "",
            "token": row.get("node_token") or "",
        }
        for row in nodes
    ]


def debug_wiki(
    *,
    action: str,
    space_id: str = "",
    root_node_token: str = "",
    folder_pattern: str = "",
    children: list | None = None,
    project: str = "MiniOrange",
    version: str = "调试",
) -> Dict[str, Any]:
    kind = str(action or "ping").strip() or "ping"
    if kind not in ("ping", "list", "mkdir", "create_doc"):
        raise ValueError("不支持的调试动作")

    space = resolve_space(space_id=space_id, root_node_token=root_node_token)
    sid = str(space.get("space_id") or "").strip()
    parent = str(space.get("root_node_token") or "").strip()
    preview = preview_path(
        folder_pattern=folder_pattern,
        children=children,
        project=project or "MiniOrange",
        version=version or "调试",
    )

    if kind == "ping":
        nodes = list_nodes(sid, parent)
        extra = space.get("spaces") if isinstance(space.get("spaces"), list) else []
        items = _items_from_nodes(nodes)
        if extra and not items:
            items = [{"label": row.get("name"), "meta": row.get("space_id"), "url": "", "token": row.get("space_id")} for row in extra]
        return {
            "ok": True,
            "action": kind,
            "title": f"已连上「{space.get('name')}」",
            "summary": space.get("note") or f"空间 {sid} · 当前层 {len(nodes)} 个节点",
            "space": {"space_id": sid, "name": space.get("name"), "description": space.get("description")},
            "preview": preview,
            "items": items,
        }

    if kind == "list":
        nodes = list_nodes(sid, parent)
        return {
            "ok": True,
            "action": kind,
            "title": f"「{space.get('name')}」当前层",
            "summary": f"{len(nodes)} 个节点" + (f" · {space.get('note')}" if space.get("note") else ""),
            "space": {"space_id": sid, "name": space.get("name")},
            "preview": preview,
            "items": _items_from_nodes(nodes),
        }

    if kind == "create_doc":
        title = f"MiniOrange调试-{datetime.now().strftime('%m%d-%H%M')}"
        created = create_node(sid, title, parent_node_token=parent)
        return {
            "ok": True,
            "action": kind,
            "title": f"已建调试页「{created.get('title')}」",
            "summary": "这是空文档，用来确认机器人能写。测完可以在飞书里删掉。",
            "space": {"space_id": sid, "name": space.get("name")},
            "preview": preview,
            "url": created.get("url") or "",
            "items": _items_from_nodes([created]),
        }

    # mkdir：按规则 find-or-create，避免连点刷出一堆文件夹
    created: List[dict] = []
    reused: List[dict] = []
    cursor = parent
    for title in preview.get("segments") or []:
        node, is_new = ensure_child(sid, title, cursor)
        cursor = str(node.get("node_token") or "").strip()
        (created if is_new else reused).append(node)
    for title in preview.get("children") or []:
        node, is_new = ensure_child(sid, title, cursor)
        (created if is_new else reused).append(node)
    return {
        "ok": True,
        "action": kind,
        "title": "调试文件夹已对齐",
        "summary": f"新建 {len(created)} 个，已有 {len(reused)} 个。路径 {preview.get('path') or '—'}",
        "space": {"space_id": sid, "name": space.get("name")},
        "preview": preview,
        "url": (created[-1].get("url") if created else (reused[-1].get("url") if reused else "")),
        "items": _items_from_nodes(created + reused),
    }


_MINDMAP_FOLDER = "测试脑图"
_WIKI_HISTORY_MAX = 30
# 画板建节点接口一次最多 3000 个，这里留足余量
_BOARD_MAX_NODES = 1000
_BOARD_NODE_MAX_W = 520
_BOARD_ROOT_FILL = "#245BDB"
_BOARD_STRUCT_FILL = "#4E5969"
_BOARD_INK = "#1F2329"
_BOARD_TEXT = "#373C43"
# 一级分支各用一个色系，同一支下面的节点跟着它走，分支之间才分得开
_BOARD_PALETTE = [
    ("#3370FF", "#E1EAFF"),
    ("#34B764", "#DFF5E6"),
    ("#FF8800", "#FFF3E0"),
    ("#7F3BF5", "#F0E8FF"),
    ("#0FBFC4", "#DDF6F7"),
    ("#E8558E", "#FDE9F1"),
]


def wiki_settings() -> dict:
    from server.services.system_settings_service import get_integration_plugin

    plugin = get_integration_plugin("feishu") or {}
    cfg = plugin.get("config") if isinstance(plugin.get("config"), dict) else {}
    wiki = cfg.get("wiki") if isinstance(cfg.get("wiki"), dict) else {}
    return {
        "space_id": str(wiki.get("space_id") or "").strip(),
        "root_node_token": str(wiki.get("root_node_token") or "").strip(),
        "folder_pattern": str(wiki.get("folder_pattern") or "{project}/版本/{version}").strip()
        or "{project}/版本/{version}",
        "children": [str(x).strip() for x in (wiki.get("children") or []) if str(x).strip()],
    }


def _safe_title(text: str, limit: int = 40) -> str:
    s = re.sub(r'[\\/:*?"<>|\n\r]+', " ", str(text or "")).strip()
    s = re.sub(r"\s+", " ", s)
    return (s or "未命名需求")[:limit]


def _mindmap_line(node: dict) -> str:
    text = str(node.get("text") or node.get("title") or "").strip()
    detail = str(node.get("detail") or "").strip()
    if text and detail:
        return f"{text} · {detail}"
    return text or detail


def mindmap_to_branches(tree: dict) -> list[dict]:
    """把脑图树摊成思维笔记要的形状：只留有文字的节点，根节点自己不算一层。"""

    def convert(node: dict) -> list[dict]:
        if not isinstance(node, dict):
            return []
        kids: list[dict] = []
        for child in node.get("children") or []:
            kids.extend(convert(child))
        line = _mindmap_line(node)
        if not line:
            return kids
        return [{"text": line[:2000], "children": kids}]

    root = tree if isinstance(tree, dict) else {}
    branches: list[dict] = []
    for child in root.get("children") or []:
        branches.extend(convert(child))
    if not branches:
        branches = convert(root)
    if not branches:
        raise ValueError("脑图是空的，没有可写入的内容")
    return branches


def _board_node_size(text: str, font_size: int) -> tuple[int, int]:
    """画板要求 width/height 必填，按字号和文字长度估一个不挤的框。"""
    px = len(text) * (font_size + 1) + 40
    width = min(_BOARD_NODE_MAX_W, max(120, px))
    lines = -(-px // width)
    return width, 18 + (font_size + 14) * lines


def _board_node_look(depth: int, color: str, light: str, leaf: bool) -> dict:
    """按层级定形状、配色和字号，让脑图有主次，而不是一堆一样的框。"""
    if depth == 0:
        return {
            "shape": "mind_map_full_round_rect",
            "font_size": 20,
            "style": {
                "fill_color": _BOARD_ROOT_FILL,
                "fill_color_type": 1,
                "fill_opacity": 100,
                "border_style": "none",
            },
            "text": {"font_weight": "bold", "text_color": "#FFFFFF", "horizontal_align": "center"},
        }
    if depth == 1:
        return {
            "shape": "mind_map_full_round_rect",
            "font_size": 16,
            "style": {
                "fill_color": color,
                "fill_color_type": 1,
                "fill_opacity": 100,
                "border_style": "none",
            },
            "text": {"font_weight": "bold", "text_color": "#FFFFFF", "horizontal_align": "center"},
        }
    if depth == 2:
        return {
            "shape": "mind_map_round_rect",
            "font_size": 15,
            "style": {
                "fill_color": light,
                "fill_color_type": 1,
                "fill_opacity": 100,
                "border_color": color,
                "border_color_type": 1,
                "border_style": "solid",
                "border_width": "narrow",
                "border_opacity": 100,
            },
            "text": {"font_weight": "bold", "text_color": _BOARD_INK, "horizontal_align": "center"},
        }
    if not leaf:
        return {
            "shape": "mind_map_round_rect",
            "font_size": 14,
            "style": {
                "fill_color": "#FFFFFF",
                "fill_color_type": 1,
                "fill_opacity": 100,
                "border_color": color,
                "border_color_type": 1,
                "border_style": "solid",
                "border_width": "extra_narrow",
                "border_opacity": 60,
            },
            "text": {"text_color": _BOARD_INK, "horizontal_align": "left"},
        }
    # 末端的测试点最多，用纯文字不画框，整张图才不会糊成一片方块
    return {
        "shape": "mind_map_text",
        "font_size": 14,
        "style": {"border_style": "none", "fill_opacity": 0},
        "text": {"text_color": _BOARD_TEXT, "horizontal_align": "left"},
    }


def mindmap_to_board_nodes(tree: dict, title: str) -> list[dict]:
    """把脑图树转成画板的思维导图节点：一个根节点 + 用批次内自编 id 串起来的子节点。"""

    def build(node_id: str, text: str, depth: int, color: str, light: str, leaf: bool) -> dict:
        look = _board_node_look(depth, color, light, leaf)
        width, height = _board_node_size(text, look["font_size"])
        return {
            "id": node_id,
            "type": "mind_map",
            "width": width,
            "height": height,
            "text": {"text": text, "font_size": look["font_size"], "vertical_align": "mid", **look["text"]},
            "style": look["style"],
            "_shape": look["shape"],
        }

    root_text = str(title or "测试脑图")[:1024]
    root = build("m0", root_text, 0, _BOARD_ROOT_FILL, _BOARD_ROOT_FILL, False)
    root["x"] = 0
    root["y"] = 0
    root["mind_map_root"] = {
        "layout": "left_right",
        "type": root.pop("_shape"),
        "line_style": "round_angle",
    }
    nodes: list[dict] = [root]
    branches = mindmap_to_branches(tree)
    # 一级只有一两个「端」时按它上色分不开，把配色下移到模块那一层
    hue_depth = 1 if len(branches) >= 3 else 2
    seq = 0
    hue_seq = -1

    def walk(items: list[dict], parent_id: str, depth: int, branch: int) -> None:
        nonlocal seq, hue_seq
        for order, item in enumerate(items):
            if len(nodes) >= _BOARD_MAX_NODES:
                return
            seq += 1
            kids = item.get("children") or []
            hue = branch
            if depth == hue_depth:
                hue_seq += 1
                hue = hue_seq
            if depth < hue_depth:
                color = light = _BOARD_STRUCT_FILL
            else:
                color, light = _BOARD_PALETTE[hue % len(_BOARD_PALETTE)]
            row = build(f"m{seq}", str(item.get("text") or "")[:1024], depth, color, light, not kids)
            row["mind_map_node"] = {
                "parent_id": parent_id,
                "type": row.pop("_shape"),
                "z_index": order,
            }
            # 左右布局下只有根节点的直接子节点能指定方向，交替放才能左右均衡
            if depth == 1:
                row["mind_map_node"]["layout_position"] = "right" if order % 2 == 0 else "left"
            nodes.append(row)
            walk(kids, f"m{seq}", depth + 1, hue)

    walk(branches, "m0", 1, 0)
    return nodes


def _docx_children(document_id: str) -> list[dict]:
    items: list[dict] = []
    page_token = ""
    while True:
        params: Dict[str, Any] = {"page_size": 500, "document_revision_id": -1}
        if page_token:
            params["page_token"] = page_token
        data = _call(
            "GET",
            f"/docx/v1/documents/{document_id}/blocks/{document_id}/children",
            params=params,
        )
        items.extend(x for x in (data.get("items") or []) if isinstance(x, dict))
        if not data.get("has_more"):
            break
        page_token = str(data.get("page_token") or "").strip()
        if not page_token:
            break
    return items


def _board_token(block: dict) -> str:
    board = block.get("board") if isinstance(block.get("board"), dict) else {}
    return str(board.get("token") or block.get("token") or block.get("block_id") or "").strip()


def _reset_board_block(document_id: str) -> str:
    """把文档正文清空，重新插一块画板，返回它的 whiteboard_id。

    画板节点没有删除接口，重建整块是唯一能保证「更新」不残留旧节点的做法。
    """
    doc_id = str(document_id or "").strip()
    if not doc_id:
        raise RuntimeError("文档 token 为空，没法插画板")
    existing = _docx_children(doc_id)
    if existing:
        _call(
            "POST",
            f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children/batch_delete",
            params={"document_revision_id": -1},
            payload={"start_index": 0, "end_index": len(existing)},
        )
    _call(
        "POST",
        f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
        params={"document_revision_id": -1},
        payload={"children": [{"block_type": 43, "board": {"align": 1}}]},
    )
    for block in _docx_children(doc_id):
        if int(block.get("block_type") or 0) == 43:
            token = _board_token(block)
            if token:
                return token
    raise RuntimeError("画板块插进去了，但没读到 whiteboard_id")


def _node_tokens(space_id: str, node: dict) -> tuple[dict, str, str]:
    """补齐 node_token / obj_token，新建的节点有时只回其中一个。"""
    node_token = str(node.get("node_token") or "").strip()
    obj_token = str(node.get("obj_token") or "").strip()
    if not obj_token and node_token:
        node = get_node(node_token)
        node_token = str(node.get("node_token") or node_token).strip()
        obj_token = str(node.get("obj_token") or "").strip()
    if not obj_token:
        raise RuntimeError("Wiki 节点已建好，但没有拿到文档 token。确认应用开通了云文档权限。")
    return node, node_token, obj_token


def _retire_node(space_id: str, node_token: str, title: str) -> str:
    """把写不进去的旧文档改名挪开，返回新标题。

    飞书 Wiki 没有删除节点的开放接口，只能靠改名避免目录里两篇同名。
    """
    if not node_token:
        return ""
    retired = _safe_title(f"{title}（旧版 {datetime.now().strftime('%m-%d %H:%M')}）", 50)
    try:
        update_node_title(space_id, node_token, retired)
    except RuntimeError:
        return ""
    return retired


def _wiki_history(req: dict) -> list[dict]:
    rows = req.get("mindmap_wiki_history")
    if not isinstance(rows, list):
        return []
    return [dict(x) for x in rows if isinstance(x, dict)]


def _latest_mindmap_dialogue(req: dict) -> str:
    """重试脑图时用户输入的文案：优先最近一次 cover_history，否则 analyst_feedback。"""
    hist = [x for x in (req.get("cover_history") or []) if isinstance(x, dict)]
    for row in reversed(hist):
        if str(row.get("job") or "") != "draft_mindmap":
            continue
        note = str(row.get("note") or "").strip()
        if note:
            return note
    return str(req.get("analyst_feedback") or "").strip()


def _snapshot_mindmap(req: dict) -> dict:
    """写入 Wiki / 失效回滚用的脑图快照（只要树，不要 failures）。"""
    mm = req.get("mindmap") if isinstance(req.get("mindmap"), dict) else {}
    if not mm:
        return {}
    out = dict(mm)
    out.pop("failures", None)
    out.pop("stats", None)
    return out


def _parse_iso_naive(value: str) -> Optional[datetime]:
    """解析 ISO 时间并去掉时区，避免 naive/aware 相减报错。"""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt


def _mindmap_from_wiki_row(req: dict, row: dict) -> dict:
    """从历史行恢复脑图：优先行内快照，否则按时间就近找 cover_history。"""
    if isinstance(row, dict):
        snap = row.get("mindmap_snapshot")
        if isinstance(snap, dict) and (snap.get("children") or snap.get("text") or snap.get("title")):
            return dict(snap)
    target = _parse_iso_naive((row or {}).get("at") or "")
    best = None
    best_delta = None
    for h in req.get("cover_history") or []:
        if not isinstance(h, dict) or str(h.get("job") or "") != "draft_mindmap":
            continue
        payload = h.get("payload") if isinstance(h.get("payload"), dict) else {}
        if not (payload.get("children") or payload.get("text") or payload.get("title")):
            continue
        if target is None:
            best = payload
            continue
        hat = _parse_iso_naive(h.get("at") or "")
        if hat is None:
            continue
        delta = abs((hat - target).total_seconds())
        if delta > 12 * 3600:
            continue
        if best_delta is None or delta < best_delta:
            best_delta = delta
            best = payload
    return dict(best) if isinstance(best, dict) else {}


def _retire_history(history: list[dict], node_token: str, title: str) -> None:
    """旧文档改名后，历史里指向它的记录跟着改名并标成已归档。"""
    for row in history:
        if str(row.get("node_token") or "") == node_token:
            row["title"] = title
            row["retired"] = True


def _wiki_row_as_current(row: dict) -> dict:
    """把历史里的一条提成 mindmap_wiki 当前指针（不含 retired / invalid）。"""
    return {
        "url": str(row.get("url") or ""),
        "node_token": str(row.get("node_token") or ""),
        "obj_token": str(row.get("obj_token") or ""),
        "obj_type": str(row.get("obj_type") or "docx"),
        "whiteboard_id": str(row.get("whiteboard_id") or ""),
        "title": str(row.get("title") or ""),
        "space_id": str(row.get("space_id") or ""),
        "folder": str(row.get("folder") or ""),
        "created": bool(row.get("created")),
        "updated_at": str(row.get("at") or row.get("updated_at") or datetime.now().isoformat(timespec="seconds")),
        "nodes": int(row.get("nodes") or 0),
    }


def invalidate_mindmap_wiki(qa_process: dict, *, requirement_id: str, node_token: str = "") -> dict:
    """将当前飞书脑图设为失效：移入失效列表，上一份有效记录变成当前，并恢复其脑图内容。

    飞书没有删节点接口，会尽量把失效文档改名成「旧版」。
    """
    from server.services.qa_role_jobs import apply_mindmap

    doc = dict(qa_process or {})
    reqs = [dict(r) for r in (doc.get("requirements") or []) if isinstance(r, dict)]
    rid = str(requirement_id or "").strip()
    idx = next((i for i, r in enumerate(reqs) if str(r.get("id") or "") == rid), -1)
    if idx < 0:
        raise ValueError("请先选一条需求")
    req = reqs[idx]
    history = _wiki_history(req)
    cur = req.get("mindmap_wiki") if isinstance(req.get("mindmap_wiki"), dict) else {}
    token = str(node_token or cur.get("node_token") or "").strip()
    if not token:
        raise ValueError("没有可失效的飞书脑图")
    if str(cur.get("node_token") or "").strip() != token:
        raise ValueError("只能把状态为「当前」的脑图设为失效")

    hide_i = next((i for i, r in enumerate(history) if str(r.get("node_token") or "") == token), -1)
    if hide_i < 0:
        history.insert(
            0,
            {
                "url": cur.get("url") or "",
                "node_token": token,
                "title": cur.get("title") or "",
                "folder": cur.get("folder") or "",
                "nodes": cur.get("nodes") or 0,
                "at": cur.get("updated_at") or datetime.now().isoformat(timespec="seconds"),
                "retired": False,
                "invalid": False,
                "dialogue": _latest_mindmap_dialogue(req),
                "mindmap_snapshot": _snapshot_mindmap(req),
                "space_id": cur.get("space_id") or "",
            },
        )
        hide_i = 0

    hide_row = history[hide_i]
    # 确保失效前把当前内存脑图落进快照，方便以后对照
    if not hide_row.get("mindmap_snapshot"):
        hide_row["mindmap_snapshot"] = _snapshot_mindmap(req)

    prev_row = None
    for j in range(hide_i + 1, len(history)):
        cand = history[j]
        if cand.get("invalid"):
            continue
        tok = str(cand.get("node_token") or "").strip()
        if tok and tok != token:
            prev_row = cand
            break
    if not prev_row:
        raise ValueError("没有上一份有效脑图可恢复为当前")

    base_title = str(hide_row.get("title") or cur.get("title") or "测试脑图")
    base_title = re.sub(r"（旧版[^）]*）\s*$", "", base_title).strip() or "测试脑图"
    space_id = str(cur.get("space_id") or hide_row.get("space_id") or "").strip()
    retired_title = ""
    if space_id and token:
        retired_title = _retire_node(space_id, token, base_title)
    if not retired_title:
        retired_title = _safe_title(f"{base_title}（旧版 {datetime.now().strftime('%m-%d %H:%M')}）", 50)
    hide_row["title"] = retired_title
    hide_row["retired"] = True
    hide_row["invalid"] = True

    prev_row["retired"] = False
    prev_row["invalid"] = False
    prev_title = str(prev_row.get("title") or "").strip()
    cleaned = re.sub(r"（旧版[^）]*）\s*$", "", prev_title).strip()
    if cleaned:
        prev_row["title"] = cleaned

    next_req = dict(req)
    next_req["mindmap_wiki"] = _wiki_row_as_current(prev_row)
    next_req["mindmap_wiki"]["title"] = prev_row.get("title") or next_req["mindmap_wiki"].get("title") or ""
    next_req["mindmap_wiki_history"] = history

    restored = _mindmap_from_wiki_row(req, prev_row)
    if restored:
        next_req = apply_mindmap(next_req, restored)

    reqs[idx] = next_req
    doc["requirements"] = reqs
    return {
        "qa_process": doc,
        "wiki": next_req["mindmap_wiki"],
        "history": history,
        "invalidated": {"node_token": token, "title": retired_title},
        "current": next_req["mindmap_wiki"],
        "mindmap_restored": bool(restored),
    }


def hide_mindmap_wiki(qa_process: dict, *, requirement_id: str, node_token: str = "") -> dict:
    """兼容旧名：同 invalidate_mindmap_wiki。"""
    return invalidate_mindmap_wiki(qa_process, requirement_id=requirement_id, node_token=node_token)


def _write_board_nodes(whiteboard_id: str, nodes: list[dict]) -> int:
    """一次提交整棵树。批次内自编的 id 只在同一个请求里有效，所以不能分批。"""
    if not nodes:
        raise ValueError("脑图是空的，没有可写入的内容")
    _call(
        "POST",
        f"/board/v1/whiteboards/{whiteboard_id}/nodes",
        params={"client_token": uuid.uuid4().hex},
        payload={"nodes": nodes},
    )
    return len(nodes)


def _ensure_path(space_id: str, segments: list[str], parent_token: str = "") -> str:
    cursor = str(parent_token or "").strip()
    sid = str(space_id or "").strip()
    for title in segments:
        name = _safe_title(title, 50)
        if not name:
            continue
        node, _created = ensure_child(sid, name, cursor)
        cursor = str(node.get("node_token") or "").strip()
        if not cursor:
            raise RuntimeError(f"创建 Wiki 目录「{name}」后没有拿到节点 token")
    return cursor


def _version_label(qa_process: dict, req: dict, release_id: str = "") -> str:
    rid = str(release_id or req.get("release_id") or "").strip()
    for rel in qa_process.get("releases") or []:
        if not isinstance(rel, dict):
            continue
        if rid and str(rel.get("id") or "") == rid:
            return _safe_title(rel.get("title") or rel.get("name") or rel.get("version") or rid, 30)
        if not rid and req.get("id") and req.get("id") in (rel.get("requirement_ids") or []):
            return _safe_title(rel.get("title") or rel.get("name") or rel.get("version") or "", 30)
    return "未分版本"


def publish_mindmap(
    qa_process: dict,
    *,
    requirement_id: str,
    app_name: str = "",
    release_id: str = "",
) -> dict:
    """在飞书 Wiki 对应目录下创建/更新文档，把当前需求的脑图写进去。"""
    doc = dict(qa_process or {})
    reqs = [r for r in (doc.get("requirements") or []) if isinstance(r, dict)]
    rid = str(requirement_id or "").strip()
    req = next((r for r in reqs if str(r.get("id") or "") == rid), None)
    if not req:
        raise ValueError("请先选一条需求")
    mindmap = req.get("mindmap") if isinstance(req.get("mindmap"), dict) else {}
    if not (mindmap.get("children") or mindmap.get("text") or mindmap.get("title")):
        raise ValueError("这条需求还没有脑图，先生成或导入后再写入 Wiki")

    cfg = wiki_settings()
    if not (cfg.get("space_id") or cfg.get("root_node_token")):
        raise RuntimeError("还没有配置飞书 Wiki 空间。到「连接 → 飞书 → Wiki」填写知识空间 ID。")
    space = resolve_space(space_id=cfg.get("space_id") or "", root_node_token=cfg.get("root_node_token") or "")
    sid = str(space.get("space_id") or "").strip()
    parent = str(space.get("root_node_token") or "").strip()
    project = _safe_title(app_name or "应用", 30)
    version = _version_label(doc, req, release_id)
    preview = preview_path(
        folder_pattern=cfg.get("folder_pattern") or "{project}/版本/{version}",
        children=[_MINDMAP_FOLDER],
        project=project,
        version=version,
    )
    folder_token = _ensure_path(
        sid,
        list(preview.get("segments") or []) + list(preview.get("children") or []),
        parent,
    )

    title = _safe_title(f"{req.get('title') or req.get('external_id') or rid} 测试脑图", 40)
    prev = req.get("mindmap_wiki") if isinstance(req.get("mindmap_wiki"), dict) else {}
    kids = list_nodes(sid, folder_token)
    created = False

    prev_node = None
    prev_token = str(prev.get("node_token") or "").strip()
    if prev_token:
        try:
            prev_node = get_node(prev_token)
        except RuntimeError:
            prev_node = None
    if prev_node and (
        str(prev_node.get("parent_node_token") or "").strip() != folder_token
        or str(prev_node.get("obj_type") or "").strip() != "docx"
    ):
        prev_node = None

    node = prev_node or _find_child(kids, title, obj_type="docx")
    if not node:
        node = create_node(sid, title, parent_node_token=folder_token, obj_type="docx")
        created = True

    node, node_token, obj_token = _node_tokens(sid, node)
    history = _wiki_history(req)

    nodes = mindmap_to_board_nodes(mindmap, str(req.get("title") or title))
    try:
        whiteboard_id = _reset_board_block(obj_token)
    except RuntimeError as exc:
        if created:
            raise RuntimeError(
                f"{exc}。往文档里插画板需要开通「云文档」权限（docx:document），并把机器人加进这个知识空间。"
            ) from exc
        # 旧文档改不动就给它改个名让开，重新建一篇干净的
        retired = _retire_node(sid, node_token, title)
        if retired:
            _retire_history(history, node_token, retired)
        node, node_token, obj_token = _node_tokens(
            sid, create_node(sid, title, parent_node_token=folder_token, obj_type="docx")
        )
        created = True
        whiteboard_id = _reset_board_block(obj_token)

    try:
        written = _write_board_nodes(whiteboard_id, nodes)
    except RuntimeError as exc:
        raise RuntimeError(
            f"{exc}。写画板节点需要开通「画板节点创建」权限（board:whiteboard:node:create）并发布版本。"
        ) from exc

    wiki_row = {
        "url": str(node.get("url") or _node_url(node_token)),
        "node_token": node_token,
        "obj_token": obj_token,
        "obj_type": "docx",
        "whiteboard_id": whiteboard_id,
        "title": str(node.get("title") or title),
        "space_id": sid,
        "folder": preview.get("preview") or preview.get("path") or "",
        "created": created,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "nodes": written,
    }
    history.insert(
        0,
        {
            "url": wiki_row["url"],
            "node_token": node_token,
            "title": wiki_row["title"],
            "folder": wiki_row["folder"],
            "nodes": written,
            "created": created,
            "at": wiki_row["updated_at"],
            "retired": False,
            "invalid": False,
            "dialogue": _latest_mindmap_dialogue(req),
            "mindmap_snapshot": _snapshot_mindmap(req),
            "space_id": sid,
            "obj_token": obj_token,
        },
    )
    del history[_WIKI_HISTORY_MAX:]

    next_req = dict(req)
    next_req["mindmap_wiki"] = wiki_row
    next_req["mindmap_wiki_history"] = history
    idx = next(i for i, r in enumerate(reqs) if str(r.get("id") or "") == rid)
    reqs[idx] = next_req
    doc["requirements"] = reqs
    return {
        "qa_process": doc,
        "wiki": wiki_row,
        "history": history,
        "created": created,
        "url": wiki_row["url"],
        "title": wiki_row["title"],
        "nodes": written,
        "obj_type": "docx",
    }
