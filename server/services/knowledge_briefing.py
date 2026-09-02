# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""本步简报：把已通过知识、图谱、当前需求编成带引用的一包。

模型吃编译结果，不吃某条原文。冲突时以当前屏幕为准。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from script.log import SLog

from server.services.knowledge_facts import DOC_SOURCES, item_is_live
from server.services.knowledge_situation import (
    normalize_bind,
    normalize_facet,
    normalize_situation,
    route_knowledge,
)
from server.services.system_settings_service import (
    dedupe_knowledge_hits,
    knowledge_body_text,
    knowledge_prompt_snippet,
)

TAG = "KnowledgeBriefing"
BRIEFING_MAX = 2800
CHROME_BODY_MAX = 360
HOWTO_BODY_MAX = 420
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}|[a-zA-Z0-9_]{3,}")

SYNTH_SYSTEM = """You compile a briefing for a UI test agent from cited sources.
Return JSON: {"briefing": "..."}.
Rules:
- Write 6-12 short lines in the same language as the sources.
- Every factual line must end with a citation like 〔id〕.
- Do not invent. Do not copy a single source wholesale.
- If sources conflict, say so and keep both citations.
- The live screenshot outranks this briefing.
- Do not assume a particular product.
"""


@dataclass
class Briefing:
    text: str
    citations: List[Dict[str, str]] = field(default_factory=list)
    knowledge: List[Dict[str, Any]] = field(default_factory=list)
    cache_key: str = ""


def briefing_cache_key(
    app_id: str,
    scene: Optional[Dict[str, Any]],
    *,
    query: str = "",
    extra: str = "",
    app_version: str = "",
    env_profile: str = "",
) -> str:
    scene = scene if isinstance(scene, dict) else {}
    bits = [
        str(app_id or ""),
        str(app_version or ""),
        str(env_profile or ""),
        str(scene.get("surface") or ""),
        str(scene.get("lane") or ""),
        str(scene.get("need") or ""),
        str(scene.get("facet") or ""),
        str(scene.get("slot") or ""),
        str(scene.get("screen_role") or ""),
        hashlib.sha1(f"{query}\n{extra}".encode("utf-8")).hexdigest()[:12],
    ]
    return "|".join(bits)


def _tokens(text: str) -> List[str]:
    found: List[str] = []
    seen: set[str] = set()
    for m in _TOKEN_RE.finditer(str(text or "").lower()):
        t = m.group(0)
        if t not in seen:
            seen.add(t)
            found.append(t)
        if len(found) >= 32:
            break
    return found


def _overlap(blob: str, query: str) -> int:
    if not query or not blob:
        return 0
    low = blob.lower()
    return sum(1 for t in _tokens(query) if t in low)


def _cite(kind: str, cid: str, title: str) -> Dict[str, str]:
    return {"kind": kind, "id": str(cid or "").strip(), "title": str(title or "").strip()}


def load_app_corpus(app_id: str) -> Dict[str, Any]:
    """图谱路径 + 需求摘要。读失败返回空语料，不编造。"""
    aid = str(app_id or "").strip()
    empty = {"app_id": aid, "app_name": "", "atlas_paths": [], "requirements": []}
    if not aid:
        return empty
    try:
        from sqlalchemy.orm import joinedload

        from server.core.database import SessionLocal
        from server.models.project import App
        from server.services.ai.app_atlas import flatten_tree
        from server.services.app_automation_service import get_automation_config

        db = SessionLocal()
        try:
            app = db.query(App).options(joinedload(App.project)).filter(App.id == aid).first()
            if not app:
                return empty
            cfg = get_automation_config(app)
            qp = cfg.get("qa_process") or {}
            paths = []
            for item in flatten_tree(qp.get("app_atlas") or {})[:80]:
                path = str(item.get("path") or item.get("name") or "").strip()
                if path:
                    paths.append({"id": str(item.get("id") or ""), "path": path, "kind": str(item.get("kind") or "")})
            reqs: List[Dict[str, str]] = []
            for req in qp.get("requirements") or []:
                if not isinstance(req, dict):
                    continue
                title = str(req.get("title") or req.get("external_id") or "").strip()
                if not title:
                    continue
                und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
                ac = und.get("ac") if isinstance(und.get("ac"), list) else []
                ac0 = str(ac[0] or "").strip()[:80] if ac else ""
                excerpt = str(und.get("source_excerpt") or "").strip().replace("\n", " ")[:80]
                reqs.append({
                    "id": str(req.get("id") or req.get("external_id") or title)[:40],
                    "title": title,
                    "ac": ac0,
                    "excerpt": excerpt,
                })
                if len(reqs) >= 16:
                    break
            return {
                "app_id": aid,
                "app_name": str(app.name or ""),
                "atlas_paths": paths,
                "requirements": reqs,
            }
        finally:
            db.close()
    except Exception as exc:
        SLog.w(TAG, f"load_app_corpus failed {aid}: {exc}")
        return empty


