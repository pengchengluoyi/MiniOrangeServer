# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""产品角色 Job：需求分析 / 测试脑图 / 用例编写，以及流程 tick。

tick 会改 qa_process 草稿字段，不改 human_verdict，不自动下发真机任务。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from server.services.ai.roles_catalog import (
    CASE_WRITER_SYSTEM_PROMPT,
    MINDMAP_WRITER_SYSTEM_PROMPT,
    REQ_ANALYST_IMPACT_PROMPT,
    REQ_ANALYST_SYSTEM_PROMPT,
)
from server.services.ai import app_atlas as atlas
from server.services.ai import dispatch_log as dispatch
from server.services.ai.concurrency import map_llm
from server.services.ai.cover import checks as cover_checks
from server.services import qa_process_jobs as cover_jobs
from server.services.qa_process_assist import artifact

LLM_JOBS = ("analyze_req", "draft_mindmap", "draft_cases", "propose_atlas")

DEFAULT_JOBS_BY_KIND = {
    "understand": ("analyze_req", "propose_atlas"),
    "cover": ("draft_mindmap", "draft_cases"),
    "scope": (),
    "dispatch": (),
    "human_verdict": (),
    "archive": (),
    "checkpoint": (),
}


def _workflow_steps(doc: dict, track: str) -> list:
    wf = doc.get("workflow") if isinstance(doc.get("workflow"), dict) else {}
    tracks = wf.get("tracks") if isinstance(wf.get("tracks"), dict) else {}
    row = tracks.get(track) if isinstance(tracks.get(track), dict) else {}
    return [s for s in (row.get("steps") or []) if isinstance(s, dict)]


def _jobs_for_step(doc: dict, track: str, gate: str, kind: str = "") -> set[str]:
    steps = _workflow_steps(doc, track)
    step = next((s for s in steps if str(s.get("id") or "") == str(gate or "")), None)
    kind = str((step or {}).get("kind") or kind or "")
    if not kind:
        if str(gate or "") in ("read", ""):
            kind = "understand"
        elif str(gate or "") == "cases":
            kind = "cover"
    jobs: list[str] = []
    wf = doc.get("workflow") if isinstance(doc.get("workflow"), dict) else {}
    wid = str((step or {}).get("workflow_id") or "").strip()
    if wid:
        for chain in wf.get("chains") or []:
            if not isinstance(chain, dict) or str(chain.get("id") or "") != wid:
                continue
            for item in chain.get("steps") or []:
                if isinstance(item, str):
                    jobs.append(item)
                elif isinstance(item, dict):
                    jobs.append(str(item.get("capability_id") or item.get("id") or item.get("job") or ""))
            break
    if not jobs:
        for item in (step or {}).get("jobs") or []:
            if isinstance(item, str):
                jobs.append(item)
            elif isinstance(item, dict):
                jobs.append(str(item.get("capability_id") or item.get("id") or item.get("job") or ""))
    if not jobs:
        jobs = list(DEFAULT_JOBS_BY_KIND.get(kind) or [])
    return {x for x in jobs if x}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _hash(text: str) -> str:
    h = 5381
    for ch in str(text or ""):
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    return format(h, "x")


def _source_text(req: dict) -> str:
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    bits = [
        req.get("title") or "",
        req.get("external_id") or "",
        req.get("source_text") or "",
        und.get("source_excerpt") or "",
        "\n".join(str(x) for x in (und.get("ac") or []) if x),
    ]
    # 去重必须看包含关系，不能只看相等：understanding.source_excerpt 是 source_text 的
    # 前 20000 字前缀（见 apply_analyze），原文超过 20000 字时两者不相等，
    # 纯 == 去重会把同一份 PRD 拼进去两次，直接让 analyze_req 的输入 token 翻倍。
    kept: list[str] = []
    for x in bits:
        s = str(x).strip()
        if not s or any(s in k for k in kept):
            continue
        kept = [k for k in kept if k not in s]
        kept.append(s)
    return "\n".join(kept)


def _analysis_bundle(req: dict) -> dict:
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    return {
        "source_excerpt": und.get("source_excerpt") or req.get("source_text") or "",
        "summary": req.get("summary") or "",
        "change_kind": und.get("change_kind") or "",
        "baseline": und.get("baseline") or "",
        "delta": und.get("delta") or "",
        "ac": und.get("ac") or [],
        "points": und.get("points") or [],
        "impact": und.get("impact") or {},
        "journeys": und.get("journeys") or [],
        "new_features": und.get("new_features") or [],
        "keep_features": und.get("keep_features") or [],
        "exceptions": und.get("exceptions") or [],
        "surfaces": und.get("surfaces") or [],
        "analyst_feedback": str(req.get("analyst_feedback") or "").strip(),
    }


def _ask_json(
    system: str,
    user: str,
    *,
    max_tokens: int = 2500,
    timeout_sec: int = 90,
    role: str = "",
    job: str = "",
    stable: str = "",
    response_schema: Optional[dict] = None,
) -> tuple[Optional[dict], dict]:
    """问一次模型要 JSON。

    stable：逐次调用完全不变的上下文（应用画像、知识库、契约）。单独成一条 message 且
    排在易变内容之前，这样 provider 的前缀缓存才可能命中 —— 分片并发时收益被放大几十倍。

    应用事实（术语表、主导航）由这里统一从画像取并排在 stable 之前，调用方不用管：
    prompt 里的应用字面量被摘干净之后，必须有人把它补回来，否则就是把应用知识删了。
    没接入画像的应用这一段是空的，不会拿别人的事实冒充。

    返回的 meta 里带 truncated / salvaged，调用方**必须**检查：截断意味着结果不完整，
    当成功处理就会静默丢覆盖。
    """
    from server.services.ai.regression.llm_client import call_chat_text, resolve_regression_provider
    from server.services.ai import app_profile as ap

    # 取消点：正在跑的那一次模型调用会跑完，下一次进这里才停。
    cover_jobs.check()
    provider, gate = resolve_regression_provider()
    tok = dispatch.bind(role=role, job=job, skill=job)
    if not provider:
        dispatch.record_job(status="skipped", job=job or "llm", role=role, detail=gate.get("reason") or "未配置模型")
        dispatch.reset(tok)
        return None, {"error": gate.get("reason") or "未配置「可用 + 用例」模型", "engine": "none"}
    messages = [{"role": "system", "content": system}]
    # 应用事实比图谱更少变，排在它前面，缓存前缀才切得干净
    facts = ap.current().facts_prompt()
    if facts:
        messages.append({"role": "user", "content": facts})
    from server.services.system_settings_service import (
        knowledge_prompt_snippet,
        match_testing_knowledge,
    )

    knowledge_text = ""
    try:
        app_id = str((dispatch.ctx() or {}).get("app_id") or "")
        query = user if isinstance(user, str) else str(user or "")
        hits = match_testing_knowledge(
            query[:2000],
            app_id=app_id or None,
            limit=3,
        )
        knowledge_text = "\n".join(
            knowledge_prompt_snippet(item)
            for item in hits
            if item.get("used")
        )
    except Exception:
        knowledge_text = ""
    if knowledge_text:
        messages.append({"role": "user", "content": knowledge_text})
    if stable:
        messages.append({"role": "user", "content": stable})
    messages.append({"role": "user", "content": user})
    parsed, meta = call_chat_text(
        provider=provider,
        messages=messages,
        temperature=0.2,
        max_tokens=max_tokens,
        timeout_sec=timeout_sec,
        response_schema=response_schema,
    )
    dispatch.reset(tok)
    if not isinstance(parsed, dict):
        return None, {**meta, "error": meta.get("error") or "模型没有返回 JSON", "engine": "llm"}
    from server.services.ai.regression.llm_client import parse_token_usage

    usage = parse_token_usage(meta.get("usage"))
    return parsed, {**meta, "engine": "llm", **usage}


def _with_engine(art: dict, engine: str, meta: dict | None = None) -> dict:
    out = dict(art)
    out["engine"] = engine
    if meta:
        out["usage"] = {
            "prompt_tokens": int(meta.get("prompt_tokens") or 0),
            "completion_tokens": int(meta.get("completion_tokens") or 0),
            "total_tokens": int(meta.get("total_tokens") or 0),
        }
    return out


def _as_dict_list(val) -> list:
    return [x for x in (val or []) if isinstance(x, dict)]


def _rule_analyze(req: dict) -> dict:
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    text = _source_text(req)
    ac = [str(x).strip() for x in (und.get("ac") or []) if str(x).strip()]
    points = und.get("points") or []
    if not ac:
        title = str(req.get("title") or "需求").strip()
        ac = [f"{title} 主流程可完成"]
    if not points:
        points = [{"id": "tp1", "kind": "正向", "text": ac[0], "case_ids": [], "waived": False}]
    platforms = list((und.get("impact") or {}).get("platforms") or [])
    if _looks_web(text) and "web" not in platforms:
        platforms.append("web")
    if _looks_app(text) and "app" not in platforms:
        platforms.append("app")
    impact = und.get("impact") if isinstance(und.get("impact"), dict) else {}
    return {
        "summary": str(req.get("title") or ""),
        "ac": ac,
        "features": [{"name": str(req.get("title") or "功能"), "notes": ""}],
        "points": [
            {
                "id": p.get("id") or f"tp{i + 1}",
                "kind": p.get("kind") or "正向",
                "text": p.get("text") or "",
            }
            for i, p in enumerate(points)
            if isinstance(p, dict)
        ],
        "risks": list(und.get("stale_risks") or []),
        "impact": {
            **impact,
            "platforms": platforms or impact.get("platforms") or [],
        },
        "change_kind": und.get("change_kind") or "",
        "baseline": und.get("baseline") or "",
        "delta": und.get("delta") or "",
        "journeys": und.get("journeys") or [],
        "new_features": und.get("new_features") or [],
        "keep_features": und.get("keep_features") or [],
        "exceptions": und.get("exceptions") or [],
        "surfaces": und.get("surfaces") or [],
        "hang": {"paths": [], "module_names": [], "feature_names": [str(req.get("title") or "").strip()]},
        "atlas_create": [],
    }


def _looks_web(text: str) -> bool:
    return bool(re.search(r"web|h5|后台|管理端|网页|运营平台|运营后台|cms", str(text or ""), re.I))


def _looks_app(text: str) -> bool:
    return bool(re.search(r"app|移动端|客户端|ios|android|安卓", str(text or ""), re.I))


