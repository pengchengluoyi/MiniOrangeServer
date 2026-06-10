# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
Figma 设计稿结构同步。

认证方式：普通 Figma 账号的 Personal Access Token（Settings → Security → Generate new token），
勾选 file_content:read 即可，**不需要** Figma Developer OAuth 应用或开发者账号。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from script.log import SLog
from server.services import system_settings_service as ss

TAG = "FigmaService"

# 本地冷却：遭遇硬限流后短期内不再请求 Figma
_FIGMA_COOLDOWN_UNTIL = 0.0
_MAX_RETRY_SLEEP_SEC = 90
_ABORT_RETRY_AFTER_SEC = 300


def _figma_cooldown_remaining() -> int:
    rem = _FIGMA_COOLDOWN_UNTIL - time.time()
    return int(rem) if rem > 0 else 0


def figma_cooldown_remaining() -> int:
    """距 Figma 本地冷却结束还剩多少秒（0 表示可请求）。"""
    return _figma_cooldown_remaining()


def _mark_figma_cooldown(seconds: int) -> None:
    global _FIGMA_COOLDOWN_UNTIL
    _FIGMA_COOLDOWN_UNTIL = time.time() + max(30, min(int(seconds), 3600))


def _retry_wait_seconds(retry_after_header: Optional[str], attempt: int) -> Tuple[int, bool]:
    """
    解析 Retry-After。若值过大（如数日），不应 sleep，直接中止重试。
    返回 (等待秒数, 是否应放弃重试)。
    """
    try:
        raw = int(retry_after_header) if retry_after_header else 0
    except (TypeError, ValueError):
        raw = 0
    if raw > _ABORT_RETRY_AFTER_SEC:
        return 0, True
    if raw > 0:
        return min(raw, _MAX_RETRY_SLEEP_SEC), False
    return min(_MAX_RETRY_SLEEP_SEC, 5 * (2 ** attempt)), False

FIGMA_FILE_URL_RE = re.compile(
    r"figma\.com/(?:file|design)/([a-zA-Z0-9]+)",
    re.I,
)

FIGMA_API = "https://api.figma.com/v1"
FIGMA_TOKEN_PREFIX = "figd_"
FIGMA_FILE_KEY_RE = re.compile(r"^[a-zA-Z0-9]{8,64}$")


def parse_file_key(file_url: str = "", file_key: str = "") -> str:
    """
    从 design/file 链接解析 file_key。
    若 File Key 误填为 PAT（figd_...），自动改用链接中的 ID。
    """
    url_key = ""
    m = FIGMA_FILE_URL_RE.search(file_url or "")
    if m:
        url_key = m.group(1)

    raw = (file_key or "").strip()
    if raw.lower().startswith(FIGMA_TOKEN_PREFIX):
        if url_key:
            return url_key
        raise ValueError(
            "File Key 不能填写 Personal Access Token（figd_...）。"
            "Token 请在「应用与环境 → 知识库」配置；此处留空，系统会从链接解析。"
        )

    if raw and FIGMA_FILE_KEY_RE.match(raw):
        return raw
    if url_key:
        return url_key
    return raw


def figma_get(
    url: str,
    *,
    token: Optional[str] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 90,
    retries: int = 3,
) -> requests.Response:
    """Figma GET，遇 429 短暂退避重试；Retry-After 过大时立即失败并进入本地冷却。"""
    cooldown = _figma_cooldown_remaining()
    if cooldown > 0:
        raise ValueError(
            f"Figma API 处于限流冷却期，请约 {cooldown} 秒后再试（避免重复触发 429）"
        )

    headers = _headers(token)
    last_resp: Optional[requests.Response] = None
    for attempt in range(max(1, retries)):
        resp = requests.get(url, headers=headers, params=params, timeout=timeout)
        last_resp = resp
        if resp.status_code != 429:
            return resp

        retry_after = resp.headers.get("Retry-After")
        wait, abort = _retry_wait_seconds(retry_after, attempt)
        if abort:
            cool = min(int(retry_after) if retry_after and str(retry_after).isdigit() else 600, 3600)
            _mark_figma_cooldown(cool)
            SLog.w(
                TAG,
                f"Figma 429 hard rate limit Retry-After={retry_after!r}, "
                f"local cooldown {cool}s (no long sleep)",
            )
            return resp

        if attempt >= retries - 1:
            _mark_figma_cooldown(120)
            return resp

        SLog.w(TAG, f"Figma rate limited (429), retry in {wait}s attempt={attempt + 1}")
        time.sleep(wait)

    if last_resp is not None:
        return last_resp
    raise RuntimeError("Figma request failed without response")


