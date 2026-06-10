# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""服务端全局配置（存于 config.json）。"""
from __future__ import annotations

import os
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from server.core.security import SecurityManager


def _feishu_root() -> Dict[str, Any]:
    SecurityManager.load()
    fb = SecurityManager._config.get("feishu")
    if not isinstance(fb, dict):
        return {}
    return fb


def _migrate_legacy_feishu(feishu: Dict[str, Any]) -> List[Dict[str, Any]]:
    """单机器人旧配置 → bots 列表。"""
    bots = feishu.get("bots")
    if isinstance(bots, list) and bots:
        return bots
    app_id = (feishu.get("app_id") or os.environ.get("FEISHU_APP_ID", "")).strip()
    secret = (feishu.get("app_secret") or os.environ.get("FEISHU_APP_SECRET", "")).strip()
    if app_id or secret:
        return [
            {
                "id": "default",
                "name": "默认机器人",
                "app_id": app_id,
                "app_secret": secret,
            }
        ]
    return []


def _mask_secret(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return secret[:4] + "****" + secret[-4:]


def _bot_public(bot: Dict[str, Any]) -> Dict[str, Any]:
    secret = (bot.get("app_secret") or "").strip()
    app_id = (bot.get("app_id") or "").strip()
    return {
        "id": bot.get("id") or "",
        "name": bot.get("name") or "未命名",
        "app_id": app_id,
        "app_secret_masked": _mask_secret(secret),
        "configured": bool(app_id and secret),
    }


def _save_bots(bots: List[Dict[str, Any]]) -> None:
    SecurityManager.load()
    feishu = SecurityManager._config.setdefault("feishu", {})
    feishu["bots"] = bots
    feishu.pop("app_id", None)
    feishu.pop("app_secret", None)
    SecurityManager.save()


def list_feishu_bots() -> List[Dict[str, Any]]:
    feishu = _feishu_root()
    bots = _migrate_legacy_feishu(feishu)
    if bots and not feishu.get("bots"):
        _save_bots(bots)
    return [_bot_public(b) for b in bots]


def get_feishu_bot(bot_id: str) -> Optional[Dict[str, Any]]:
    feishu = _feishu_root()
    for bot in _migrate_legacy_feishu(feishu):
        if str(bot.get("id")) == str(bot_id):
            return bot
    return None


def get_feishu_credentials(bot_id: Optional[str] = None) -> Tuple[str, str]:
    """飞书 API 鉴权；未指定 bot_id 时使用第一个已配置机器人。"""
    feishu = _feishu_root()
    bots = _migrate_legacy_feishu(feishu)
    target = None
    if bot_id:
        target = get_feishu_bot(bot_id)
        if not target:
            raise RuntimeError(f"未找到飞书机器人 id={bot_id}")
    else:
        for bot in bots:
            if (bot.get("app_id") or "").strip() and (bot.get("app_secret") or "").strip():
                target = bot
                break
    if target:
        return (target.get("app_id") or "").strip(), (target.get("app_secret") or "").strip()
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    return app_id, app_secret


def get_feishu_bot_settings() -> Dict[str, Any]:
    """兼容旧接口：返回首个机器人摘要。"""
    bots = list_feishu_bots()
    if not bots:
        return {
            "app_id": "",
            "app_secret_masked": "",
            "configured": False,
            "source": "none",
            "bots": [],
        }
    first = bots[0]
    return {
        "app_id": first.get("app_id", ""),
        "app_secret_masked": first.get("app_secret_masked", ""),
        "configured": first.get("configured", False),
        "source": "server",
        "bots": bots,
    }


def create_feishu_bot(*, name: str, app_id: str, app_secret: str) -> Dict[str, Any]:
    feishu = _feishu_root()
    bots = _migrate_legacy_feishu(feishu)
    if not (app_id or "").strip():
        raise ValueError("App ID 不能为空")
    if not (app_secret or "").strip():
        raise ValueError("App Secret 不能为空")
    bot = {
        "id": uuid.uuid4().hex[:12],
        "name": (name or "飞书机器人").strip() or "飞书机器人",
        "app_id": str(app_id).strip(),
        "app_secret": str(app_secret).strip(),
    }
    bots.append(bot)
    _save_bots(bots)
    return _bot_public(bot)


def update_feishu_bot(
    bot_id: str,
    *,
    name: Optional[str] = None,
    app_id: Optional[str] = None,
    app_secret: str = "",
    clear_secret: bool = False,
) -> Dict[str, Any]:
    feishu = _feishu_root()
    bots = _migrate_legacy_feishu(feishu)
    found = None
    for bot in bots:
        if str(bot.get("id")) == str(bot_id):
            found = bot
            break
    if not found:
        raise ValueError(f"机器人不存在: {bot_id}")
    if name is not None:
        found["name"] = str(name).strip() or found.get("name")
    if app_id is not None:
        found["app_id"] = str(app_id).strip()
    if clear_secret:
        found.pop("app_secret", None)
    elif app_secret and str(app_secret).strip():
        found["app_secret"] = str(app_secret).strip()
    _save_bots(bots)
    return _bot_public(found)


def delete_feishu_bot(bot_id: str) -> None:
    feishu = _feishu_root()
    bots = _migrate_legacy_feishu(feishu)
    new_bots = [b for b in bots if str(b.get("id")) != str(bot_id)]
    if len(new_bots) == len(bots):
        raise ValueError(f"机器人不存在: {bot_id}")
    _save_bots(new_bots)


def save_feishu_bot_settings(
    *,
    app_id: str,
    app_secret: str = "",
    clear_secret: bool = False,
) -> Dict[str, Any]:
    """兼容旧单机器人 PUT：更新或创建 default。"""
    bots = list_feishu_bots()
    if bots:
        return update_feishu_bot(
            bots[0]["id"],
            app_id=app_id,
            app_secret=app_secret,
            clear_secret=clear_secret,
        )
    if clear_secret:
        return get_feishu_bot_settings()
    return create_feishu_bot(name="默认机器人", app_id=app_id, app_secret=app_secret)


def _testing_root() -> Dict[str, Any]:
    SecurityManager.load()
    root = SecurityManager._config.setdefault("testing", {})
    if not isinstance(root, dict):
        root = {}
        SecurityManager._config["testing"] = root
    return root


def list_testing_knowledge() -> List[Dict[str, Any]]:
    root = _testing_root()
    items = root.get("knowledge") or []
    if not isinstance(items, list):
        return []
    return [dict(x) for x in items if isinstance(x, dict)]


def save_testing_knowledge(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    root = _testing_root()
    cleaned: List[Dict[str, Any]] = []
    for raw in items or []:
        if not isinstance(raw, dict):
            continue
        title = (raw.get("title") or "").strip()
        content = (raw.get("content") or "").strip()
        if not title or not content:
            continue
        tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()]
        app_ids = [str(a).strip() for a in (raw.get("app_ids") or []) if str(a).strip()]
        category = (raw.get("category") or "").strip() or "其他"
        cleaned.append(
            {
                "id": (raw.get("id") or uuid.uuid4().hex[:12]),
                "title": title,
                "content": content,
                "category": category,
                "tags": tags,
                "app_ids": app_ids,
                "enabled": raw.get("enabled", True) is not False,
            }
        )
    root["knowledge"] = cleaned
    SecurityManager.save()
    return cleaned


def match_testing_knowledge(
    text: str,
    *,
    app_id: Optional[str] = None,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """按关键词/标签匹配知识条目，供规划与语义层引用。"""
    query = (text or "").strip().lower()
    if not query:
        return []
    hits: List[tuple] = []
    for item in list_testing_knowledge():
        if item.get("enabled") is False:
            continue
        app_ids = item.get("app_ids") or []
        if app_ids and app_id and str(app_id) not in [str(x) for x in app_ids]:
            continue
        title = (item.get("title") or "").lower()
        content = (item.get("content") or "").lower()
        tags = [str(t).lower() for t in (item.get("tags") or [])]
        score = 0
        matched_tokens = 0
        category = (item.get("category") or "").lower()
        stop = {
            "点击", "输入", "勾选", "页面", "步骤", "进行", "成功", "失败",
            "登录", "打开", "关闭", "测试", "用例", "操作", "验证", "检查",
        }
        for token in re.split(r"[\s,，、/]+", query):
            if len(token) < 2 or token in stop:
                continue
            if len(token) < 3 and not re.search(r"[a-zA-Z]{3,}", token):
                continue
            local = 0
            if token in title:
                local += 8
            if token in content:
                local += 4
            if category and token in category:
                local += 5
            for tag in tags:
                if token in tag or tag in token:
                    local += 6
            if local > 0:
                score += local
                matched_tokens += 1
        if score >= 10 and matched_tokens >= 1:
            hits.append((score, item))
    hits.sort(key=lambda x: x[0], reverse=True)
    return [h[1] for h in hits[:limit]]


def get_figma_access_token() -> str:
    root = _testing_root()
    figma = root.get("figma") or {}
    if not isinstance(figma, dict):
        return ""
    return (figma.get("access_token") or "").strip()


def get_figma_settings() -> Dict[str, Any]:
    root = _testing_root()
    figma = root.get("figma") or {}
    if not isinstance(figma, dict):
        figma = {}
    token = get_figma_access_token()
    return {
        "access_token_masked": _mask_secret(token),
        "configured": bool(token),
        "default_file_url": (figma.get("default_file_url") or "").strip(),
    }


def save_figma_settings(
    *,
    access_token: str = "",
    clear_token: bool = False,
    default_file_url: str = "",
) -> Dict[str, Any]:
    root = _testing_root()
    figma = root.setdefault("figma", {})
    if clear_token:
        figma.pop("access_token", None)
    elif access_token and str(access_token).strip():
        figma["access_token"] = str(access_token).strip()
    if default_file_url is not None:
        figma["default_file_url"] = str(default_file_url or "").strip()
    SecurityManager.save()
    return get_figma_settings()