def analyze_req(req: dict, cases: list | None = None, atlas_doc: dict | None = None, *, user_note: str = "") -> dict:
    text = _source_text(req)
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    # 稳定块：图谱和用例库是应用级的，一次 tick 里遍历多条需求时逐字节相同 → 命中前缀缓存。
    stable = json.dumps(
        {
            "app_atlas": atlas.compact_atlas(atlas_doc),
            "existing_cases": [
                {
                    "case_id": c.get("case_id"),
                    "name": c.get("name") or c.get("title"),
                    "module": c.get("module"),
                }
                for c in (cases or [])[:40]
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    user = json.dumps(
        {
            "title": req.get("title"),
            "external_id": req.get("external_id"),
            "source": text,
            "human_feedback": str(user_note or req.get("analyst_feedback") or "").strip(),
            "previous_analysis": {
                "summary": req.get("summary") or "",
                "change_kind": und.get("change_kind") or "",
                "baseline": und.get("baseline") or "",
                "delta": und.get("delta") or "",
                "impact": und.get("impact") or {},
            },
            "note": "先对照 app_atlas。必须挖入口（不要默认首页）、新增 vs 维持、上传异常兜底、运营平台/Web。platforms 用 app/web/e2e。human_feedback 必须逐条落实。",
        },
        ensure_ascii=False,
    )
    parsed, meta = _ask_json(
        REQ_ANALYST_SYSTEM_PROMPT,
        user,
        max_tokens=8192,
        timeout_sec=240,
        role="req-analyst",
        job="analyze_req",
        stable=stable,
    ) if text else (None, {"error": "没有需求原文"})
    payload = parsed if parsed else _rule_analyze(req)
    engine = "llm" if parsed else "rule"
    failures: list = []
    if not parsed:
        failures.append(
            {
                "reason": "llm_failed",
                "detail": str(meta.get("error") or "没有需求原文")[:160],
                "fallback": "rule_analyze",
            }
        )
    elif meta.get("truncated"):
        failures.append(
            {
                "reason": "truncated",
                "detail": "需求分析输出被截断"
                + ("，已抢救出前面完整的字段，后面的可能缺失" if meta.get("salvaged") else ""),
                "fallback": "salvaged" if meta.get("salvaged") else "",
            }
        )
    if engine == "llm" and not failures:
        suggest = f"已拆验收标准和测试点 · {len(payload.get('points') or [])} 个测试点"
    elif engine == "llm":
        suggest = f"分析不完整：{failures[0].get('detail')}"
    else:
        suggest = f"规则拆点（{meta.get('error') or '无模型'}）"
    payload = dict(payload)
    payload["failures"] = failures
    return _with_engine(
        artifact(
            job="analyze_req",
            suggest=suggest,
            citations=[req.get("id") or ""],
            payload=payload,
            input_hash=_hash(text + "\n" + str(user_note or "")),
        ),
        engine,
        meta,
    )


def _mindmap_point_digest(prev: dict) -> list:
    """上一版脑图只回传「点的 id + 文案」，不回传整棵树。

    整棵树（含每个节点的 detail / path / case_ids）动辄几千 token，模型只需要知道
    上一版有哪些点、不要漏掉，不需要原样看见结构。
    """
    return [
        {"id": p.get("point_id") or p.get("id") or "", "text": str(p.get("text") or "")[:40]}
        for p in collect_mindmap_points(prev)
        if str(p.get("text") or "").strip()
    ][:200]


def _compact_mindmap_for_prompt(node: dict, *, max_detail: int = 48) -> dict:
    """修订模式用的压缩树：保留结构/id，缩短 detail，去掉无用字段。"""
    if not isinstance(node, dict):
        return {}
    out: dict[str, Any] = {}
    for key in ("id", "text", "title", "kind", "platform", "point_id", "path"):
        if node.get(key) not in (None, "", []):
            out[key] = node.get(key)
    detail = str(node.get("detail") or "").strip()
    if detail:
        out["detail"] = detail if len(detail) <= max_detail else detail[:max_detail]
    case_ids = [str(x) for x in (node.get("case_ids") or []) if str(x).strip()]
    if case_ids:
        out["case_ids"] = case_ids[:8]
    kids = [_compact_mindmap_for_prompt(c, max_detail=max_detail) for c in (node.get("children") or []) if isinstance(c, dict)]
    if kids:
        out["children"] = kids
    return out


def _find_platform_branch(mindmap: dict, platform: str, label: str = "") -> dict:
    """从上一版整树里抠出某一端的枝；没有则返回空 platform 根。"""
    kids = [c for c in ((mindmap or {}).get("children") or []) if isinstance(c, dict)]
    if not kids:
        return {
            "id": f"{platform}-root",
            "text": label or platform,
            "kind": "platform",
            "platform": platform,
            "children": [],
        }
    # 复用抽取逻辑：包一层假根
    return _extract_platform_branch({"children": kids}, platform, label or platform)


def _capture_mindmap_retry_note(*, note: str, req: dict) -> Optional[dict]:
    """重试脑图对话框里的评论 → 测试知识库，直接 approved。"""
    text = str(note or "").strip()
    if not text:
        return None
    try:
        from server.services.system_settings_service import upsert_knowledge_item
    except Exception:
        return None
    title_base = str(req.get("title") or req.get("id") or "需求").strip() or "需求"
    app_id = str(dispatch.ctx().get("app_id") or "").strip()
    try:
        return upsert_knowledge_item(
            {
                "title": f"脑图修订·{title_base[:40]}",
                "content": text,
                "category": "测试脑图",
                "tags": ["mindmap-retry", str(req.get("id") or "").strip()],
                "app_ids": [app_id] if app_id else [],
                "source": "manual",
                "review_status": "approved",
                "enabled": True,
            }
        )
    except Exception:
        return None


MINDMAP_TOKENS = 8192
MINDMAP_SKELETON_TOKENS = 2048
MINDMAP_FILL_TOKENS = 2048
# 填点截断/空白烧 token 时再打一轮：略加预算 + 强制紧凑，避免再刷 2k 制表符。
MINDMAP_FILL_RETRY_TOKENS = 3072
MINDMAP_REVISE_TOKENS = 4096
MINDMAP_SHARD_TIMEOUT_SEC = 90
MINDMAP_FILL_COMPACT_NOTE = (
    "上一次输出无效。这次必须输出紧凑 JSON（尽量单行）："
    '{"points":[{"text":"可判定的一句话","kind":"正向|异常|边界","detail":""}]}。'
    "禁止缩进、制表符、无意义空白；普通功能 2~4 个点，最多 5 个。不要输出别的字段。"
)


def _mindmap_platforms(req: dict, package: str = "") -> list[tuple[str, str]]:
    """按需求分析里的端拆脑图调用。整棵树一次吐 8192 token 必然截断。

    端的枚举和别名来自应用画像（`app_profile`），不写死在这里 —— 换个有小程序或桌面端的
    应用，写死的三个端会让那部分覆盖无处安放。
    """
    from server.services.ai import app_profile as ap

    prof = ap.current(package)
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    found: set[str] = set()

    def add(raw: str) -> None:
        sid = prof.surface_of(raw)
        if sid:
            found.add(sid)

    for row in und.get("surfaces") or []:
        if isinstance(row, dict):
            add(row.get("kind") or row.get("platform") or row.get("name") or "")
        else:
            add(row)
    impact = und.get("impact") if isinstance(und.get("impact"), dict) else {}
    for p in impact.get("platforms") or []:
        add(p)
    for j in und.get("journeys") or []:
        if isinstance(j, dict):
            add(j.get("platform") or "")
    if not found:
        found = set(prof.declared_surfaces())
    # 两个以上真实端才有「端到端」可测
    if len({s for s in found if s != ap.E2E_SURFACE}) >= 2:
        found.add(ap.E2E_SURFACE)
    return [pair for pair in prof.surface_options() if pair[0] in found]


def _prefix_node_ids(node: dict, prefix: str) -> dict:
    out = dict(node)
    nid = str(out.get("id") or "").strip()
    if nid and not nid.startswith(prefix):
        out["id"] = f"{prefix}{nid}"
    kids = []
    for ch in out.get("children") or []:
        if isinstance(ch, dict):
            kids.append(_prefix_node_ids(ch, prefix))
    out["children"] = kids
    return out


def _extract_platform_branch(parsed: dict, platform: str, label: str, package: str = "") -> dict:
    from server.services.ai import app_profile as ap

    prof = ap.current(package)
    children = [c for c in (parsed.get("children") or []) if isinstance(c, dict)]
    hit = None
    for c in children:
        # 模型有时不把端写在 platform 字段上，只写在 text 里（而且用的是画像里的别名，
        # 例如把 Web 那一枝叫「运营平台」）。两边都认，但只认整段就是端名的情况。
        plat = prof.surface_of(c.get("platform") or "", loose=False)
        text_plat = prof.surface_of(c.get("text") or "", loose=False)
        if platform in (plat, text_plat):
            hit = c
            break
    if hit is None and len(children) == 1:
        hit = children[0]
    if hit is None:
        hit = {
            "id": f"{platform}-root",
            "text": label,
            "kind": "platform",
            "platform": platform,
            "children": children,
        }
    node = dict(hit)
    node["kind"] = "platform"
    node["platform"] = platform
    node["text"] = label
    return _prefix_node_ids(node, f"{platform}-")


def _module_titles(node: dict) -> list[str]:
    out = []

    def walk(n: dict, depth: int) -> None:
        text = str(n.get("text") or "").strip()
        kind = str(n.get("kind") or "")
        if depth >= 1 and text and kind in ("module", "feature", "platform"):
            out.append(text)
        for ch in n.get("children") or []:
            if isinstance(ch, dict):
                walk(ch, depth + 1)

    walk(node, 0)
    return out[:48]


def _merge_platform_children(base: dict, extra: dict) -> dict:
    have = {str(c.get("text") or "") for c in (base.get("children") or []) if isinstance(c, dict)}
    kids = [c for c in (base.get("children") or []) if isinstance(c, dict)]
    for ch in extra.get("children") or []:
        if not isinstance(ch, dict):
            continue
        name = str(ch.get("text") or "")
        if name and name in have:
            continue
        kids.append(ch)
        if name:
            have.add(name)
    out = dict(base)
    out["children"] = kids
    return out


def _point_node(raw: dict, *, platform: str, parent_path: list, index: int) -> dict:
    text = str(raw.get("text") or raw.get("title") or "").strip()
    kind = str(raw.get("kind") or raw.get("point_kind") or "正向").strip() or "正向"
    path = list(parent_path) + [text] if text else list(parent_path)
    return {
        "id": str(raw.get("id") or f"pt-{index + 1}"),
        "text": text[:40],
        "kind": "point",
        "point_kind": kind,
        "point_id": str(raw.get("point_id") or raw.get("id") or ""),
        "platform": str(raw.get("platform") or platform or ""),
        "detail": str(raw.get("detail") or "")[:160],
        "path": path,
        "case_ids": list(raw.get("case_ids") or []),
        "children": [],
    }


def _points_payload(parsed: dict, *, platform: str, parent_path: list) -> list[dict]:
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("points")
    if not isinstance(raw, list):
        # 模型偶尔仍吐一棵小树，从里面把测试点抠出来
        raw = [n for n in cover_checks.collect_points(parsed)]
    out = []
    have = set()
    for i, item in enumerate(raw or []):
        if not isinstance(item, dict):
            continue
        node = _point_node(item, platform=platform, parent_path=parent_path, index=i)
        key = str(node.get("text") or "")
        if not key or key in have:
            continue
        have.add(key)
        out.append(node)
    return out


def _attach_points(feat: dict, points: list[dict]) -> int:
    kids = [c for c in (feat.get("children") or []) if isinstance(c, dict)]
    have = {str(c.get("text") or "") for c in kids if cover_checks.is_point(c)}
    added = 0
    for p in points:
        text = str(p.get("text") or "")
        if not text or text in have:
            continue
        kids.append(p)
        have.add(text)
        added += 1
    feat["children"] = kids
    return added


def _draft_mindmap_revise(
    req: dict,
    prev: dict,
    atlas_doc: dict | None,
    *,
    user_note: str,
    stable: str,
    base_payload: dict,
) -> dict:
    """有上一版脑图时的修订路径：按端带走 previous_branch，在基线上改，不整树重写。"""
    note = str(user_note or "").strip()
    shards = _mindmap_platforms(req)
    failures: list = []
    usage_acc: dict = {}
    cover_jobs.report(phase="mindmap_revise", label="正在按上一版修订脑图", done=0, total=len(shards) or 1)

    def ask_shard(platform: str, label: str, extra: dict) -> tuple[Optional[dict], dict]:
        user = json.dumps(
            {
                **base_payload,
                "previous_mindmap": _compact_mindmap_for_prompt(prev),
                "scope": {
                    "platform": platform,
                    "label": label,
                    **extra,
                },
            },
            ensure_ascii=False,
        )
        return _ask_json(
            MINDMAP_WRITER_SYSTEM_PROMPT,
            user,
            max_tokens=MINDMAP_REVISE_TOKENS,
            timeout_sec=MINDMAP_SHARD_TIMEOUT_SEC,
            role="mindmap-writer",
            job="draft_mindmap",
            stable=stable,
        )

    def revise_one(pair: tuple[str, str]) -> dict:
        platform, label = pair
        prev_branch = _find_platform_branch(prev, platform, label)
        compact_branch = _compact_mindmap_for_prompt(prev_branch)
        only = (
            f"在 previous_branch 上修订 {label} 这一枝。"
            + (f"必须落实：{note}" if note else "对照需求补漏加深，没点名的节点尽量保留 id。")
            + "不要另起一棵树，不要把新旧两版拼在一起。"
        )
        parsed, meta = ask_shard(
            platform,
            label,
            {
                "mode": "revise",
                "previous_branch": compact_branch,
                "only": only,
            },
        )
        local_fail: list = []
        if not parsed:
            local_fail.append(
                {
                    "reason": "llm_failed",
                    "detail": f"{label} 修订：{str(meta.get('error') or '模型没有返回可用脑图')[:120]}",
                    "fallback": "keep_previous",
                    "platform": platform,
                }
            )
            return {
                "platform": platform,
                "label": label,
                "branch": prev_branch,
                "meta": meta,
                "usage": (meta,),
                "failures": local_fail,
            }
        branch = _extract_platform_branch(parsed, platform, label)
        if meta.get("truncated"):
            local_fail.append(
                {
                    "reason": "truncated",
                    "detail": f"{label} 修订被 max_tokens 截断，已尽量保留可解析部分；不足处请再评一次",
                    "fallback": "salvaged" if meta.get("salvaged") else "",
                    "platform": platform,
                }
            )
        return {
            "platform": platform,
            "label": label,
            "branch": branch,
            "meta": meta,
            "usage": (meta,),
            "failures": local_fail,
        }

    branches: list[dict] = []
    last_meta: dict = {}
    for row in map_llm(revise_one, shards):
        if isinstance(row, Exception):
            if isinstance(row, cover_jobs.Cancelled):
                raise row
            failures.append({"reason": "llm_failed", "detail": f"修订并发失败：{row}"[:160], "fallback": ""})
            cover_jobs.inc(1)
            continue
        for u in row.get("usage") or (row.get("meta"),):
            if isinstance(u, dict):
                _add_usage(usage_acc, u)
                last_meta = u
        failures.extend(row.get("failures") or [])
        if row.get("branch"):
            branches.append(row["branch"])
        cover_jobs.inc(1, label=f"修订 · {row.get('label') or row.get('platform') or ''}")

    user = json.dumps({**base_payload, "mode": "revise"}, ensure_ascii=False)
    if not branches:
        # 修订全失败：保住上一版，别交空树
        parsed = _clip_mindmap(prev) if prev else {"title": _short_title(req.get("title") or "需求", 10), "children": []}
        if not failures:
            failures.append(
                {
                    "reason": "llm_failed",
                    "detail": str(last_meta.get("error") or "修订失败，已保留上一版脑图")[:160],
                    "fallback": "keep_previous",
                }
            )
    else:
        parsed = {"title": _short_title(req.get("title") or prev.get("title") or "需求", 10), "children": branches}

    parsed = _normalize_mindmap_hierarchy(parsed)
    for gap in cover_checks.gaps(req, parsed):
        failures.append(
            {
                "reason": "coverage_gap",
                "detail": f"脑图缺少{ {'new_feature': '新功能', 'keep_feature': '回归功能', 'exception': '异常点', 'journey': '路径'} .get(gap['kind'], gap['kind']) }「{gap['name']}」",
                "gap": gap,
            }
        )

    n_points = _count_mindmap_points(parsed)
    llm_ok = bool(branches) and (usage_acc.get("engine") == "llm" or last_meta.get("engine") == "llm")
    engine = "llm" if llm_ok else "rule"
    suggest = f"已按上一版修订测试脑图 · {n_points} 个测试点"
    if note:
        suggest += "（已落实评论）"
    if failures:
        suggest += f"（{failures[0].get('detail') or '修订不完整'}）"
    payload = dict(parsed)
    payload["failures"] = failures
    payload["stats"] = {
        "points": n_points,
        "shards": [p for p, _ in shards],
        "features": len(cover_checks.feature_nodes(parsed)),
        "mode": "revise",
        "gaps": sum(1 for f in failures if f.get("reason") == "coverage_gap"),
    }
    meta = {**last_meta, **usage_acc}
    return _with_engine(
        artifact(
            job="draft_mindmap",
            suggest=suggest,
            citations=[req.get("id") or ""],
            payload=payload,
            input_hash=_hash(user),
        ),
        engine,
        meta,
    )


def draft_mindmap(req: dict, cases: list | None = None, atlas_doc: dict | None = None, *, user_note: str = "") -> dict:
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    intent = req.get("atlas_intent") if isinstance(req.get("atlas_intent"), dict) else {}
    hang = intent.get("hang") if isinstance(intent.get("hang"), dict) else {}
    atlas_paths = atlas.paths_for_req(atlas_doc, req.get("id") or "") or hang.get("paths") or []
    bundle = _analysis_bundle(req)
    note = str(user_note or req.get("analyst_feedback") or "").strip()
    prev = _baseline_mindmap(req)
    # 稳定块：同一个应用的图谱在多次调用 / 多条需求之间不变，单独成 message 命中前缀缓存。
    stable = json.dumps(
        {"app_atlas": atlas.compact_atlas(atlas_doc)},
        ensure_ascii=False,
        sort_keys=True,
    )
    has_prev = bool(isinstance(prev, dict) and prev.get("children"))
    base_payload = {
        "title": req.get("title"),
        **bundle,
        "atlas_paths": atlas_paths,
        "features": req.get("features") or und.get("features") or [],
        "previous_points": _mindmap_point_digest(prev),
        "retry_note": note,
        "human_feedback": note,
        "note": (
            "这是修订不是重写。previous_mindmap / previous_branch 是权威基线；"
            "评论没点名的尽量保留 id；评论要求改结构就改；禁止新旧两版拼成平行树。"
            if has_prev
            else "必须详尽。入口跟 journeys，不要默认首页。new_features 加厚，keep_features 回归，exceptions 全部落点。"
        ),
    }
    if has_prev:
        return _draft_mindmap_revise(
            req,
            prev,
            atlas_doc,
            user_note=note,
            stable=stable,
            base_payload=base_payload,
        )
    # ↓ 首版：骨架 + 填点
    shards = _mindmap_platforms(req)
    failures: list = []
    usage_acc: dict = {}
    cover_jobs.report(phase="mindmap_skeleton", label="正在写脑图骨架", done=0, total=len(shards) or 1)

    def ask_shard(platform: str, label: str, extra: dict, *, max_tokens: int = MINDMAP_SKELETON_TOKENS) -> tuple[Optional[dict], dict]:
        user = json.dumps(
            {
                **base_payload,
                "scope": {
                    "platform": platform,
                    "label": label,
                    **extra,
                },
            },
            ensure_ascii=False,
        )
        return _ask_json(
            MINDMAP_WRITER_SYSTEM_PROMPT,
            user,
            max_tokens=max_tokens,
            timeout_sec=MINDMAP_SHARD_TIMEOUT_SEC,
            role="mindmap-writer",
            job="draft_mindmap",
            stable=stable,
        )

    def skeleton_one(pair: tuple[str, str]) -> dict:
        platform, label = pair
        local_fail: list = []
        parsed, meta = ask_shard(
            platform,
            label,
            {
                "mode": "skeleton",
                "only": f"这一轮只输出 {label} 的骨架（模块和功能，不要测试点）。children 里只能有一个 kind=platform 且 platform={platform} 的根。",
            },
        )
        if not parsed:
            local_fail.append(
                {
                    "reason": "llm_failed",
                    "detail": f"{label} 骨架：{str(meta.get('error') or '模型没有返回可用脑图')[:120]}",
                    "fallback": "",
                    "platform": platform,
                }
            )
            return {"platform": platform, "label": label, "branch": None, "meta": meta, "usage": (meta,), "failures": local_fail}
        branch = _extract_platform_branch(parsed, platform, label)
        usages = [meta]
        if meta.get("truncated"):
            already = _module_titles(branch)
            parsed2, meta2 = ask_shard(
                platform,
                label,
                {
                    "mode": "skeleton",
                    "already": already,
                    "only": f"上一轮 {label} 骨架被截断。不要重复 already 里的模块，只补骨架，不要测试点。",
                },
            )
            usages.append(meta2)
            if parsed2:
                branch = _merge_platform_children(branch, _extract_platform_branch(parsed2, platform, label))
            if meta2.get("truncated") or not parsed2:
                local_fail.append(
                    {
                        "reason": "truncated",
                        "detail": f"{label} 骨架仍被 max_tokens 截断，已尽量补全前面的模块",
                        "fallback": "salvaged" if (meta.get("salvaged") or meta2.get("salvaged")) else "",
                        "platform": platform,
                    }
                )
        return {
            "platform": platform,
            "label": label,
            "branch": branch,
            "meta": usages[-1],
            "usage": tuple(usages),
            "failures": local_fail,
        }

    shard_rows = map_llm(skeleton_one, shards)
    branches: list[dict] = []
    last_meta: dict = {}
    for row in shard_rows:
        if isinstance(row, Exception):
            if isinstance(row, cover_jobs.Cancelled):
                raise row
            failures.append({"reason": "llm_failed", "detail": f"骨架并发失败：{row}"[:160], "fallback": ""})
            cover_jobs.inc(1)
            continue
        for u in row.get("usage") or (row.get("meta"),):
            if isinstance(u, dict):
                _add_usage(usage_acc, u)
                last_meta = u
        failures.extend(row.get("failures") or [])
        if row.get("branch"):
            branches.append(row["branch"])
        cover_jobs.inc(1, label=f"骨架 · {row.get('label') or row.get('platform') or ''}")

    # 第 2 段：点太少的功能枝并发填点。模型如果在骨架阶段已经写了点，thin_features 会跳过。
    fill_jobs = []
    for branch in branches:
        plat = str(branch.get("platform") or "")
        for feat in cover_checks.thin_features(branch, min_points=2):
            fill_jobs.append({"branch": branch, "feat": feat, "platform": plat})

    def fill_one(job: dict) -> dict:
        feat = job["feat"]
        platform = job["platform"]
        path = list(feat.get("path") or [str(feat.get("text") or "")])
        feat_text = feat.get("text")
        parsed, meta = ask_shard(
            platform,
            platform,
            {
                "mode": "fill_points",
                "branch": {
                    "text": feat_text,
                    "kind": feat.get("kind"),
                    "path": path,
                    "platform": platform,
                },
                "only": f"只给功能「{feat_text}」写测试点，不要输出别的模块。",
            },
            max_tokens=MINDMAP_FILL_TOKENS,
        )
        meta_first = dict(meta) if isinstance(meta, dict) else {}
        points = _points_payload(parsed or {}, platform=platform, parent_path=path)
        added = _attach_points(feat, points)
        # 截断（常见：开头就把 2048 token 烧在空白上）或解析失败：紧凑格式再打一轮。
        need_retry = not added and (
            bool(meta_first.get("truncated"))
            or str(meta_first.get("fail_kind") or "") in ("parse", "truncated")
            or bool(meta_first.get("error"))
        )
        if need_retry:
            parsed2, meta2 = ask_shard(
                platform,
                platform,
                {
                    "mode": "fill_points",
                    "branch": {
                        "text": feat_text,
                        "kind": feat.get("kind"),
                        "path": path,
                        "platform": platform,
                    },
                    "only": f"只给功能「{feat_text}」写测试点，不要输出别的模块。",
                    "retry_note": MINDMAP_FILL_COMPACT_NOTE,
                },
                max_tokens=MINDMAP_FILL_RETRY_TOKENS,
            )
            points2 = _points_payload(parsed2 or {}, platform=platform, parent_path=path)
            added2 = _attach_points(feat, points2)
            meta2 = dict(meta2) if isinstance(meta2, dict) else {}
            merged = {
                **meta2,
                "retry_reasons": list(meta_first.get("retry_reasons") or [])
                + ["fill_compact"]
                + list(meta2.get("retry_reasons") or []),
                "attempts": int(meta_first.get("attempts") or 0) + int(meta2.get("attempts") or 0),
                "elapsed_ms": int(meta_first.get("elapsed_ms") or 0) + int(meta2.get("elapsed_ms") or 0),
            }
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                merged[k] = int(meta_first.get(k) or 0) + int(meta2.get(k) or 0)
            if added2:
                added = added2
                merged["error"] = ""
                merged["fail_kind"] = ""
                merged["truncated"] = bool(meta2.get("truncated"))
            elif meta2.get("error"):
                merged["error"] = meta2.get("error")
                merged["fail_kind"] = meta2.get("fail_kind") or meta_first.get("fail_kind")
            meta = merged
        return {"meta": meta, "added": added, "text": feat_text, "platform": platform}

    if fill_jobs:
        cover_jobs.report(phase="mindmap_fill", label="正在给功能填测试点")
        cover_jobs.add_total(len(fill_jobs))
        for row in map_llm(fill_one, fill_jobs):
            if isinstance(row, Exception):
                if isinstance(row, cover_jobs.Cancelled):
                    raise row
                failures.append({"reason": "llm_failed", "detail": f"填点失败：{row}"[:160], "fallback": ""})
                cover_jobs.inc(1)
                continue
            if isinstance(row.get("meta"), dict):
                _add_usage(usage_acc, row["meta"])
                last_meta = row["meta"]
                if not row.get("added") and row["meta"].get("error"):
                    failures.append(
                        {
                            "reason": "llm_failed",
                            "detail": f"{row.get('text') or '功能'} 填点：{str(row['meta'].get('error') or '')[:120]}",
                            "platform": row.get("platform") or "",
                        }
                    )
            cover_jobs.inc(1, label=f"填点 · {row.get('text') or ''}")

    user = json.dumps(base_payload, ensure_ascii=False)
    if not branches:
        grouped = {}
        for i, p in enumerate(und.get("points") or []):
            if not isinstance(p, dict):
                continue
            path = p.get("path") or (atlas_paths[min(i, len(atlas_paths) - 1)] if atlas_paths else ["测试点"])
            key = tuple(path) if isinstance(path, list) else tuple(split_label(path))
            grouped.setdefault(key or ("测试点",), []).append(p)
        children = _tree_from_groups(grouped) or [{
            "id": "n1",
            "text": "测试点",
            "kind": "feature",
            "path": ["测试点"],
            "children": [
                {
                    "id": f"n1-{j + 1}",
                    "text": _short_title(p.get("text") or f"点{j + 1}"),
                    "kind": "point",
                    "point_id": p.get("id") or "",
                    "case_ids": list(p.get("case_ids") or []),
                    "children": [],
                }
                for j, p in enumerate(p for p in (und.get("points") or []) if isinstance(p, dict))
            ],
        }]
        parsed = {"title": _short_title(req.get("title") or "需求", 10), "children": children}
        if not failures:
            failures.append(
                {
                    "reason": "llm_failed",
                    "detail": str(last_meta.get("error") or "模型没有返回可用脑图")[:160],
                    "fallback": "rule_tree",
                }
            )
    else:
        parsed = {"title": _short_title(req.get("title") or "需求", 10), "children": branches}

    # 第 3 段：代码校验。缺的功能只记进 failures，下一次定点重试 / 评论重跑时会带着 missing 再问。
    # 这里不自动再调一轮模型 —— 漏检必须让人看见，静默补齐会把「模型没写」伪装成「已经覆盖」。
    parsed = _normalize_mindmap_hierarchy(parsed)
    for gap in cover_checks.gaps(req, parsed):
        failures.append(
            {
                "reason": "coverage_gap",
                "detail": f"脑图缺少{ {'new_feature': '新功能', 'keep_feature': '回归功能', 'exception': '异常点', 'journey': '路径'} .get(gap['kind'], gap['kind']) }「{gap['name']}」",
                "gap": gap,
            }
        )

    n_points = _count_mindmap_points(parsed)
    llm_ok = bool(branches) and (usage_acc.get("engine") == "llm" or last_meta.get("engine") == "llm")
    engine = "llm" if llm_ok else "rule"
    suggest = f"已生成测试脑图草稿 · {n_points} 个测试点"
    if failures:
        suggest += f"（{failures[0].get('detail') or '生成不完整'}，请重试或补充说明）"
    payload = dict(parsed)
    payload["failures"] = failures
    payload["stats"] = {
        "points": n_points,
        "shards": [p for p, _ in shards],
        "features": len(cover_checks.feature_nodes(parsed)),
        "fill_jobs": len(fill_jobs),
        "gaps": sum(1 for f in failures if f.get("reason") == "coverage_gap"),
    }
    meta = {**last_meta, **usage_acc}
    return _with_engine(
        artifact(
            job="draft_mindmap",
            suggest=suggest,
            citations=[req.get("id") or ""],
            payload=payload,
            input_hash=_hash(user),
        ),
        engine,
        meta,
    )


def split_label(label: str) -> list:
    return [p.strip() for p in str(label or "").replace(">", "/").split("/") if p.strip()]


def _case_text(val) -> str:
    if val is None:
        return ""
    if isinstance(val, list):
        lines = []
        n = 0
        for item in val:
            if isinstance(item, dict):
                t = str(item.get("text") or item.get("step") or item.get("expected") or item.get("action") or "").strip()
            else:
                t = str(item).strip()
            if not t:
                continue
            n += 1
            if t[0] not in "0123456789":
                t = f"{n}. {t}"
            lines.append(t)
        return "\n".join(lines)
    return str(val).strip()


def _normalize_draft_case(row: dict, index: int) -> dict:
    out = dict(row)
    steps = _case_text(out.get("steps") or out.get("step") or out.get("test_steps"))
    expected = _case_text(out.get("expected") or out.get("expect") or out.get("expected_result"))
    if not steps:
        steps = f"1. 打开应用\n2. 覆盖「{out.get('name') or '该测试点'}」"
    if not expected:
        expected = "1. 达到该测试点描述的结果"
    out["case_id"] = out.get("case_id") or f"draft-{index + 1}"
    out["name"] = out.get("name") or out.get("title") or out["case_id"]
    out["steps"] = steps
    out["expected"] = expected
    out["precondition"] = _case_text(out.get("precondition") or out.get("pre")) or "账号可用，应用可启动"
    out["steps_raw"] = steps
    out["expected_raw"] = expected
    out["precondition_raw"] = out["precondition"]
    out["platform"] = out.get("platform") or "双端"
    out["aspect"] = str(out.get("aspect") or out.get("kind") or "正向").strip() or "正向"
    # origin：llm | stub | human | import。缺省视为 llm（模型写的）。
    # 这个字段是「不再静默降级」的基础：桩用例必须能被前端和覆盖率统计区分出来。
    out["origin"] = str(out.get("origin") or "llm").strip() or "llm"
    # locked：人工改过的用例，重试时不许被覆盖。
    out["locked"] = bool(out.get("locked"))
    return out


def _short_title(text: str, max_len: int = 40) -> str:
    s = " ".join(str(text or "").split())
    if not s:
        return ""
    return s if len(s) <= max_len else s[:max_len]


def _clip_mindmap(node: dict) -> dict:
    out = dict(node)
    title = out.get("text") or out.get("title") or ""
    kind = out.get("kind") or ""
    cap = 40 if kind == "point" else 20
    if title:
        clipped = _short_title(title, cap)
        if "text" in out or not out.get("title"):
            out["text"] = clipped
        if out.get("title"):
            out["title"] = _short_title(out.get("title") or "", 20)
    kids = out.get("children")
    if isinstance(kids, list):
        out["children"] = [_clip_mindmap(c) for c in kids if isinstance(c, dict)]
    return out


def _node_kind(node: dict) -> str:
    kind = str((node or {}).get("kind") or "").strip().lower()
    if kind in ("root", "platform", "module", "feature", "point"):
        return kind
    kids = [c for c in ((node or {}).get("children") or []) if isinstance(c, dict)]
    text = str((node or {}).get("text") or (node or {}).get("title") or "").strip()
    if not kids and len(text) >= 12:
        return "point"
    if not kids:
        return "point"
    # 有子节点且文案短 → 更像模块/功能
    if any(_node_kind(c) == "point" for c in kids) or any(str(c.get("kind") or "") == "point" for c in kids):
        return "feature"
    return "module"


def _pick_feature_for_point(features: list[dict], point: dict) -> Optional[dict]:
    """把误挂的测试点归到文案最相关的功能下。"""
    if not features:
        return None
    pt = str(point.get("text") or point.get("title") or "")
    best = None
    best_score = 0
    for feat in features:
        ft = str(feat.get("text") or feat.get("title") or "")
        if not ft:
            continue
        score = 0
        if ft in pt or pt in ft:
            score = max(score, len(ft))
        # 共享 2+ 字片段
        for i in range(len(ft) - 1):
            gram = ft[i : i + 2]
            if gram and gram in pt:
                score = max(score, 2)
                break
        if score > best_score:
            best_score = score
            best = feat
    return best or features[0]


def _normalize_mindmap_hierarchy(tree: dict) -> dict:
    """纠正「测试点与功能同级」：模块下只留 module/feature，点一律挂到功能下。"""
    if not isinstance(tree, dict):
        return tree

    def fix(node: dict) -> dict:
        out = dict(node)
        kind = _node_kind(out)
        if not out.get("kind"):
            out["kind"] = kind
        raw_kids = [fix(c) for c in (out.get("children") or []) if isinstance(c, dict)]
        if kind in ("point",):
            out["children"] = []
            out["kind"] = "point"
            return out

        modules: list[dict] = []
        features: list[dict] = []
        points: list[dict] = []
        for ch in raw_kids:
            ck = _node_kind(ch)
            ch["kind"] = ck
            if ck == "point":
                points.append(ch)
            elif ck == "feature":
                features.append(ch)
            elif ck == "module":
                modules.append(ch)
            elif ck == "platform":
                modules.append(ch)
            else:
                # 有子节点当模块，否则当点
                (features if ch.get("children") else points).append(ch)

        if points and kind in ("platform", "module", "root", ""):
            for pt in points:
                pt["kind"] = "point"
                pt["children"] = []
                host = _pick_feature_for_point(features, pt)
                if host is None:
                    host = {
                        "id": f"{out.get('id') or 'n'}-misc",
                        "text": "未归类",
                        "kind": "feature",
                        "path": list(out.get("path") or []) + ["未归类"],
                        "children": [],
                    }
                    features.append(host)
                kids = [c for c in (host.get("children") or []) if isinstance(c, dict)]
                # 同文案去重
                texts = {str(c.get("text") or "") for c in kids}
                if str(pt.get("text") or "") not in texts:
                    kids.append(pt)
                host["children"] = kids
                host["kind"] = "feature"
            points = []

        if kind == "feature":
            # 功能下只留测试点；误塞的功能/模块降成点或展平其子点
            flat_points: list[dict] = []
            for ch in features + modules + points:
                if _node_kind(ch) == "point" or not ch.get("children"):
                    row = dict(ch)
                    row["kind"] = "point"
                    row["children"] = []
                    flat_points.append(row)
                else:
                    for gp in ch.get("children") or []:
                        if isinstance(gp, dict):
                            row = dict(gp)
                            row["kind"] = "point"
                            row["children"] = []
                            flat_points.append(row)
            # 去重
            seen = set()
            uniq = []
            for p in flat_points:
                key = str(p.get("text") or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                uniq.append(p)
            out["children"] = uniq
            out["kind"] = "feature"
            return out

        out["children"] = modules + features
        return out

    return fix(tree)


def _baseline_mindmap(req: dict) -> dict:
    """重试应以「当前」飞书脑图对应的快照为准；没有快照再退回 req.mindmap。"""
    cur = req.get("mindmap_wiki") if isinstance(req.get("mindmap_wiki"), dict) else {}
    token = str(cur.get("node_token") or "").strip()
    if token:
        for row in req.get("mindmap_wiki_history") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("node_token") or "") != token:
                continue
            if row.get("invalid"):
                continue
            snap = row.get("mindmap_snapshot")
            if isinstance(snap, dict) and (snap.get("children") or snap.get("text") or snap.get("title")):
                return dict(snap)
    mm = req.get("mindmap") if isinstance(req.get("mindmap"), dict) else {}
    return dict(mm) if mm else {}


def _tree_from_groups(grouped: dict) -> list:
    root_kids: list[dict] = []

    def ensure(path: tuple, pts: list) -> None:
        kids = root_kids
        acc: list[str] = []
        for i, part in enumerate(path):
            acc.append(part)
            found = next((x for x in kids if x.get("text") == part), None)
            if not found:
                found = {
                    "id": f"n-{'-'.join(acc)}",
                    "text": _short_title(part, 20),
                    "kind": "feature" if i == len(path) - 1 else "module",
                    "path": list(acc),
                    "children": [],
                }
                kids.append(found)
            kids = found["children"]
        for j, p in enumerate(pts):
            if not isinstance(p, dict):
                continue
            kids.append(
                {
                    "id": f"{found.get('id')}-{j + 1}",
                    "text": _short_title(p.get("text") or f"点{j + 1}"),
                    "kind": "point",
                    "point_id": p.get("id") or "",
                    "platform": str(p.get("platform") or ""),
                    "case_ids": list(p.get("case_ids") or []),
                    "children": [],
                }
            )

    for path, pts in grouped.items():
        parts = path if isinstance(path, tuple) else tuple(split_label(path))
        ensure(parts or ("测试点",), pts)
    return root_kids


def apply_mindmap(req: dict, payload: dict) -> dict:
    next_req = dict(req)
    raw = dict(payload) if isinstance(payload, dict) else {}
    # failures / stats 是这次生成的元信息，不是脑图节点 —— 单独存，别塞进树里。
    failures = [x for x in (raw.pop("failures", None) or []) if isinstance(x, dict)]
    stats = raw.pop("stats", None)
    next_req["mindmap"] = _clip_mindmap(_normalize_mindmap_hierarchy(raw))
    next_req["mindmap_failures"] = failures
    if isinstance(stats, dict):
        next_req["mindmap_stats"] = stats
    next_req = _sync_points_from_mindmap(next_req)
    # 脑图换了，测试点 id 可能变。未锁定且挂在已消失测试点上的用例是孤儿，留着只会
    # 把覆盖率搅乱。锁定的（人工改过 / 导入）永远留下。
    live = {str(p.get("id") or "") for p in (next_req.get("understanding") or {}).get("points") or []}
    next_req["draft_cases"] = [
        row
        for row in (next_req.get("draft_cases") or [])
        if isinstance(row, dict)
        and (
            row.get("locked")
            or any(str(x) in live for x in (row.get("point_ids") or []))
            or not (row.get("point_ids") or [])
        )
    ]
    next_req["updated_at"] = _now()
    return next_req


def collect_mindmap_points(mindmap) -> list:
    """测试点 = kind=point（含带子节点的）以及没有子节点的叶子。中间层 module/feature 不算。"""
    out = []

    def walk(node, path):
        if not isinstance(node, dict):
            return
        text = str(node.get("text") or node.get("title") or "").strip()
        kind = str(node.get("kind") or "")
        kids = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        next_path = list(path)
        if text and kind not in ("root", "platform"):
            next_path = path + [text]
        leaf = not kids
        is_point = bool(text) and kind not in ("root", "platform") and (
            kind == "point" or (leaf and kind not in ("module", "feature"))
        )
        if is_point:
            row = dict(node)
            row["path"] = node.get("path") or next_path
            out.append(row)
        for child in kids:
            walk(child, next_path)

    walk(mindmap if isinstance(mindmap, dict) else {}, [])
    return out


def _sync_points_from_mindmap(req: dict) -> dict:
    leaves = collect_mindmap_points(req.get("mindmap"))
    if not leaves:
        return req
    und = dict(req.get("understanding") or {})
    old = [p for p in (und.get("points") or []) if isinstance(p, dict)]
    old_by_id = {str(p.get("id") or ""): p for p in old}
    old_by_text = {str(p.get("text") or "").strip(): p for p in old}
    points = []
    used = set()
    for i, leaf in enumerate(leaves):
        pid = str(leaf.get("point_id") or leaf.get("id") or f"tp{i + 1}").strip() or f"tp{i + 1}"
        if pid in used:
            pid = f"{pid}-{i + 1}"
        used.add(pid)
        text = str(leaf.get("text") or leaf.get("title") or "").strip()
        prev = old_by_id.get(pid) or old_by_text.get(text) or {}
        points.append(
            {
                "id": pid,
                "kind": str(leaf.get("point_kind") or prev.get("kind") or "正向"),
                "text": text or str(prev.get("text") or ""),
                "detail": str(leaf.get("detail") or prev.get("detail") or ""),
                "path": leaf.get("path") or prev.get("path") or [],
                "platform": str(leaf.get("platform") or prev.get("platform") or ""),
                "case_ids": list(prev.get("case_ids") or leaf.get("case_ids") or []),
                "waived": bool(prev.get("waived")),
                "waive_reason": prev.get("waive_reason") or "",
            }
        )
    und["points"] = points
    next_req = dict(req)
    next_req["understanding"] = und
    return next_req


def _gap_points(req: dict) -> list:
    out = []
    for p in ((req.get("understanding") or {}).get("points") or []):
        if not isinstance(p, dict) or p.get("waived"):
            continue
        if p.get("case_ids"):
            continue
        out.append(p)
    return out


def _count_mindmap_points(payload) -> int:
    return len(collect_mindmap_points(payload if isinstance(payload, dict) else {}))


def _append_cover_history(req: dict, *, job: str, kind: str, note: str, art: dict) -> None:
    payload = art.get("payload") if isinstance(art.get("payload"), dict) else {}
    if job == "draft_mindmap":
        summary = f"{_count_mindmap_points(payload)} 个测试点"
        snap = payload
    else:
        rows = [x for x in (payload.get("cases") or []) if isinstance(x, dict)]
        summary = f"{len(rows)} 条用例"
        snap = {"cases": rows}
    hist = [x for x in (req.get("cover_history") or []) if isinstance(x, dict)]
    hist.append(
        {
            "id": f"gen-{int(time.time() * 1000):x}-{job[-6:]}",
            "at": _now(),
            "job": job,
            "kind": kind if kind in ("retry", "generate") else "generate",
            "note": str(note or "").strip(),
            "engine": art.get("engine") or "",
            "suggest": art.get("suggest") or "",
            "summary": summary,
            "payload": snap,
        }
    )
    req["cover_history"] = hist[-24:]


def _apply_cover_art(req: dict, art: dict, *, job: str, user_note: str = "", replace: bool = False) -> dict:
    had_map = isinstance(req.get("mindmap"), dict) and bool((req.get("mindmap") or {}).get("children"))
    had_cases = bool(req.get("draft_cases"))
    if job == "draft_mindmap":
        next_req = apply_mindmap(req, art.get("payload") or {})
        kind = "retry" if (replace or had_map) else "generate"
    else:
        next_req = apply_cases(req, art.get("payload") or {}, replace=replace)
        kind = "retry" if (replace or had_cases) else "generate"
    _append_cover_history(next_req, job=job, kind=kind, note=user_note, art=art)
    next_req["ai_artifacts"] = [*(next_req.get("ai_artifacts") or []), art][-12:]
    return next_req


def _norm_points(items) -> list:
    out = []
    used = set()
    for i, p in enumerate(items or []):
        if not isinstance(p, dict) or p.get("waived"):
            continue
        # 取 id 的优先级必须和 _sync_points_from_mindmap 完全一致（point_id 优先）。
        # 曾经这里是 `id or point_id`、那边是 `point_id or id`，同一个脑图叶子算出两个不同 id：
        # understanding.points[].id = "tp1"，生成的用例 point_ids = ["n1-1-1"]，
        # apply_cases 里永远匹配不上 → case_ids 恒为空 → 每个测试点都显示成「没挂用例」。
        pid = str(p.get("point_id") or p.get("id") or f"tp{i + 1}").strip() or f"tp{i + 1}"
        if pid in used:
            pid = f"{pid}-{i + 1}"
        used.add(pid)
        text = str(p.get("text") or p.get("title") or "").strip()
        if not text:
            continue
        out.append(
            {
                "id": pid,
                "kind": str(p.get("kind") or p.get("point_kind") or "正向"),
                "text": text,
                "detail": str(p.get("detail") or ""),
                "path": p.get("path") or [],
                "platform": str(p.get("platform") or ""),
            }
        )
    return out


def _case_point_ids(rows) -> set:
    ids = set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for pid in row.get("point_ids") or []:
            if pid:
                ids.add(str(pid))
    return ids


def _stub_case(req: dict, point: dict, index: int, *, aspect: str = "正向") -> dict:
    pid = str(point.get("id") or f"tp{index + 1}")
    text = str(point.get("text") or "测试点").strip()
    path = [str(x).strip() for x in (point.get("path") or []) if str(x).strip()]
    module = "-".join(path) or str(req.get("title") or "功能")
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    journeys = [x for x in (und.get("journeys") or []) if isinstance(x, dict)]
    steps = []
    n = 1
    if journeys:
        j = journeys[0]
        entry = str(j.get("entry") or "").strip()
        if entry:
            steps.append(f"{n}. 打开应用，进入「{entry}」")
            n += 1
        for via in j.get("via") or []:
            name = str(via or "").strip()
            if name:
                steps.append(f"{n}. 进入「{name}」")
                n += 1
        page = str(j.get("page") or "").strip()
        if page:
            steps.append(f"{n}. 打开「{page}」")
            n += 1
    if not steps:
        steps.append(f"{n}. 打开应用，按「{module}」进入对应页面")
        n += 1
    if aspect == "异常":
        steps.append(f"{n}. 触发「{text}」的失败/取消/超时")
        n += 1
        expected = "有明确失败或兜底提示，且可以返回重试"
        name = f"{text[:28]}失败兜底"
    elif aspect == "边界":
        steps.append(f"{n}. 用空值、超限或非法格式覆盖「{text}」")
        n += 1
        expected = "非法输入不被提交，提示可理解"
        name = f"{text[:28]}边界"
    else:
        steps.append(f"{n}. 按正向路径覆盖「{text}」")
        n += 1
        expected = "达到该测试点描述的结果"
        name = text[:40]
    steps.append(f"{n}. 核对页面展示与提示")
    plat = str(point.get("platform") or "").strip() or "双端"
    if plat in ("运营平台", "后台"):
        plat = "web"
    suffix = {"正向": "ok", "异常": "ex", "边界": "bd"}.get(aspect, "ok")
    return {
        "case_id": f"draft-{pid}-{suffix}",
        "name": name,
        "module": module,
        "aspect": aspect,
        "precondition": "账号可用，应用可启动" if plat != "web" else "运营平台账号可登录",
        "steps": "\n".join(steps),
        "expected": f"1. {expected}",
        "point_ids": [pid],
        "platform": plat,
        # 模板兜底，不是模型写的。前端据此显红并允许定点补写；覆盖率统计不能把它算成已覆盖。
        "origin": "stub",
    }


def _stub_cases_for_point(req: dict, point: dict, index: int, aspects: list | None = None) -> list:
    """给这个点造模板桩用例。aspects 为空时按规范铺全，否则只补指定的情况。"""
    return [_stub_case(req, point, index, aspect=a) for a in (aspects or _expected_aspects(point))]


CASE_WRITER_EXCERPT_CHARS = 4000


def _case_writer_context(req: dict) -> dict:
    """用例编写者的共享上下文。

    以前这里**没有需求原文也没有 AC**，模型只拿到一个 40 字截断的测试点标题就要写出
    可执行步骤和可判定预期 —— 步骤空泛、预期不可判定是必然结果。
    原文放进共享上下文（stable 块）而不是每批的变化部分，靠前缀缓存摊薄成本。
    """
    bundle = _analysis_bundle(req)
    return {
        "title": req.get("title"),
        "ac": bundle.get("ac") or [],
        "baseline": bundle.get("baseline") or "",
        "delta": bundle.get("delta") or "",
        "source_excerpt": str(bundle.get("source_excerpt") or "")[:CASE_WRITER_EXCERPT_CHARS],
        "journeys": bundle.get("journeys") or [],
        "new_features": bundle.get("new_features") or [],
        "keep_features": bundle.get("keep_features") or [],
        "exceptions": bundle.get("exceptions") or [],
        "surfaces": bundle.get("surfaces") or [],
        "retry_note": bundle.get("analyst_feedback") or "",
    }


def _graft_missing_points(mindmap: dict, missing: list) -> dict:
    root = dict(mindmap or {})
    kids = list(root.get("children") or []) if isinstance(root.get("children"), list) else []
    plat_label = {"app": "App", "web": "Web", "e2e": "端到端"}
    for i, item in enumerate(missing or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        plat = str(item.get("platform") or "app").strip().lower()
        if plat not in plat_label:
            plat = "app"
        path = [str(x).strip() for x in (item.get("path") or []) if str(x).strip()]
        platform_node = next(
            (c for c in kids if isinstance(c, dict) and (c.get("platform") == plat or c.get("text") == plat_label[plat])),
            None,
        )
        if not platform_node:
            platform_node = {"id": f"p-{plat}", "text": plat_label[plat], "kind": "platform", "platform": plat, "children": []}
            kids.append(platform_node)
        cursor = platform_node
        parts = [p for p in path if p not in (plat_label[plat], text)]
        for j, part in enumerate(parts):
            children = cursor.setdefault("children", [])
            if not isinstance(children, list):
                children = []
                cursor["children"] = children
            found = next((c for c in children if isinstance(c, dict) and c.get("text") == part), None)
            if not found:
                found = {
                    "id": f"bf-{i}-{j}",
                    "text": part,
                    "kind": "feature" if j == len(parts) - 1 else "module",
                    "path": parts[: j + 1],
                    "children": [],
                }
                children.append(found)
            cursor = found
        children = cursor.setdefault("children", [])
        if not isinstance(children, list):
            children = []
            cursor["children"] = children
        if any(isinstance(c, dict) and str(c.get("text") or "") == text for c in children):
            continue
        children.append(
            {
                "id": f"bf-pt-{i + 1}",
                "text": text[:40],
                "kind": "point",
                "point_id": f"tp-bf-{i + 1}",
                "point_kind": item.get("kind") or "异常",
                "platform": plat,
                "detail": str(item.get("reason") or ""),
                "case_ids": [],
                "children": [],
            }
        )
    root["children"] = kids
    return root


def _append_unique_cases(rows: list, incoming: list, batch_ids: set | None = None) -> None:
    used = {str(x.get("case_id") or "") for x in rows}
    for row in incoming:
        if not isinstance(row, dict):
            continue
        row = dict(row)
        pids = [str(x) for x in (row.get("point_ids") or []) if x]
        if batch_ids is not None:
            pids = [pid for pid in pids if pid in batch_ids][:1] or pids[:1]
        if not pids:
            continue
        row["point_ids"] = pids[:1]
        cid = str(row.get("case_id") or "").strip() or f"draft-{row['point_ids'][0]}-{len(rows) + 1}"
        if cid in used:
            cid = f"{cid}-{len(rows) + 1}"
        used.add(cid)
        row["case_id"] = cid
        rows.append(row)


CASE_BATCH = 4
CASE_CALL_TIMEOUT_SEC = 120
# 安全阀，不是覆盖上限。撞到它必须把剩下的测试点显式写进 failures，
# 绝不允许像以前那样静默补桩（那会让覆盖率虚高）。
CASE_DEADLINE_SEC = 900
CASE_MAX_ATTEMPTS = 3
CASE_MAX_MISSING = 5
# 一条用例（名称+模块+前置+3~5步步骤+预期）的输出量，实测校准。
TOKENS_PER_CASE = 320
CASE_TOKENS_FLOOR = 1200
CASE_TOKENS_CEIL = 8192


def _expected_aspects(point: dict) -> list[str]:
    """这个测试点按规范必须有哪些情况 —— 代码说了算，不问模型。

    同一份规则同时用于三处：生成时的输出预算、生成后的完整性校验、兜底桩用例。
    v2 会把关键词表换成 capability_tags 查知识库（见 docs/plan-qa-role-quality-v2.md 3.3），
    届时只需要换掉本函数的实现。
    """
    blob = f"{point.get('kind') or ''}{point.get('text') or ''}{point.get('detail') or ''}"
    aspects = ["正向"]
    if any(k in blob for k in ("上传", "保存", "下单", "提交", "失败", "兜底", "权限", "登录")):
        aspects.append("异常")
    if any(k in blob for k in ("上传", "输入", "数量", "空", "格式", "大小")):
        aspects.append("边界")
    return aspects


def _case_token_budget(batch: list) -> int:
    """按本批预计要写多少条用例算输出预算，留 2 倍余量。

    原来固定 max_tokens=4096 要装下 8 个点铺开后的 15~24 条用例，必然截断；
    截断后整批退化成模板桩，且不上报 —— 这是「覆盖不全」的头号原因。
    """
    n = sum(len(_expected_aspects(p)) for p in batch) or len(batch)
    return max(CASE_TOKENS_FLOOR, min(CASE_TOKENS_CEIL, 600 + n * TOKENS_PER_CASE * 2))


def _failure_detail(reason: str, meta: dict) -> str:
    if reason == "truncated":
        return f"输出被 max_tokens 截断（finish_reason=length，已生成 {meta.get('completion_tokens') or '?'} token）"
    if reason == "parse_failed":
        return f"模型没有返回可解析的 JSON：{str(meta.get('content_preview') or '')[:80]}"
    if reason == "llm_error":
        return str(meta.get("error") or "模型调用失败")[:160]
    if reason == "deadline":
        return "撞到生成安全阀"
    return "模型返回了 JSON 但没有覆盖这些测试点"


def _point_aspect_gaps(target: list, rows: list) -> dict:
    """每个测试点还缺哪些情况。只认非桩用例 —— 模板桩不算覆盖。

    这是覆盖的唯一判据。以前是「这个点有没有任何用例」，于是一个人工只写了正向的点
    会被整体跳过，异常和边界永远不会被生成 —— 覆盖率显示满格，实际缺一半。
    """
    have: dict[str, set] = {}
    for row in rows or []:
        if not isinstance(row, dict) or str(row.get("origin") or "llm") == "stub":
            continue
        aspect = str(row.get("aspect") or "正向").strip() or "正向"
        for pid in row.get("point_ids") or []:
            have.setdefault(str(pid), set()).add(aspect)
    out: dict[str, list] = {}
    for p in target or []:
        pid = str(p.get("id") or "")
        missing = [a for a in _expected_aspects(p) if a not in have.get(pid, set())]
        if missing:
            out[pid] = missing
    return out


def _fill_cases(req: dict, target: list, rows: list, ctx: dict, usage: dict) -> tuple[list, dict, str, list]:
    """给每个测试点写用例，直到每个点该有的情况都齐。

    返回 (missing_points, last_meta, engine, failures)。failures 是本次**没写成**的
    测试点清单及原因 —— 以前这里是静默补桩，界面看不出差别，覆盖率照样 100%。
    """
    last_meta: dict = {}
    engine = "rule"
    missing: list = []
    failures: list = []
    started = time.monotonic()
    all_titles = [str(p.get("text") or "")[:40] for p in target if str(p.get("text") or "").strip()][:80]
    by_id = {str(p.get("id") or ""): p for p in target}

    # 逐批不变的共享上下文单独成一条 message：分片之间逐字节一致，provider 前缀缓存才可能命中。
    # 以前它和变化的 points 拼在同一个 JSON 里，每批重发一遍且缓存全不命中。
    stable_ctx = json.dumps({**ctx, "all_points": all_titles}, ensure_ascii=False, sort_keys=True)

    def ask(batch: list) -> tuple[Optional[dict], dict]:
        user = json.dumps(
            {
                "points": batch,
                "note": (
                    "只给本批 points 写用例。每个 point 的 need_aspects 列出这次必须补的情况，"
                    "每条用例的 aspect 必须取自该点的 need_aspects，一条用例只覆盖一个点的一种情况。"
                    "missing_points 仅在整张脑图都没有该场景时才报，最多 5 条。"
                ),
            },
            ensure_ascii=False,
        )
        return _ask_json(
            CASE_WRITER_SYSTEM_PROMPT,
            user,
            max_tokens=_case_token_budget(batch),
            timeout_sec=CASE_CALL_TIMEOUT_SEC,
            role="case-writer",
            job="draft_cases",
            stable=stable_ctx,
        )

    def fail_reason(meta: dict) -> str:
        kind = str((meta or {}).get("fail_kind") or "")
        if (meta or {}).get("truncated"):
            return "truncated"
        if kind == "http":
            return "llm_error"
        if kind == "parse":
            return "parse_failed"
        return kind or "incomplete"

    pending_ids = list(_point_aspect_gaps(target, rows).keys())
    work: list[tuple[list, int]] = [
        (pending_ids[i : i + CASE_BATCH], 0) for i in range(0, len(pending_ids), CASE_BATCH)
    ]
    cover_jobs.report(phase="draft_cases", label="正在写用例", done=0, total=len(work) or (1 if pending_ids else 0))

    while work:
        cover_jobs.check()
        if time.monotonic() - started > CASE_DEADLINE_SEC:
            dropped = [pid for ids, _ in work for pid in ids]
            if dropped:
                failures.append(
                    {
                        "reason": "deadline",
                        "point_ids": dropped,
                        "detail": f"超过 {CASE_DEADLINE_SEC}s 安全阀，剩余 {len(dropped)} 个测试点未生成",
                        "stubbed": False,
                    }
                )
            break

        # 这一轮的缺口快照。并发写入 rows 会打架，所以先并行问、再串行合并。
        gap_snap = _point_aspect_gaps(target, rows)

        def run(item: tuple[list, int]):
            ids, attempt = item
            batch = []
            for pid in ids:
                p = by_id.get(pid)
                if p and pid in gap_snap:
                    batch.append({**p, "need_aspects": gap_snap[pid]})
            if not batch:
                return ids, attempt, batch, None, {}
            parsed, meta = ask(batch)
            return ids, attempt, batch, parsed, meta or {}

        next_work: list[tuple[list, int]] = []
        for result in map_llm(run, work):
            if isinstance(result, Exception):
                if isinstance(result, cover_jobs.Cancelled):
                    raise result
                failures.append({"reason": "llm_error", "point_ids": [], "detail": str(result)[:160], "stubbed": False})
                cover_jobs.inc(1)
                continue
            ids, attempt, batch, parsed, meta = result
            last_meta = meta
            _add_usage(usage, meta)
            got: list = []
            if isinstance(parsed, dict):
                got = [x for x in (parsed.get("cases") or []) if isinstance(x, dict)]
                for item in parsed.get("missing_points") or []:
                    if isinstance(item, dict) and len(missing) < CASE_MAX_MISSING:
                        missing.append(item)
                if meta.get("engine") == "llm" and got:
                    engine = "llm"
            _append_unique_cases(rows, got, {str(p.get("id")) for p in batch})
            still = _point_aspect_gaps(target, rows)
            uncovered = [pid for pid in ids if pid in still]
            cover_jobs.inc(1, label=f"用例 · 已写 {len(rows)} 条")
            if not uncovered:
                continue
            reason = fail_reason(meta)
            if attempt + 1 < CASE_MAX_ATTEMPTS:
                if len(uncovered) > 1:
                    mid = max(1, len(uncovered) // 2)
                    next_work.append((uncovered[:mid], attempt + 1))
                    next_work.append((uncovered[mid:], attempt + 1))
                else:
                    next_work.append((uncovered, attempt + 1))
                continue
            for pid in uncovered:
                p = by_id.get(pid)
                if p:
                    _append_unique_cases(rows, _stub_cases_for_point(req, p, len(rows), aspects=still.get(pid)))
            failures.append(
                {
                    "reason": reason,
                    "point_ids": uncovered,
                    "detail": _failure_detail(reason, meta),
                    "stubbed": True,
                }
            )
        if next_work:
            cover_jobs.add_total(len(next_work))
        work = next_work

    return missing, last_meta, engine, failures


def _aspect_gaps(target: list, rows: list) -> list:
    """情况缺口的展示形态（给前端和 artifact 用）。判据见 _point_aspect_gaps。"""
    by_id = {str(p.get("id") or ""): p for p in target}
    return [
        {
            "point_id": pid,
            "text": str((by_id.get(pid) or {}).get("text") or ""),
            "missing_aspects": aspects,
        }
        for pid, aspects in _point_aspect_gaps(target, rows).items()
    ]


def _seed_cases(
    existing: list,
    *,
    replace: bool = False,
    point_ids: list | None = None,
    rewrite_stubs: bool = False,
) -> tuple[list, int]:
    """重试时哪些旧用例留下。锁定的永远留下。

    - replace：范围内未锁定的全部丢掉（整表重写或定点重写）
    - rewrite_stubs：范围内的模板桩丢掉，真用例留下 —— 「补写模板」走这条
    - 都不开：全部留下，只补缺口
    """
    want = {str(x).strip() for x in (point_ids or []) if str(x).strip()}
    kept: list[dict] = []
    locked_n = 0
    for row in existing or []:
        if not isinstance(row, dict):
            continue
        pids = {str(x) for x in (row.get("point_ids") or []) if x}
        in_scope = (not want) or bool(pids & want)
        if row.get("locked"):
            kept.append(row)
            locked_n += 1
            continue
        if not in_scope:
            kept.append(row)
            continue
        if replace:
            continue
        if rewrite_stubs and str(row.get("origin") or "llm") == "stub":
            continue
        kept.append(row)
    return kept, locked_n


def draft_cases(
    req: dict,
    cases: list | None = None,
    *,
    user_note: str = "",
    replace: bool = False,
    point_ids: list | None = None,
    rewrite_stubs: bool = False,
) -> dict:
    und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
    leaves = collect_mindmap_points(req.get("mindmap"))
    und_points = [p for p in (und.get("points") or []) if isinstance(p, dict)]
    target = _norm_points(leaves or und_points or _gap_points(req))
    want = {str(x).strip() for x in (point_ids or []) if str(x).strip()}
    if want:
        scoped = [p for p in target if str(p.get("id") or "") in want]
        # 指定的 id 一个都对不上就当没指定 —— 否则会静默写出 0 条，看起来像成功。
        if scoped:
            target = scoped
    note = str(user_note or req.get("analyst_feedback") or "").strip()
    ctx = _case_writer_context(req)
    ctx["retry_note"] = note or ctx.get("retry_note") or ""
    existing = [x for x in (req.get("draft_cases") or []) if isinstance(x, dict)]
    rows, kept_locked = _seed_cases(
        existing, replace=replace, point_ids=list(want) or None, rewrite_stubs=rewrite_stubs
    )
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    missing, last_meta, engine, failures = _fill_cases(req, target, rows, ctx, usage)
    mindmap = req.get("mindmap") if isinstance(req.get("mindmap"), dict) else {}
    if missing:
        mindmap = _graft_missing_points(mindmap, missing)
        extra_req = dict(req)
        extra_req["mindmap"] = mindmap
        extra_req = _sync_points_from_mindmap(extra_req)
        extra = _norm_points(collect_mindmap_points(extra_req.get("mindmap")))
        extra = [p for p in extra if p["id"] not in _case_point_ids(rows)]
        for p in extra:
            _append_unique_cases(rows, _stub_cases_for_point(extra_req, p, len(rows)))
        if extra:
            # 反推补出来的点是生成之后才加进脑图的，只有模板桩，必须让人知道要补写。
            failures.append(
                {
                    "reason": "backfilled_point",
                    "point_ids": [p["id"] for p in extra],
                    "detail": f"写用例时反推补了 {len(extra)} 个脑图测试点，这些点目前只有模板兜底",
                    "stubbed": True,
                }
            )
    last_meta = {**last_meta, **usage}
    real_rows = [r for r in rows if str(r.get("origin") or "llm") != "stub"]
    stub_rows = [r for r in rows if str(r.get("origin") or "llm") == "stub"]
    covered = len(_case_point_ids(real_rows))
    gaps = _aspect_gaps(target, rows)
    fully = len(target) - len(gaps)
    suggest = f"写了 {len(rows)} 条用例，{fully}/{len(target)} 个测试点情况齐全"
    if stub_rows:
        suggest += f"；{len(stub_rows)} 条是模板兜底，需要补写"
    if gaps:
        suggest += f"；{len(gaps)} 个点缺情况（正向/异常/边界）"
    if missing:
        suggest += f"；反推补了 {len(missing)} 个脑图测试点"
    if kept_locked:
        suggest += f"；保留了 {kept_locked} 条人工锁定用例"
    payload = {
        "cases": rows,
        "missing_points": missing,
        "failures": failures,
        "aspect_gaps": gaps,
        "stats": {
            "points": len(target),
            "cases": len(rows),
            "real_cases": len(real_rows),
            "stub_cases": len(stub_rows),
            "covered_points": covered,
            "fully_covered_points": fully,
            "locked_kept": kept_locked,
        },
    }
    if missing:
        payload["mindmap"] = mindmap
    return _with_engine(
        artifact(
            job="draft_cases",
            suggest=suggest,
            citations=[req.get("id") or ""],
            payload=payload,
            input_hash=_hash(json.dumps({"ids": [p["id"] for p in target], "note": note}, ensure_ascii=False)),
        ),
        engine if rows else "none",
        last_meta,
    )


def apply_analyze(req: dict, payload: dict) -> dict:
    next_req = dict(req)
    next_req["analyze_failures"] = [x for x in (payload.get("failures") or []) if isinstance(x, dict)]
    und = dict(next_req.get("understanding") or {})
    ac = [str(x).strip() for x in (payload.get("ac") or []) if str(x).strip()]
    if ac:
        und["ac"] = ac
    points = []
    old = {str(p.get("id")): p for p in (und.get("points") or []) if isinstance(p, dict)}
    for i, p in enumerate(payload.get("points") or []):
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or f"tp{i + 1}")
        prev = old.get(pid) or {}
        points.append(
            {
                "id": pid,
                "kind": p.get("kind") or prev.get("kind") or "正向",
                "text": p.get("text") or prev.get("text") or "",
                "detail": p.get("detail") or prev.get("detail") or "",
                "path": p.get("path") or prev.get("path") or [],
                "platform": str(p.get("platform") or prev.get("platform") or ""),
                "case_ids": list(prev.get("case_ids") or []),
                "waived": bool(prev.get("waived")),
            }
        )
    if points:
        und["points"] = points
    if not und.get("source_excerpt"):
        und["source_excerpt"] = str(req.get("source_text") or _source_text(req) or "")[:20000]
    und["extracted_at"] = _now()
    und["engine"] = "llm" if payload else und.get("engine") or "rule"
    if payload.get("change_kind"):
        und["change_kind"] = str(payload.get("change_kind") or "")
    if payload.get("baseline"):
        und["baseline"] = str(payload.get("baseline") or "")
    if payload.get("delta"):
        und["delta"] = str(payload.get("delta") or "")
    for key in ("journeys", "new_features", "keep_features", "exceptions", "surfaces"):
        rows = _as_dict_list(payload.get(key))
        if rows:
            und[key] = rows
    impact = payload.get("impact") if isinstance(payload.get("impact"), dict) else {}
    prev_impact = und.get("impact") if isinstance(und.get("impact"), dict) else {}
    und["impact"] = {
        "platforms": list(impact.get("platforms") or prev_impact.get("platforms") or []),
        "notes": impact.get("notes") or prev_impact.get("notes") or "",
        "modules": list(impact.get("modules") or prev_impact.get("modules") or []),
        "features": list(impact.get("features") or prev_impact.get("features") or []),
        "e2e": bool(impact["e2e"]) if "e2e" in impact else bool(prev_impact.get("e2e")),
        "how_to_run": str(impact.get("how_to_run") or prev_impact.get("how_to_run") or ""),
    }
    hang = payload.get("hang") if isinstance(payload.get("hang"), dict) else {}
    creates = payload.get("atlas_create") if isinstance(payload.get("atlas_create"), list) else []
    paths = []
    for item in hang.get("paths") or []:
        if isinstance(item, (list, tuple)):
            parts = [str(x).strip() for x in item if str(x).strip()]
        else:
            parts = [p.strip() for p in str(item).replace(">", "/").replace("-", "/").split("/") if p.strip()]
        if parts:
            paths.append(parts)
    next_req["atlas_intent"] = {
        "hang": {
            "paths": paths,
            "module_names": [str(x).strip() for x in (hang.get("module_names") or []) if str(x).strip()],
            "feature_names": [str(x).strip() for x in (hang.get("feature_names") or []) if str(x).strip()],
        },
        "create": [x for x in creates if isinstance(x, dict)],
    }
    if payload.get("risks"):
        und["stale_risks"] = [str(x) for x in payload.get("risks") if x]
    next_req["understanding"] = und
    next_req["summary"] = payload.get("summary") or next_req.get("summary") or next_req.get("title")
    feats = []
    for i, f in enumerate(payload.get("features") or []):
        if not isinstance(f, dict):
            continue
        name = str(f.get("name") or "").strip()
        if name:
            feats.append({"id": f.get("id") or f"feat-{i + 1}", "name": name, "notes": f.get("notes") or ""})
    if feats:
        next_req["features"] = feats
    next_req["updated_at"] = _now()
    return next_req


def apply_cases(req: dict, payload: dict, *, replace: bool = False) -> dict:
    next_req = dict(req)
    rows = []
    missing = []
    if isinstance(payload, dict):
        rows = [x for x in (payload.get("cases") or []) if isinstance(x, dict)]
        missing = [x for x in (payload.get("missing_points") or []) if isinstance(x, dict)]
        if isinstance(payload.get("mindmap"), dict) and payload["mindmap"].get("children"):
            next_req["mindmap"] = payload["mindmap"]
        elif missing:
            next_req["mindmap"] = _graft_missing_points(next_req.get("mindmap") or {}, missing)
        if missing:
            next_req = _sync_points_from_mindmap(next_req)
            next_req["mindmap_backfill"] = missing
        # 生成失败清单和情况缺口落到需求上，前端据此显红并允许定点补写。
        next_req["case_failures"] = [x for x in (payload.get("failures") or []) if isinstance(x, dict)]
        next_req["case_aspect_gaps"] = [x for x in (payload.get("aspect_gaps") or []) if isinstance(x, dict)]
        next_req["case_stats"] = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
    next_req["draft_cases"] = [_normalize_draft_case(row, i) for i, row in enumerate(rows)]
    und = dict(next_req.get("understanding") or {})
    points = []
    row_ids = {str(r.get("case_id") or "") for r in next_req["draft_cases"]}
    for p in und.get("points") or []:
        if not isinstance(p, dict):
            continue
        p = dict(p)
        # 清掉已经不存在的 draft 用例 id（以前会残留，让覆盖率虚高）；
        # 非 draft 前缀的是用例库里的真用例链接，不动。
        hung = (
            []
            if replace
            else [
                cid
                for cid in (p.get("case_ids") or [])
                if not str(cid).startswith("draft-") or str(cid) in row_ids
            ]
        )
        for row in next_req["draft_cases"]:
            if p.get("id") in (row.get("point_ids") or []) and row.get("case_id") and row.get("case_id") not in hung:
                hung.append(row.get("case_id"))
        p["case_ids"] = hung
        points.append(p)
    if points:
        und["points"] = points
        next_req["understanding"] = und
    next_req["updated_at"] = _now()
    return next_req


def propose_atlas(qa_process: dict, cases: list | None = None, *, user_note: str = "") -> dict:
    current = atlas.normalize_atlas(qa_process.get("app_atlas"))
    reqs = [r for r in (qa_process.get("requirements") or []) if isinstance(r, dict)]
    module_hints = []
    seen_mod = set()
    for c in cases or []:
        if not isinstance(c, dict):
            continue
        module = str(c.get("module") or "").strip()
        if not module or module in seen_mod:
            continue
        seen_mod.add(module)
        module_hints.append(
            {
                "module": module,
                "cases": [
                    str(x.get("name") or x.get("title") or "")
                    for x in (cases or [])
                    if isinstance(x, dict) and str(x.get("module") or "") == module
                ][:8],
            }
        )
        if len(module_hints) >= 16:
            break
    user = json.dumps(
        {
            "current_atlas": atlas.compact_atlas(current),
            "requirements": [
                {
                    "id": r.get("id"),
                    "title": r.get("title"),
                    "summary": r.get("summary"),
                    "features": r.get("features") or [],
                    "atlas_intent": r.get("atlas_intent") or {},
                    "module_ids": r.get("module_ids") or [],
                    "feature_ids": r.get("feature_ids") or [],
                    "change_kind": (r.get("understanding") or {}).get("change_kind") if isinstance(r.get("understanding"), dict) else "",
                    "baseline": (r.get("understanding") or {}).get("baseline") if isinstance(r.get("understanding"), dict) else "",
                    "delta": (r.get("understanding") or {}).get("delta") if isinstance(r.get("understanding"), dict) else "",
                    "impact": (r.get("understanding") or {}).get("impact") if isinstance(r.get("understanding"), dict) else {},
                    "analyst_feedback": r.get("analyst_feedback") or "",
                }
                for r in reqs[:40]
            ],
            "existing_cases": [
                {
                    "case_id": c.get("case_id"),
                    "name": c.get("name") or c.get("title"),
                    "module": c.get("module"),
                }
                for c in (cases or [])[:40]
            ],
            "draft_cases": [
                {"case_id": c.get("case_id"), "name": c.get("name"), "module": c.get("module")}
                for r in reqs[:20]
                for c in (r.get("draft_cases") or [])
                if isinstance(c, dict)
            ][:40],
            "module_hints": module_hints,
            "human_feedback": str(user_note or "").strip(),
            "note": "module_hints 只是参考，不要当成权威模块划分。优化需求优先挂已有节点。有 human_feedback 必须按人的理解重提。",
        },
        ensure_ascii=False,
    )
    parsed, meta = _ask_json(REQ_ANALYST_IMPACT_PROMPT, user, max_tokens=4500, role="req-analyst", job="propose_atlas")
    case_changes = []
    if parsed:
        after = atlas.merge_payload(current, parsed, reqs)
        reason = str(parsed.get("reason") or "建议更新应用图谱或相关用例")
        if user_note:
            reason = f"{reason}｜按人的补充重提"
        engine = "llm"
        case_changes = [x for x in (parsed.get("case_changes") or []) if isinstance(x, dict)]
        if not case_changes:
            case_changes = atlas.rule_case_changes(current, after, cases or [], reqs)
    else:
        after = atlas.rule_propose(current, reqs, cases or [])
        reason = "规则草稿：按需求功能点搭骨架，用例模块仅作参考"
        engine = "rule"
        meta = meta or {}
        case_changes = atlas.rule_case_changes(current, after, cases or [], reqs)
    return _with_engine(
        artifact(
            job="propose_atlas",
            suggest=reason,
            citations=[r.get("id") or "" for r in reqs if r.get("id")],
            payload={"atlas": after, "reason": reason, "case_changes": case_changes},
            input_hash=_hash(user),
        ),
        engine if atlas.atlas_has_nodes(after) or atlas.atlas_has_nodes(current) or case_changes else "none",
        meta,
    )


def run_llm_job(job: str, *, requirement=None, cases=None, qa_process=None) -> dict:
    req = requirement or {}
    if job == "analyze_req":
        return analyze_req(req, cases or [], (qa_process or {}).get("app_atlas"))
    if job == "draft_mindmap":
        return draft_mindmap(req, cases or [], (qa_process or {}).get("app_atlas"))
    if job == "propose_atlas":
        return propose_atlas(qa_process or {}, cases or [])
    return draft_cases(req, cases or [])


def _autonomy(doc: dict) -> dict:
    raw = doc.get("autonomy") if isinstance(doc.get("autonomy"), dict) else {}
    return {
        "enabled": raw.get("enabled", True) is not False,
        "auto_analyze": raw.get("auto_analyze", True) is not False,
        "auto_mindmap": raw.get("auto_mindmap", True) is not False,
        "auto_cases": raw.get("auto_cases", True) is not False,
        "auto_atlas": raw.get("auto_atlas", True) is not False,
        "auto_dispatch": raw.get("auto_dispatch") is True,
    }


def _add_usage(total: dict, extra) -> None:
    if not isinstance(extra, dict):
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        total[key] = int(total.get(key) or 0) + int(extra.get(key) or 0)
    if extra.get("engine") == "llm":
        total["engine"] = "llm"


_ROLE_CN = {
    "conductor": "分析师",
    "req-analyst": "需求分析师",
    "mindmap-writer": "测试脑图编写",
    "case-writer": "测试用例编写",
    "req-qa-bm": "需求QA BM",
    "version-qa-bm": "版本QA BM",
    "test-engineer": "测试工程师",
    "doc-keeper": "文档维护",
    "report-writer": "报告编写",
}

_SKILL_CN = {
    "analyze_req": "拆验收标准",
    "propose_atlas": "建议图谱",
    "draft_mindmap": "写测试脑图",
    "draft_cases": "写用例草稿",
    "map_cases": "对照用例库",
    "draft_sign": "验收草稿",
    "pick_regression": "圈回归范围",
    "draft_gate": "发版草稿",
    "pick_device": "申请执行设备",
    "pick_account": "租账号",
}


def _routed_from_actions(actions: list) -> list[dict]:
    out = []
    seen = set()
    for item in actions or []:
        if not isinstance(item, dict):
            continue
        skill = str(item.get("action") or item.get("job") or "").strip()
        role = str(item.get("role") or "").strip()
        if not skill or skill in ("skip", "skipped", "blocked"):
            continue
        key = (role, skill)
        if key in seen:
            continue
        seen.add(key)
        out.append({"role": role, "skill": skill})
    return out


def _route_detail(routed: list[dict]) -> str:
    if not routed:
        return "分析师理解任务后，没有需要调用的技能"
    bits = [
        f"{_ROLE_CN.get(row['role'], row['role'] or '角色')} · {_SKILL_CN.get(row['skill'], row['skill'])}"
        for row in routed
    ]
    return "分析师理解任务后调用：" + " → ".join(bits)


def tick(*, qa_process: dict, cases: list | None = None, requirement_id: str = "", requirement_ids: list | None = None, app_id: str = "", app_name: str = "", user_note: str = "", force: bool = False, jobs: list | None = None, source: str = "", point_ids: list | None = None, rewrite_stubs: bool = False, replace_cases: bool = False) -> dict:
    """推进未完成的分析/脑图/用例草稿。不改验收门禁、不自动下发设备。jobs 指定时按重试处理。"""
    tok = dispatch.bind(
        trigger="qa_tick",
        source=source or "continue_analysis",
        routed_by="conductor",
        app_id=app_id,
        app_name=app_name,
        pipeline_id=dispatch.new_pipeline_id(),
    )
    try:
        result = _tick_body(
            qa_process=qa_process,
            cases=cases,
            requirement_id=requirement_id,
            requirement_ids=requirement_ids,
            user_note=user_note,
            force=force,
            jobs=jobs,
            point_ids=point_ids,
            rewrite_stubs=rewrite_stubs,
            replace_cases=replace_cases,
        )
        actions = result.get("actions") or []
        did = [a for a in actions if a.get("action") not in ("skip", "skipped", "blocked", "")]
        routed = _routed_from_actions(did)
        dispatch.record_job(
            status="done",
            job="route",
            role="conductor",
            skill="",
            routed=routed,
            detail=_route_detail(routed),
            input_data={"requirement_id": requirement_id or ""},
            output_data={"actions": [a.get("action") for a in did], "routed": routed},
        )
        return result
    except cover_jobs.Cancelled:
        dispatch.record_job(status="cancelled", job="route", role="conductor", detail="已取消")
        raise
    except Exception as e:
        dispatch.record_job(status="error", job="route", role="conductor", error=str(e)[:240])
        raise
    finally:
        dispatch.reset(tok)


def _tick_flush(doc: dict, reqs: list, req: dict, rid: str) -> None:
    """分片写完就回写，进程崩了也不丢已经生成的脑图/用例。"""
    for i, row in enumerate(reqs):
        if str(row.get("id") or "") == rid:
            reqs[i] = req
            break
    doc["requirements"] = reqs
    cover_jobs.save(doc)


def _tick_body(*, qa_process: dict, cases: list | None = None, requirement_id: str = "", requirement_ids: list | None = None, user_note: str = "", force: bool = False, jobs: list | None = None, point_ids: list | None = None, rewrite_stubs: bool = False, replace_cases: bool = False) -> dict:
    """推进未完成的分析/脑图/用例草稿。不改验收门禁、不自动下发设备。"""
    doc = dict(qa_process or {})
    reqs = [dict(r) for r in (doc.get("requirements") or []) if isinstance(r, dict)]
    auto = _autonomy(doc)
    log = [x for x in (doc.get("role_log") or []) if isinstance(x, dict)]
    actions: list[dict] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    target = str(requirement_id or "").strip()
    want = {str(x).strip() for x in (requirement_ids or []) if str(x).strip()}
    if target:
        want.add(target)
    note = str(user_note or "").strip()
    retry_jobs = {str(x).strip() for x in (jobs or []) if str(x).strip() in LLM_JOBS}
    # jobs 写了就只跑写了的那些。以前「重试脑图 + 评论」会自动扩散成
    # analyze_req → draft_mindmap → draft_cases 且 replace=True，把人改过的用例整表删掉，
    # 一轮 10 分钟。评论作为 user_note 交给被点的那个角色就够了。
    rewrite_stubs = bool(rewrite_stubs)
    replace_cases = bool(replace_cases)
    scope_ids = [str(x).strip() for x in (point_ids or []) if str(x).strip()]

    if not auto["enabled"]:
        return {"qa_process": doc, "actions": [{"role": "system", "action": "skipped", "detail": "autonomy.enabled=false"}]}

    for i, req in enumerate(reqs):
        if want and str(req.get("id") or "") not in want:
            continue
        rid = str(req.get("id") or "")
        text = _source_text(req)
        if not text:
            actions.append({"role": "req-analyst", "req_id": rid, "action": "skip", "detail": "没有需求原文"})
            continue
        src_hash = _hash(text)
        und = req.get("understanding") if isinstance(req.get("understanding"), dict) else {}
        if note:
            req["analyst_feedback"] = note
        gate = str(req.get("gate") or "")
        allowed = set(retry_jobs) if retry_jobs else _jobs_for_step(doc, "req", gate)
        if force and not retry_jobs:
            allowed |= {"analyze_req", "propose_atlas"}

        if "analyze_req" in allowed and (bool(retry_jobs) or auto["auto_analyze"]) and (
            force or retry_jobs or und.get("source_hash") != src_hash
        ):
            cover_jobs.report(phase="analyze_req", label="正在分析需求")
            art = analyze_req(req, cases or [], doc.get("app_atlas"), user_note=note)
            req = apply_analyze(req, art.get("payload") or {})
            und = dict(req.get("understanding") or {})
            und["engine"] = art.get("engine") or "rule"
            und["source_hash"] = src_hash
            req["understanding"] = und
            req["ai_artifacts"] = [*(req.get("ai_artifacts") or []), art][-12:]
            actions.append({"role": "req-analyst", "req_id": rid, "action": "analyze_req", "engine": art.get("engine"), "step_id": gate})
            log.append({"at": _now(), "role": "req-analyst", "job": "analyze_req", "req_id": rid, "engine": art.get("engine"), "step_id": gate, "output": art.get("suggest") or "已拆验收标准"})
            _add_usage(usage, art.get("usage"))
            _tick_flush(doc, reqs, req, rid)

        want_map = "draft_mindmap" in allowed and (bool(retry_jobs) or auto["auto_mindmap"])
        empty_map = not (isinstance(req.get("mindmap"), dict) and req["mindmap"].get("children"))
        if want_map and (retry_jobs or (not force and empty_map)):
            # 重试对话框里的评论 → 知识库直接过审（不是脑图快照）
            if note and "draft_mindmap" in retry_jobs:
                saved = _capture_mindmap_retry_note(note=note, req=req)
                if saved:
                    actions.append(
                        {
                            "role": "knowledge",
                            "req_id": rid,
                            "action": "capture_retry_note",
                            "knowledge_id": saved.get("id") or "",
                            "step_id": gate,
                        }
                    )
            art = draft_mindmap(req, cases or [], doc.get("app_atlas"), user_note=note)
            req = _apply_cover_art(req, art, job="draft_mindmap", user_note=note, replace=bool(retry_jobs))
            actions.append({"role": "mindmap-writer", "req_id": rid, "action": "draft_mindmap", "engine": art.get("engine"), "step_id": gate})
            log.append({"at": _now(), "role": "mindmap-writer", "job": "draft_mindmap", "req_id": rid, "engine": art.get("engine"), "step_id": gate, "output": art.get("suggest") or "已写脑图"})
            _add_usage(usage, art.get("usage"))
            _tick_flush(doc, reqs, req, rid)

        want_cases = "draft_cases" in allowed and (bool(retry_jobs) or auto["auto_cases"])
        empty_cases = not req.get("draft_cases")
        if want_cases and (retry_jobs or (not force and empty_cases)):
            # 重试用例默认只扔掉模板桩再补缺口，已有真用例和锁定用例都不动。
            # 整表重写必须显式传 replace_cases；定点重写传 point_ids。
            drop_stubs = rewrite_stubs or (bool(retry_jobs) and "draft_cases" in retry_jobs and not replace_cases)
            art = draft_cases(
                req,
                cases or [],
                user_note=note,
                replace=replace_cases or bool(scope_ids),
                point_ids=scope_ids or None,
                rewrite_stubs=drop_stubs and not replace_cases and not scope_ids,
            )
            req = _apply_cover_art(req, art, job="draft_cases", user_note=note, replace=replace_cases)
            actions.append({"role": "case-writer", "req_id": rid, "action": "draft_cases", "engine": art.get("engine"), "step_id": gate})
            log.append({"at": _now(), "role": "case-writer", "job": "draft_cases", "req_id": rid, "engine": art.get("engine"), "step_id": gate, "output": art.get("suggest") or "已写用例草稿"})
            _add_usage(usage, art.get("usage"))
            _tick_flush(doc, reqs, req, rid)

        reqs[i] = req
        req["_allowed_jobs"] = list(allowed)

    atlas_step = next((str(r.get("gate") or "") for r in reqs if "propose_atlas" in (r.get("_allowed_jobs") or []) and (not want or str(r.get("id") or "") in want)), "")
    for r in reqs:
        r.pop("_allowed_jobs", None)
    doc["requirements"] = reqs
    patches = [x for x in (doc.get("atlas_patches") or []) if isinstance(x, dict)]
    current_atlas = atlas.normalize_atlas(doc.get("app_atlas"))
    analyzed = any(a.get("action") == "analyze_req" for a in actions)
    if retry_jobs and "propose_atlas" not in retry_jobs:
        allow_atlas = False
    else:
        allow_atlas = bool(atlas_step) or force
    need_atlas = allow_atlas and atlas.has_seed_material(reqs, cases or []) and (
        not atlas.atlas_has_nodes(current_atlas) or analyzed or force or atlas.intent_needs_patch(reqs, current_atlas)
    )
    if auto.get("auto_atlas") is not False and need_atlas and (not atlas.pending_patches(patches) or force):
        cover_jobs.report(phase="propose_atlas", label="正在建议应用图谱")
        art = propose_atlas({"app_atlas": current_atlas, "requirements": reqs}, cases or [], user_note=note)
        after = (art.get("payload") or {}).get("atlas") or {}
        reason = (art.get("payload") or {}).get("reason") or art.get("suggest") or "建议更新应用图谱"
        patch = atlas.enqueue_patch(
            patches,
            before=current_atlas,
            after=after,
            reason=reason,
            source={
                "req_id": target or next((str(a.get("req_id") or "") for a in actions if a.get("req_id")), ""),
                "engine": art.get("engine"),
                "human_feedback": note,
            },
            reqs=reqs,
            case_changes=(art.get("payload") or {}).get("case_changes") or [],
            force=bool(note or force),
        )
        if patch:
            actions.append(
                {
                    "role": "req-analyst",
                    "action": "propose_atlas",
                    "engine": art.get("engine"),
                    "detail": f"{len(patch.get('lines') or patch.get('diff') or [])} 处骨架 · {len(patch.get('case_changes') or [])} 条用例待确认",
                }
            )
            log.append({
                "at": _now(),
                "role": "req-analyst",
                "job": "propose_atlas",
                "engine": art.get("engine"),
                "req_id": target or next((str(a.get("req_id") or "") for a in actions if a.get("req_id")), ""),
                "step_id": atlas_step,
                "output": reason,
            })
            _add_usage(usage, art.get("usage"))
        elif art.get("engine") and art.get("engine") != "none":
            actions.append({"role": "req-analyst", "action": "skip", "detail": "影响范围无需变更"})

    doc["app_atlas"] = current_atlas
    doc["atlas_patches"] = patches[-20:]
    doc["features"] = atlas.flatten_features(current_atlas, reqs)
    doc["autonomy"] = auto
    doc["role_log"] = log[-80:]
    doc["updated_at"] = _now()
    if auto.get("auto_dispatch"):
        actions.append({"role": "test-engineer", "action": "blocked", "detail": "自动下发未开：需要设备选择，避免误跑"})
    return {"qa_process": doc, "actions": actions, "autonomy": auto, "usage": usage}


def run_followup_pipeline(
    *,
    qa_process: dict,
    cases: list | None = None,
    requirement_ids: list | None = None,
    trigger: str = "atlas_edit",
    app_id: str = "",
    app_name: str = "",
    pipeline_id: str = "",
    force: bool = False,
) -> dict:
    """骨架确认或人手改完之后：缺脑图就写脑图，缺用例就写用例。已有内容默认不重跑。"""
    doc = dict(qa_process or {})
    reqs = [dict(r) for r in (doc.get("requirements") or []) if isinstance(r, dict)]
    want = {str(x).strip() for x in (requirement_ids or []) if str(x).strip()}
    if not want:
        want = {str(r.get("id") or "") for r in reqs if r.get("id")}
    targets = [r for r in reqs if str(r.get("id") or "") in want]
    pipeline_id = pipeline_id or dispatch.new_pipeline_id()
    planned = 0
    for req in targets:
        has_map = isinstance(req.get("mindmap"), dict) and bool(req["mindmap"].get("children"))
        has_cases = bool(req.get("draft_cases"))
        if force or not has_map:
            planned += 1
        if force or not has_cases:
            planned += 1
    tok = dispatch.bind(
        trigger=trigger,
        source=trigger,
        routed_by="conductor",
        app_id=app_id,
        app_name=app_name,
        pipeline_id=pipeline_id,
        step_total=max(1, planned),
    )
    actions: list[dict] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    log = [x for x in (doc.get("role_log") or []) if isinstance(x, dict)]
    step = 0
    by_id = {str(r.get("id") or ""): i for i, r in enumerate(reqs)}
    try:
        for req in targets:
            rid = str(req.get("id") or "")
            idx = by_id.get(rid)
            if idx is None:
                continue
            has_map = isinstance(req.get("mindmap"), dict) and bool(req["mindmap"].get("children"))
            has_cases = bool(req.get("draft_cases"))
            if force or not has_map:
                step += 1
                dispatch.bind(step_index=step, role="mindmap-writer", job="draft_mindmap", skill="draft_mindmap")
                art = draft_mindmap(req, cases or [], doc.get("app_atlas"))
                req = _apply_cover_art(req, art, job="draft_mindmap", replace=force)
                actions.append({"role": "mindmap-writer", "req_id": rid, "action": "draft_mindmap", "engine": art.get("engine")})
                log.append({"at": _now(), "role": "mindmap-writer", "job": "draft_mindmap", "req_id": rid, "engine": art.get("engine"), "pipeline_id": pipeline_id})
                _add_usage(usage, art.get("usage"))
            if force or not has_cases:
                step += 1
                dispatch.bind(step_index=step, role="case-writer", job="draft_cases", skill="draft_cases")
                art = draft_cases(req, cases or [], replace=force)
                req = _apply_cover_art(req, art, job="draft_cases", replace=force)
                actions.append({"role": "case-writer", "req_id": rid, "action": "draft_cases", "engine": art.get("engine")})
                log.append({"at": _now(), "role": "case-writer", "job": "draft_cases", "req_id": rid, "engine": art.get("engine"), "pipeline_id": pipeline_id})
                _add_usage(usage, art.get("usage"))
            reqs[idx] = req
        dispatch.bind(step_index=0, role="req-analyst", job="atlas_followup")
        dispatch.record_job(
            status="done",
            job="atlas_followup",
            role="req-analyst",
            detail=f"已补 {len(actions)} 步：脑图/用例" if actions else "这条需求已有脑图和用例，没有重跑",
            output_data={"actions": [a.get("action") for a in actions], "requirement_ids": list(want)},
        )
    except Exception as e:
        dispatch.bind(step_index=0, role="req-analyst", job="atlas_followup")
        dispatch.record_job(status="error", job="atlas_followup", role="req-analyst", error=str(e)[:240])
        raise
    finally:
        dispatch.reset(tok)
    doc["requirements"] = reqs
    doc["features"] = atlas.flatten_features(doc.get("app_atlas"), reqs)
    doc["role_log"] = log[-80:]
    doc["updated_at"] = _now()
    return {"qa_process": doc, "actions": actions, "usage": usage, "pipeline_id": pipeline_id, "autonomy": _autonomy(doc)}