def collect_scene_knowledge(
    app_id: str,
    scene: Optional[Dict[str, Any]],
    *,
    query: str = "",
    app_version: str = "",
) -> List[Dict[str, Any]]:
    """按情境拉知识：壳层不靠和用例撞词。"""
    scene = dict(scene or {})
    surface = str(scene.get("surface") or "")
    lane = str(scene.get("lane") or "")
    need = str(scene.get("need") or "")

    def _route(text: str = "", **kw):
        kw.setdefault("app_id", app_id)
        kw.setdefault("app_version", app_version)
        return route_knowledge(text, **kw)

    batches: List[List[Dict[str, Any]]] = []
    batches.append(_route(
        "",
        scene={
            "facet": "chrome",
            "need": "judge_selected",
            "screen_role": "chrome_nav",
            "surface": surface,
            "lane": "expect",
        },
        limit=4,
    ))
    if lane == "prep" or need in ("howto", "fill"):
        batches.append(_route(
            "",
            scene={"lane": "prep", "need": "howto", "surface": surface},
            limit=6,
        ))
        batches.append(_route(
            "",
            scene={"lane": "prep", "need": "fill", "slot": "identity.otp", "surface": surface, "screen_role": "auth_form"},
            limit=3,
        ))
    if lane == "expect" or need in ("judge", "judge_selected"):
        batches.append(_route(
            "",
            scene={"lane": "expect", "need": need or "judge", "surface": surface, "facet": scene.get("facet") or "chrome"},
            limit=4,
        ))
    if lane == "prep" or need == "howto":
        batches.append(_legacy_session_rows(app_id, app_version=app_version))
    if query.strip():
        batches.append(_route(query, scene=scene, limit=4))
    merged: List[Dict[str, Any]] = []
    for batch in batches:
        merged.extend(batch or [])
    unique = dedupe_knowledge_hits(merged)
    return [r for r in unique if r.get("used") is not False and item_is_live(r, app_version)]


def _legacy_session_rows(app_id: str, app_version: str = "") -> List[Dict[str, Any]]:
    """未打情境卡的登录/退出旧条目，按说明书槽位标题召回。"""
    try:
        from server.services.system_settings_service import match_testing_knowledge

        rows = list(match_testing_knowledge(
            "如何登录 如何退出登录 如何判断登录态 身份页 访客浏览",
            app_id=app_id,
            limit=8,
            categories=["登录注册", "应用基础逻辑"],
        ) or [])
        return [r for r in rows if item_is_live(r, app_version)]
    except Exception as exc:
        SLog.d(TAG, f"legacy session rows skipped: {exc}")
        return []


def _playbook_nav_lines(playbook: Optional[dict]) -> List[str]:
    """只给底栏/分段槽名单，不含独立入口口诀。"""
    if not isinstance(playbook, dict):
        return []
    lines: List[str] = []
    tabs = [str(x).strip() for x in (playbook.get("bottom_tabs") or []) if str(x).strip()]
    segs = [str(x).strip() for x in (playbook.get("segment_tabs") or []) if str(x).strip()]
    if tabs:
        lines.append("底栏槽：" + "、".join(tabs[:12]))
    if segs:
        lines.append("顶部分段：" + "、".join(segs[:12]))
    return lines


def _clip(text: str, n: int) -> str:
    t = (text or "").strip()
    if len(t) <= n:
        return t
    return t[:n].rstrip() + "…"


