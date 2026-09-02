# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""知识情境卡：分面 + 场景 + 绑定槽。

执行按卡取，不按用例原文撞词。槽是跨 App / Web 的产业字段，应用私有值写在知识里。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Optional

from script.log import SLog

TAG = "KnowledgeSituation"

FACETS = ("chrome", "server", "hybrid", "exception")
SURFACES = ("app", "web")
LANES = ("prep", "step", "expect")
NEEDS = ("fill", "judge_selected", "judge", "howto")
SCREEN_ROLES = ("chrome_nav", "auth_form", "content", "dialog")
SLOTS = ("identity.otp", "identity.phone", "identity.password")
ENVS = ("test", "staging", "prod")

HITL_FIELD_TO_SLOT = {
    "sms_code": "identity.otp",
    "phone": "identity.phone",
}

_ENV_CANON = {
    "test": "test",
    "testing": "test",
    "dev": "test",
    "qa": "test",
    "测试": "test",
    "staging": "staging",
    "stg": "staging",
    "pre": "staging",
    "预发": "staging",
    "prod": "prod",
    "production": "prod",
    "live": "prod",
    "正式": "prod",
    "生产": "prod",
}

EXTRACT_SYSTEM = """You classify a knowledge article about any app or website for a test agent.
Return one JSON object, no markdown.
Do not assume a particular product. Do not copy UI labels into facet/need/slot enums.

{
  "facet": "chrome | server | hybrid | exception | ",
  "situation": {
    "surface": "app | web | ",
    "lane": "prep | step | expect | ",
    "need": "fill | judge_selected | judge | howto | ",
    "slot": "identity.otp | identity.phone | identity.password | ",
    "screen_role": "chrome_nav | auth_form | content | dialog | "
  },
  "bind": {
    "slot": "identity.otp | identity.phone | identity.password | ",
    "value": "string to type into a field, or empty",
    "env": "test | staging | prod | ",
    "surface": "app | web | "
  }
}

facet:
- chrome: shell / navigation / selected state / independent entry
- server: observable result of backend rules (counts, timing, state)
- hybrid: one action must match both UI and backend
- exception: lab secrets, known defects, must-ask-human doors

need fill + bind.value: a reusable value the agent can type (one-time code, login id, password).
identity.otp = one-time code (sms, email, authenticator, fixed lab code).
identity.phone = login identifier (phone, email, username).
identity.password = password.
If the text does not give a concrete fillable value, leave bind.value empty.
If env is implied (lab / test only), set env=test.
Empty string means unrestricted.
"""

EXTRACT_BATCH_SYSTEM = EXTRACT_SYSTEM + """
Input is a JSON list of {id, title, content}.
Return {"items":[{id, facet, situation, bind}, ...]}. Keep the same ids. Skip empty articles.
"""

SIT_MATCH_USED = 3
BACKFILL_BATCH = 8


def slot_for_hitl_field(field: str) -> str:
    return HITL_FIELD_TO_SLOT.get(str(field or "").strip().lower(), "")


def content_fingerprint(title: str, content: str) -> str:
    raw = f"{(title or '').strip()}\n{(content or '').strip()}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def canon_env(raw: str) -> str:
    s = str(raw or "").strip().lower()
    if not s:
        return ""
    return _ENV_CANON.get(s, s if s in ENVS else s)


def _pick(raw: Any, allowed: Iterable[str]) -> str:
    s = str(raw or "").strip().lower()
    return s if s in set(allowed) else ""


def normalize_situation(raw: Any) -> Dict[str, str]:
    src = raw if isinstance(raw, dict) else {}
    sit = {
        "surface": _pick(src.get("surface"), SURFACES),
        "lane": _pick(src.get("lane"), LANES),
        "need": _pick(src.get("need"), NEEDS),
        "slot": _pick(src.get("slot"), SLOTS),
        "screen_role": _pick(src.get("screen_role"), SCREEN_ROLES),
    }
    return {k: v for k, v in sit.items() if v}