def _headers(token: Optional[str] = None) -> Dict[str, str]:
    tok = (token or ss.get_figma_access_token() or "").strip()
    if not tok:
        raise ValueError(
            "未配置 Figma Token。请在「应用与环境 → 知识库」填写 Personal Access Token。"
            "普通 Figma 账号即可生成，无需 Developer OAuth 应用。"
        )
    return {"X-Figma-Token": tok}


def test_figma_token(token: Optional[str] = None) -> Dict[str, Any]:
    """验证 Token 是否有效，返回当前 Figma 用户信息。"""
    resp = requests.get(f"{FIGMA_API}/me", headers=_headers(token), timeout=30)
    if resp.status_code == 403:
        raise ValueError("Token 无效或 scope 不足，请勾选 file_content:read 后重新生成")
    if not resp.ok:
        raise ValueError(f"Figma 验证失败 ({resp.status_code})")
    data = resp.json() or {}
    return {
        "ok": True,
        "email": data.get("email") or "",
        "handle": data.get("handle") or "",
        "id": data.get("id") or "",
    }


def _collect_frame_names(node: Dict[str, Any], depth: int = 0, limit: int = 12) -> List[str]:
    names: List[str] = []
    if not isinstance(node, dict):
        return names
    ntype = node.get("type") or ""
    name = (node.get("name") or "").strip()
    if ntype in ("FRAME", "COMPONENT", "COMPONENT_SET", "SECTION") and name:
        names.append(name)
    if depth >= 3 or len(names) >= limit:
        return names
    for child in node.get("children") or []:
        if len(names) >= limit:
            break
        names.extend(_collect_frame_names(child, depth + 1, limit - len(names)))
    return names


def _walk_node(
    node: Dict[str, Any],
    *,
    depth: int = 0,
    max_depth: int = 6,
    texts: Optional[List[str]] = None,
    frames: Optional[List[Dict[str, Any]]] = None,
) -> None:
    if not isinstance(node, dict):
        return
    if texts is None:
        texts = []
    if frames is None:
        frames = []

    ntype = node.get("type") or ""
    name = (node.get("name") or "").strip()

    if ntype == "TEXT":
        chars = (node.get("characters") or "").strip()
        if chars and len(chars) >= 2:
            texts.append(chars)
    elif ntype in ("FRAME", "COMPONENT", "COMPONENT_SET", "SECTION") and name:
        frame_texts: List[str] = []
        for child in node.get("children") or []:
            _walk_node(child, depth=depth + 1, max_depth=max_depth, texts=frame_texts, frames=None)
        frames.append({"name": name, "figma_id": node.get("id"), "texts": frame_texts[:16]})

    if depth >= max_depth:
        return
    for child in node.get("children") or []:
        _walk_node(child, depth=depth + 1, max_depth=max_depth, texts=texts, frames=frames)