def assemble_briefing(
    *,
    knowledge: List[Dict[str, Any]],
    corpus: Optional[Dict[str, Any]] = None,
    playbook: Optional[dict] = None,
    case_intent: str = "",
    scene: Optional[Dict[str, Any]] = None,
    app_version: str = "",
) -> Briefing:
    corpus = corpus if isinstance(corpus, dict) else {}
    scene = scene if isinstance(scene, dict) else {}
    citations: List[Dict[str, str]] = []
    chrome_lines: List[str] = []
    bind_lines: List[str] = []
    howto_lines: List[str] = []
    other_lines: List[str] = []
    doc_lines: List[str] = []
    used_ids: set[str] = set()

    for row in knowledge or []:
        if not item_is_live(row, app_version):
            continue
        kid = str(row.get("id") or "").strip()
        title = str(row.get("title") or "").strip() or kid
        facet = normalize_facet(row.get("facet"))
        sit = normalize_situation(row.get("situation"))
        bind = normalize_bind(row.get("bind"))
        body = knowledge_body_text(row).strip()
        need = sit.get("need") or ""
        cid = kid or title
        mark = f"〔{cid}〕" if cid else ""
        src = str(row.get("source") or "").strip()
        if bind.get("slot") and bind.get("value"):
            bind_lines.append(f"{bind['slot']} 〔已配置，由资源网关填写〕{mark}")
            citations.append(_cite("knowledge", cid, title))
            used_ids.add(cid)
        is_chrome = facet == "chrome" or need == "judge_selected" or sit.get("screen_role") == "chrome_nav"
        if is_chrome and body:
            chrome_lines.append(_clip(body, CHROME_BODY_MAX) + mark)
            citations.append(_cite("knowledge", cid, title))
            used_ids.add(cid)
            continue
        if src in DOC_SOURCES and body:
            doc_lines.append(_clip(body, 160) + mark)
            citations.append(_cite(src, cid, title))
            used_ids.add(cid)
            continue
        if bind.get("value") and need == "fill":
            continue
        if need == "howto" or sit.get("lane") == "prep":
            bit = knowledge_prompt_snippet(row, max_chars=HOWTO_BODY_MAX) if body or bind else ""
            if bit:
                howto_lines.append(bit + ("" if mark in bit else mark))
                citations.append(_cite("knowledge", cid, title))
                used_ids.add(cid)
            continue
        if body and cid not in used_ids:
            other_lines.append(_clip(body, 280) + mark)
            citations.append(_cite("knowledge", cid, title))
            used_ids.add(cid)

    if str(scene.get("lane") or "") == "prep" or str(scene.get("need") or "") in ("howto", "fill"):
        try:
            from server.services.ai.playbook_service import session_howto_block

            howto = session_howto_block(playbook)
            if howto:
                howto_lines.insert(0, _clip(howto, 900) + "〔playbook〕")
                citations.append(_cite("playbook", "playbook-session", "登录退出"))
        except Exception:
            pass

    for line in _playbook_nav_lines(playbook):
        chrome_lines.append(line + "〔playbook〕")
        citations.append(_cite("playbook", "playbook", "应用说明书导航槽"))

    try:
        from server.services.ai.playbook_service import env_howto_block

        env_how = env_howto_block(playbook)
        intent = str(case_intent or "")
        env_intent = "环境" in intent or "env" in intent.lower()
        if env_how and env_intent:
            howto_lines.insert(0, _clip(env_how, 420) + "〔playbook〕")
            citations.append(_cite("playbook", "playbook-env", "切换环境"))
    except Exception:
        pass

    atlas_lines: List[str] = []
    intent = case_intent or ""
    for item in corpus.get("atlas_paths") or []:
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        score = _overlap(path, intent) if intent else 1
        if score <= 0:
            continue
        aid = str(item.get("id") or path)[:24]
        atlas_lines.append(f"{path}〔{aid}〕")
        citations.append(_cite("atlas", aid, path))
        if len(atlas_lines) >= (8 if intent else 12):
            break
    if not atlas_lines and not intent:
        for item in (corpus.get("atlas_paths") or [])[:12]:
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            aid = str(item.get("id") or path)[:24]
            atlas_lines.append(f"{path}〔{aid}〕")
            citations.append(_cite("atlas", aid, path))

    req_lines: List[str] = []
    if not doc_lines:
        for req in corpus.get("requirements") or []:
            title = str(req.get("title") or "").strip()
            if not title:
                continue
            blob = " ".join(str(req.get(k) or "") for k in ("title", "ac", "excerpt"))
            if intent and _overlap(blob, intent) <= 0:
                continue
            rid = str(req.get("id") or title)[:24]
            ac = str(req.get("ac") or "").strip()
            bit = title if not ac else f"{title}：{ac}"
            req_lines.append(_clip(bit, 120) + f"〔{rid}〕")
            citations.append(_cite("requirement", rid, title))
            if len(req_lines) >= (4 if intent else 8):
                break

    parts: List[str] = ["==== 本步简报（编译结果，不是某条原文；与屏幕冲突时以屏幕为准）===="]
    app_name = str(corpus.get("app_name") or "").strip()
    if app_name:
        parts.append(f"应用：{app_name}")
    if app_version:
        parts.append(f"版本：as-of {app_version}")
    env_key = str(corpus.get("env_key") or "").strip()
    env_label = str(corpus.get("env_label") or "").strip()
    if env_key or env_label:
        if env_label and env_key and env_label != env_key:
            parts.append(f"环境：{env_label}（{env_key}）")
        else:
            parts.append(f"环境：{env_label or env_key}")
    if chrome_lines:
        parts.append("【壳层】")
        parts.extend(f"- {x}" for x in chrome_lines[:6])
    if bind_lines:
        parts.append("【绑定】当前屏在问对应字段时 input_text 并带 field，值由资源网关填，不要问人，不要写出口令。")
        parts.extend(f"- {x}" for x in bind_lines[:4])
    if howto_lines:
        parts.append("【路径】")
        parts.extend(f"- {x}" for x in howto_lines[:5])
    if other_lines and str(scene.get("lane") or "") != "expect":
        parts.append("【其它】")
        parts.extend(f"- {x}" for x in other_lines[:3])
    if atlas_lines:
        parts.append("【图谱】")
        parts.extend(f"- {x}" for x in atlas_lines[:8])
    if doc_lines:
        parts.append("【文档】")
        parts.extend(f"- {x}" for x in doc_lines[:6])
    elif req_lines:
        parts.append("【需求】")
        parts.extend(f"- {x}" for x in req_lines[:6])

    seen = set()
    cite_bits: List[str] = []
    unique_cites: List[Dict[str, str]] = []
    for c in citations:
        key = f"{c.get('kind')}:{c.get('id')}"
        if key in seen or not c.get("id"):
            continue
        seen.add(key)
        unique_cites.append(c)
        title = c.get("title") or ""
        cite_bits.append(f"{c['id']}「{title}」" if title else c["id"])
    if cite_bits:
        parts.append("出处：" + "；".join(cite_bits[:12]))
    text = "\n".join(parts).strip()
    if len(text) > BRIEFING_MAX:
        text = text[:BRIEFING_MAX].rstrip() + "…"
    if len(parts) <= 1:
        text = ""
    return Briefing(text=text, citations=unique_cites, knowledge=list(knowledge or []))