def normalize_bind(raw: Any) -> Dict[str, str]:
    src = raw if isinstance(raw, dict) else {}
    slot = _pick(src.get("slot"), SLOTS)
    value = str(src.get("value") or "").strip()
    env = canon_env(src.get("env"))
    if env not in ENVS:
        env = ""
    surface = _pick(src.get("surface"), SURFACES)
    bind: Dict[str, str] = {}
    if slot:
        bind["slot"] = slot
    if value:
        bind["value"] = value
    if env:
        bind["env"] = env
    if surface:
        bind["surface"] = surface
    if bind.get("value") and not bind.get("slot"):
        bind["slot"] = "identity.otp"
    return bind


def normalize_facet(raw: Any) -> str:
    return _pick(raw, FACETS)


def bind_slot_of(item: Dict[str, Any]) -> str:
    bind = normalize_bind(item.get("bind"))
    if bind.get("slot"):
        return bind["slot"]
    sit = normalize_situation(item.get("situation"))
    return sit.get("slot") or ""


def situation_fingerprint_of(item: Dict[str, Any]) -> str:
    return str(item.get("situation_fp") or "").strip()


def apply_card(item: Dict[str, Any], card: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(item)
    facet = normalize_facet(card.get("facet"))
    sit = normalize_situation(card.get("situation"))
    bind = normalize_bind(card.get("bind"))
    if bind.get("slot") and not sit.get("slot"):
        sit["slot"] = bind["slot"]
    if bind.get("value") and not sit.get("need"):
        sit["need"] = "fill"
    if bind.get("value") and not facet:
        facet = "exception"
    if bind.get("surface") and not sit.get("surface"):
        sit["surface"] = bind["surface"]
    if bind.get("value") and sit.get("surface") and not bind.get("surface"):
        bind["surface"] = sit["surface"]
    if facet:
        row["facet"] = facet
    elif "facet" in row and not facet:
        row.pop("facet", None)
    if sit:
        row["situation"] = sit
    else:
        row.pop("situation", None)
    if bind:
        row["bind"] = bind
    else:
        row.pop("bind", None)
    return row


def parse_extract_payload(raw: Any) -> Dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    if isinstance(src.get("items"), list) and src["items"]:
        first = src["items"][0] if isinstance(src["items"][0], dict) else {}
        src = {**src, **first}
    return {
        "facet": normalize_facet(src.get("facet")),
        "situation": normalize_situation(src.get("situation")),
        "bind": normalize_bind(src.get("bind")),
    }


def _card_from_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "facet": normalize_facet(item.get("facet")),
        "situation": normalize_situation(item.get("situation")),
        "bind": normalize_bind(item.get("bind")),
    }


def _incoming_has_card_key(item: Dict[str, Any]) -> bool:
    return any(k in item for k in ("facet", "situation", "bind"))


def env_matches(bind_env: str, run_env: str) -> bool:
    want = canon_env(bind_env)
    have = canon_env(run_env)
    if not want:
        return True
    if not have:
        return False
    return want == have


def surface_matches(item_surface: str, run_surface: str) -> bool:
    want = _pick(item_surface, SURFACES)
    have = _pick(run_surface, SURFACES)
    if not want or not have:
        return True
    return want == have


def situation_overlap(item: Dict[str, Any], scene: Optional[Dict[str, Any]]) -> int:
    scene = scene if isinstance(scene, dict) else {}
    sit = normalize_situation(item.get("situation"))
    bind = normalize_bind(item.get("bind"))
    facet = normalize_facet(item.get("facet"))
    score = 0

    item_slot = bind.get("slot") or sit.get("slot") or ""
    scene_slot = _pick(scene.get("slot"), SLOTS)
    if scene_slot and item_slot == scene_slot:
        score += 6
    elif scene_slot and item_slot and item_slot != scene_slot:
        score -= 3

    pairs = (
        ("need", sit.get("need"), _pick(scene.get("need"), NEEDS), 3, 2),
        ("lane", sit.get("lane"), _pick(scene.get("lane"), LANES), 2, 2),
        ("facet", facet, _pick(scene.get("facet"), FACETS), 2, 1),
        ("surface", sit.get("surface") or bind.get("surface"), _pick(scene.get("surface"), SURFACES), 2, 3),
        ("screen_role", sit.get("screen_role"), _pick(scene.get("screen_role"), SCREEN_ROLES), 2, 1),
    )
    for _key, iv, sv, hit, miss in pairs:
        if iv and sv and iv == sv:
            score += hit
        elif iv and sv and iv != sv:
            score -= miss
    return score


