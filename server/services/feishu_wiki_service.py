# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""飞书 Wiki：读空间、列节点、建调试页。正式按版本落副本还没接到流程上。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

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


def preview_path(*, folder_pattern: str, children: list | None = None, project: str = "MiniOrange", version: str = "调试") -> dict:
    pattern = str(folder_pattern or "{project}/版本/{version}").strip() or "{project}/版本/{version}"
    path = pattern.replace("{project}", project).replace("{version}", version)
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


def _find_child(nodes: list[dict], title: str) -> dict | None:
    want = str(title or "").strip()
    for row in nodes:
        if str(row.get("title") or "").strip() == want:
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