def _synthesize(packet: Briefing, scene: Optional[Dict[str, Any]]) -> Optional[str]:
    if not packet.text or not packet.citations:
        return None
    try:
        from server.services.ai.regression.llm_client import call_chat_text, resolve_regression_provider
        from server.services.ai import dispatch_log as dispatch
    except Exception as exc:
        SLog.w(TAG, f"synth import failed: {exc}")
        return None
    provider, gate = resolve_regression_provider()
    if provider is None:
        return None
    payload = {
        "scene": {k: v for k, v in (scene or {}).items() if v},
        "draft": packet.text[:2000],
        "citations": packet.citations[:12],
    }
    tok = dispatch.bind(
        trigger="knowledge_briefing",
        source="knowledge_briefing",
        role="knowledge-reviewer",
        job="knowledge-briefing",
        skill="knowledge-briefing",
    )
    try:
        raw, meta = call_chat_text(
            provider=provider,
            messages=[
                {"role": "system", "content": SYNTH_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.1,
            max_tokens=500,
            timeout_sec=25,
        )
    finally:
        dispatch.reset(tok)
    if not isinstance(raw, dict):
        SLog.w(TAG, f"synth failed: {(meta or {}).get('error')}")
        return None
    text = str(raw.get("briefing") or raw.get("text") or "").strip()
    return text[:BRIEFING_MAX] if text else None


def compile_briefing(
    app_id: str = "",
    scene: Optional[Dict[str, Any]] = None,
    *,
    query: str = "",
    case_intent: str = "",
    playbook: Optional[dict] = None,
    corpus: Optional[Dict[str, Any]] = None,
    knowledge: Optional[List[Dict[str, Any]]] = None,
    synthesize: bool = False,
    app_version: str = "",
    env_profile: str = "",
    env_label: str = "",
) -> Briefing:
    if knowledge is None:
        try:
            from server.services.knowledge_facts import ensure_app_facts

            ensure_app_facts(str(app_id or ""))
        except Exception as exc:
            SLog.w(TAG, f"ensure_app_facts skipped: {exc}")
        rows = collect_scene_knowledge(
            str(app_id or ""), scene, query=query, app_version=app_version,
        )
    else:
        rows = [r for r in knowledge if item_is_live(r, app_version)]
    corp = dict(corpus if corpus is not None else load_app_corpus(str(app_id or "")))
    if env_profile:
        corp["env_key"] = env_profile
    if env_label:
        corp["env_label"] = env_label
    packet = assemble_briefing(
        knowledge=rows,
        corpus=corp,
        playbook=playbook,
        case_intent=case_intent,
        scene=scene,
        app_version=app_version,
    )
    packet.cache_key = briefing_cache_key(
        app_id, scene, query=query, extra=case_intent[:200],
        app_version=app_version, env_profile=env_profile,
    )
    if synthesize:
        fused = _synthesize(packet, scene)
        if fused:
            packet.text = fused
    return packet