def _app_ok(item: Dict[str, Any], app_id: str) -> bool:
    if not app_id:
        return True
    app_ids = item.get("app_ids") or []
    if not app_ids:
        return True
    return str(app_id) in [str(x) for x in app_ids]


def _eligible_items(app_id: str = "", app_version: str = "") -> List[Dict[str, Any]]:
    from server.services.knowledge_facts import item_is_live
    from server.services.system_settings_service import (
        knowledge_body_text,
        list_testing_knowledge,
    )

    out: List[Dict[str, Any]] = []
    for item in list_testing_knowledge():
        if not item_is_live(item, app_version):
            continue
        if not _app_ok(item, app_id):
            continue
        if not knowledge_body_text(item).strip() and not normalize_bind(item.get("bind")).get("value"):
            continue
        out.append(item)
    return out


_BACKFILL_DONE: set[str] = set()


def _scan_bind(app_id: str, want: str, env: str, surface: str, app_version: str = "") -> Optional[Dict[str, str]]:
    for item in _eligible_items(app_id, app_version):
        bind = normalize_bind(item.get("bind"))
        if bind.get("slot") != want or not bind.get("value"):
            continue
        if not env_matches(bind.get("env", ""), env):
            continue
        if not surface_matches(bind.get("surface") or "", surface):
            continue
        return {
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "slot": bind["slot"],
            "value": bind["value"],
            "env": bind.get("env") or "",
            "source": "knowledge_bind",
        }
    return None


def lookup_bind_value(
    *,
    app_id: str,
    slot: str,
    env: str = "",
    surface: str = "",
    backfill: bool = False,
    app_version: str = "",
) -> Optional[Dict[str, str]]:
    """按槽取绑定值。账号调用方自己先取；这里只读知识事实。"""
    want = _pick(slot, SLOTS)
    if not want:
        return None
    hit = _scan_bind(app_id, want, env, surface, app_version)
    if hit:
        return hit
    key = str(app_id or "*")
    if backfill and key not in _BACKFILL_DONE:
        _BACKFILL_DONE.add(key)
        for _ in range(3):
            n = backfill_situations(app_id, limit=BACKFILL_BATCH)
            hit = _scan_bind(app_id, want, env, surface, app_version)
            if hit or n == 0:
                break
    return hit