def _keywords_for_page(name: str, texts: List[str], frames: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    if name:
        out.append(name)
    for t in texts[:8]:
        if t not in out:
            out.append(t)
    for f in frames[:6]:
        fn = (f.get("name") or "").strip()
        if fn and fn not in out:
            out.append(fn)
    return out[:20]


def parse_figma_document(data: Dict[str, Any], *, max_depth: int = 6) -> Dict[str, Any]:
    """解析 Figma /files 响应为应用逻辑结构。"""
    doc = data.get("document") or {}
    file_name = (data.get("name") or "").strip()
    pages: List[Dict[str, Any]] = []
    pages_summary: List[str] = []
    page_count = 0
    frame_total = 0

    for page in doc.get("children") or []:
        if (page.get("type") or "") != "CANVAS":
            continue
        page_count += 1
        page_name = (page.get("name") or f"Page {page_count}").strip()
        texts: List[str] = []
        frames: List[Dict[str, Any]] = []
        _walk_node(page, depth=0, max_depth=max_depth, texts=texts, frames=frames)
        frame_total += len(frames)
        texts = list(dict.fromkeys(texts))[:32]
        keywords = _keywords_for_page(page_name, texts, frames)
        slug = re.sub(r"\W+", "_", page_name)[:32] or f"page_{page_count}"
        pages.append(
            {
                "figma_id": page.get("id"),
                "node_id": f"figma_{slug}",
                "name": page_name,
                "frames": frames[:20],
                "texts": texts,
                "keywords": keywords,
            }
        )
        frame_names = [f.get("name") for f in frames if f.get("name")]
        if frame_names:
            preview = "、".join(frame_names[:8])
            if len(frame_names) > 8:
                preview += f" 等 {len(frame_names)} 个"
            pages_summary.append(f"「{page_name}」: {preview}")
        else:
            pages_summary.append(f"「{page_name}」: （无 Frame）")

    if file_name:
        pages_summary.insert(0, f"文件：{file_name}（{page_count} 个页面，约 {frame_total} 个 Frame）")
    if not pages_summary:
        pages_summary.append("文件已读取，但未解析到 CANVAS 页面")

    return {
        "file_name": file_name,
        "page_count": page_count,
        "frame_count": frame_total,
        "pages_summary": pages_summary,
        "logic": {
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "source": "figma_api",
            "pages": pages,
        },
    }


def sync_figma_file(
    *,
    file_url: str = "",
    file_key: str = "",
    depth: int = 4,
    token: Optional[str] = None,
    include_raw_document: bool = False,
) -> Dict[str, Any]:
    """
    调用 Figma REST API 拉取文件结构，并生成 logic.pages 供应用逻辑学习。
    include_raw_document=True 时附带原始 JSON，供登录图标提取复用（勿写入 app.env）。
    """
    key = parse_file_key(file_url, file_key)
    if not key:
        raise ValueError("无法从链接解析 file_key，请检查 Figma 文件 URL")

    resp = figma_get(
        f"{FIGMA_API}/files/{key}",
        token=token,
        params={"depth": max(1, min(int(depth), 8))},
        timeout=120,
    )
    if resp.status_code == 403:
        raise ValueError(
            "Figma Token 无效或无权访问该文件。"
            "请确认：1) Token 勾选 file_content:read；2) 该账号已被邀请查看此设计稿"
        )
    if resp.status_code == 404:
        raise ValueError("Figma 文件不存在或 file_key 错误")
    if resp.status_code == 429:
        ra = resp.headers.get("Retry-After")
        raise ValueError(
            "Figma API 配额/频率已触顶 (429)。"
            + (f" 官方建议等待约 {ra} 秒后再试。" if ra else " 请 10–30 分钟后再试。")
            + " 期间请勿重复点「同步/导入」。"
        )
    if not resp.ok:
        detail = ""
        try:
            detail = resp.json().get("err") or resp.json().get("message") or ""
        except Exception:
            detail = resp.text[:200]
        raise ValueError(f"Figma API 请求失败 ({resp.status_code}): {detail or 'unknown'}")

    raw = resp.json() or {}
    parsed = parse_figma_document(raw, max_depth=depth)
    out: Dict[str, Any] = {
        "file_key": key,
        "file_url": (file_url or "").strip() or f"https://www.figma.com/design/{key}",
        "last_sync_at": datetime.now(timezone.utc).isoformat(),
        "pages_summary": parsed["pages_summary"],
        "page_count": parsed["page_count"],
        "frame_count": parsed["frame_count"],
        "logic": parsed["logic"],
    }
    if include_raw_document:
        out["raw_document"] = raw
    return out


def fetch_figma_nodes(
    file_key: str,
    node_ids: List[str],
    *,
    depth: int = 6,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """按节点 ID 拉取子树（比整文件更省配额）。"""
    key = (file_key or "").strip()
    ids = [i for i in node_ids if i]
    if not key or not ids:
        raise ValueError("缺少 file_key 或 node_ids")
    resp = figma_get(
        f"{FIGMA_API}/files/{key}/nodes",
        token=token,
        params={"ids": ",".join(ids), "depth": max(1, min(int(depth), 10))},
        timeout=120,
    )
    if resp.status_code == 429:
        raise ValueError("Figma API 请求过于频繁 (429)，请 1–2 分钟后再试")
    if not resp.ok:
        raise ValueError(f"Figma 节点读取失败 ({resp.status_code})")
    return resp.json() or {}