def route_knowledge(
    text: str = "",
    *,
    app_id: Optional[str] = None,
    scene: Optional[Dict[str, Any]] = None,
    limit: int = 3,
    categories: Optional[List[str]] = None,
    exclude_categories: Optional[List[str]] = None,
    app_version: str = "",
) -> List[Dict[str, Any]]:
    """情境重叠优先；无卡的旧条目仍走字面得分。"""
    from server.services.knowledge_facts import item_is_live
    from server.services.system_settings_service import (
        KNOWLEDGE_SKIP_REASON,
        KNOWLEDGE_USE_MIN_PCT,
        _knowledge_match_pct,
        _score_knowledge_item,
        dedupe_knowledge_hits,
        knowledge_body_text,
        list_testing_knowledge,
    )

    query = (text or "").strip()
    scene = scene if isinstance(scene, dict) else {}
    want = {str(c).strip() for c in (categories or []) if str(c).strip()}
    skip = {str(c).strip() for c in (exclude_categories or []) if str(c).strip()}
    scene_slot = _pick(scene.get("slot"), SLOTS)
    ranked: List[tuple[tuple, Dict[str, Any], int, int]] = []
    for item in list_testing_knowledge():
        if not item_is_live(item, app_version):
            continue
        cat = str(item.get("category") or "").strip()
        if want and cat not in want:
            continue
        if skip and cat in skip:
            continue
        if not _app_ok(item, str(app_id or "")):
            continue
        if not knowledge_body_text(item).strip() and not normalize_bind(item.get("bind")).get("value"):
            continue
        sit_score = situation_overlap(item, scene)
        text_score = _score_knowledge_item(item, query) if query else 0
        bind_hit = 1 if scene_slot and bind_slot_of(item) == scene_slot and normalize_bind(item.get("bind")).get("value") else 0
        ranked.append(((bind_hit, sit_score, text_score), item, sit_score, text_score))
    ranked.sort(key=lambda x: x[0], reverse=True)
    unique = dedupe_knowledge_hits([item for _, item, _, _ in ranked])
    meta = {
        str(item.get("id") or id(item)): (sit_score, text_score, key[0])
        for key, item, sit_score, text_score in ranked
    }
    out: List[Dict[str, Any]] = []
    for item in unique[: max(1, int(limit or 3))]:
        sit_score, text_score, bind_hit = meta.get(str(item.get("id") or id(item)), (0, 0, 0))
        pct = _knowledge_match_pct(text_score)
        used = bool(bind_hit) or sit_score >= SIT_MATCH_USED or pct >= KNOWLEDGE_USE_MIN_PCT
        row = dict(item)
        row["score"] = text_score
        row["sit_score"] = sit_score
        row["match_pct"] = pct if pct else (100 if bind_hit or sit_score >= SIT_MATCH_USED else 0)
        row["used"] = used
        row["skip_reason"] = "" if used else KNOWLEDGE_SKIP_REASON
        out.append(row)
    return out


def _extract_via_llm(title: str, content: str) -> Optional[Dict[str, Any]]:
    body = (content or "").strip()
    if not body:
        return None
    try:
        from server.services.ai.regression.llm_client import call_chat_text, resolve_regression_provider
        from server.services.ai import dispatch_log as dispatch
    except Exception as exc:
        SLog.w(TAG, f"extract import failed: {exc}")
        return None
    provider, gate = resolve_regression_provider()
    if provider is None:
        SLog.i(TAG, f"skip extract, no provider: {(gate or {}).get('reason')}")
        return None
    user = json.dumps({"title": (title or "")[:120], "content": body[:4000]}, ensure_ascii=False)
    tok = dispatch.bind(
        trigger="knowledge_situation",
        source="knowledge_situation",
        role="knowledge-reviewer",
        job="knowledge-situation",
        skill="knowledge-situation",
    )
    try:
        raw, meta = call_chat_text(
            provider=provider,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM},
                {"role": "user", "content": user},
            ],
            temperature=0.0,
            max_tokens=400,
            timeout_sec=25,
        )
    finally:
        dispatch.reset(tok)
    if raw is None:
        SLog.w(TAG, f"extract llm failed: {(meta or {}).get('error')}")
        return None
    return parse_extract_payload(raw)


def _extract_batch_via_llm(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    payload = []
    for row in rows:
        body = str(row.get("content") or "").strip()
        if not body:
            continue
        payload.append({
            "id": str(row.get("id") or ""),
            "title": str(row.get("title") or "")[:120],
            "content": body[:1200],
        })
    if not payload:
        return {}
    try:
        from server.services.ai.regression.llm_client import call_chat_text, resolve_regression_provider
        from server.services.ai import dispatch_log as dispatch
    except Exception as exc:
        SLog.w(TAG, f"batch extract import failed: {exc}")
        return {}
    provider, gate = resolve_regression_provider()
    if provider is None:
        return {}
    tok = dispatch.bind(
        trigger="knowledge_situation",
        source="knowledge_situation",
        role="knowledge-reviewer",
        job="knowledge-situation-batch",
        skill="knowledge-situation",
    )
    try:
        raw, meta = call_chat_text(
            provider=provider,
            messages=[
                {"role": "system", "content": EXTRACT_BATCH_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0.0,
            max_tokens=1200,
            timeout_sec=45,
        )
    finally:
        dispatch.reset(tok)
    if not isinstance(raw, dict):
        SLog.w(TAG, f"batch extract failed: {(meta or {}).get('error')}")
        return {}
    items = raw.get("items") if isinstance(raw.get("items"), list) else []
    out: Dict[str, Dict[str, Any]] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        kid = str(it.get("id") or "").strip()
        if kid:
            out[kid] = parse_extract_payload(it)
    return out


def enrich_item(
    item: Dict[str, Any],
    *,
    previous: Optional[Dict[str, Any]] = None,
    incoming: Optional[Dict[str, Any]] = None,
    skip_extract: bool = False,
) -> Dict[str, Any]:
    """保存时抽/对齐情境卡。空正文不抽。incoming 是请求原文，用来区分「调用方改了卡」和「合并进了旧字段」。"""
    row = dict(item)
    src = incoming if isinstance(incoming, dict) else row
    title = str(row.get("title") or "")
    content = str(row.get("content") or "").strip()
    fp = content_fingerprint(title, content)
    prev = previous if isinstance(previous, dict) else {}
    incoming_card = _card_from_item(src) if _incoming_has_card_key(src) else None
    prev_card = _card_from_item(prev)
    prev_fp = situation_fingerprint_of(prev)

    if skip_extract or not content:
        out = apply_card(row, incoming_card or prev_card)
        if content:
            out["situation_fp"] = fp
        elif prev_fp:
            out["situation_fp"] = prev_fp
        return out

    need_extract = (fp != prev_fp) or not prev_fp
    card: Dict[str, Any] = dict(prev_card)
    if need_extract:
        extracted = _extract_via_llm(title, content)
        if extracted:
            card = extracted
    if incoming_card is not None:
        if incoming_card.get("facet"):
            card["facet"] = incoming_card["facet"]
        if incoming_card.get("situation"):
            merged_sit = dict(card.get("situation") or {})
            merged_sit.update(incoming_card["situation"])
            card["situation"] = merged_sit
        if "bind" in src:
            inc_bind = incoming_card.get("bind") or {}
            if inc_bind.get("value") or inc_bind.get("slot") or not need_extract:
                card["bind"] = inc_bind
    out = apply_card(row, card)
    has_card = bool(out.get("facet") or out.get("situation") or out.get("bind"))
    if has_card or not need_extract:
        out["situation_fp"] = fp
    else:
        out.pop("situation_fp", None)
    return out


def backfill_situations(app_id: str, *, limit: int = BACKFILL_BATCH) -> int:
    """给还没有情境指纹的已通过条目补卡。查找绑定未命中时调用。"""
    from server.services.system_settings_service import knowledge_body_text, upsert_knowledge_item

    pending: List[Dict[str, Any]] = []
    for item in _eligible_items(app_id):
        body = knowledge_body_text(item).strip()
        if not body:
            continue
        fp = content_fingerprint(str(item.get("title") or ""), str(item.get("content") or ""))
        if situation_fingerprint_of(item) == fp:
            continue
        pending.append(item)
        if len(pending) >= max(1, int(limit or BACKFILL_BATCH)):
            break
    if not pending:
        return 0
    extracted = _extract_batch_via_llm(pending)
    n = 0
    for item in pending:
        kid = str(item.get("id") or "")
        card = extracted.get(kid) or {}
        fp = content_fingerprint(str(item.get("title") or ""), str(item.get("content") or ""))
        row = apply_card(dict(item), card)
        row["situation_fp"] = fp
        try:
            upsert_knowledge_item(row, skip_extract=True)
            n += 1
        except Exception as exc:
            SLog.w(TAG, f"backfill upsert failed {kid}: {exc}")
    return n
