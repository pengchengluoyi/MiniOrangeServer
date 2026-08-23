# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""IM 总指挥：选出角色后真正调用该角色 prompt。"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List

SKIP_ROLES = {"im-qa-assistant", "im-defect-assistant", "conductor"}
ALIASES = {
    "req-analyst": "req-analyst",
    "req-analyst": "req-analyst",
    "需求分析师": "req-analyst",
    "需求分析": "req-analyst",
    "mindmap-writer": "mindmap-writer",
    "测试脑图编写": "mindmap-writer",
    "脑图": "mindmap-writer",
    "case-writer": "case-writer",
    "测试用例编写": "case-writer",
    "用例": "case-writer",
    "req-qa-bm": "req-qa-bm",
    "version-qa-bm": "version-qa-bm",
    "report-writer": "report-writer",
    "product-expert": "product-expert",
}


def _prompt_of(role: Dict[str, Any]) -> str:
    return str(role.get("system_prompt") or role.get("system_prompt") or "").strip()


def _callable_roles() -> List[Dict[str, str]]:
    from server.services.ai.roles_catalog import list_roles

    rows = []
    for row in list_roles().get("product") or []:
        rid = str(row.get("id") or "")
        if rid in SKIP_ROLES or not _prompt_of(row):
            continue
        rows.append(
            {
                "id": rid,
                "label": str(row.get("label") or rid),
                "summary": str(row.get("summary") or "")[:80],
            }
        )
    return rows


def _resolve_role_id(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    mapped = ALIASES.get(text) or ALIASES.get(text.lower())
    if mapped:
        return mapped
    from server.services.ai.roles_catalog import get_role

    if get_role(text):
        return text
    for row in _callable_roles():
        if row["id"] == text or row["label"] == text:
            return row["id"]
    return ""


def _workspace_brief(user_text: str) -> str:
    try:
        from sqlalchemy.orm import joinedload

        from server.core.database import SessionLocal
        from server.models.project import App
        from server.services import app_automation_service as aas
    except Exception:
        return ""
    needle = str(user_text or "")
    db = SessionLocal()
    try:
        apps = db.query(App).options(joinedload(App.project)).order_by(App.name).all()
        chunks: List[str] = []
        for app in apps:
            name = str(app.name or "").strip()
            proc = aas.get_automation_config(app).get("qa_process") or {}
            reqs = [r for r in (proc.get("requirements") or []) if isinstance(r, dict)]
            rels = [r for r in (proc.get("releases") or []) if isinstance(r, dict)]
            tokens = [name]
            tokens += [str(r.get("version") or r.get("name") or "") for r in rels]
            tokens += [str(r.get("title") or r.get("name") or "") for r in reqs[:30]]
            hit = (not needle) or any(t and t in needle for t in tokens if t)
            if needle and name and name not in needle and not hit:
                continue
            lines = [f"应用 {name}"]
            for rel in rels[:12]:
                ver = str(rel.get("version") or rel.get("name") or rel.get("id") or "").strip()
                if ver:
                    lines.append(f"  版本 {ver}")
            for req in reqs[:40]:
                rid = str(req.get("external_id") or req.get("id") or "").strip()
                title = str(req.get("title") or req.get("name") or "").strip()
                module = str(req.get("module") or "").strip()
                version = str(req.get("version") or req.get("release") or "").strip()
                summary = str(
                    req.get("summary") or req.get("source") or req.get("text") or ""
                ).replace("\n", " ").strip()[:180]
                bit = "  需求"
                if rid:
                    bit += f" {rid}"
                if title:
                    bit += f" {title}"
                if module:
                    bit += f" · {module}"
                if version:
                    bit += f" · {version}"
                if summary:
                    bit += f"：{summary}"
                lines.append(bit)
            if len(lines) > 1:
                chunks.append("\n".join(lines))
            if len(chunks) >= 4:
                break
        return "\n\n".join(chunks)[:6000]
    except Exception:
        return ""
    finally:
        db.close()


def _parse_plan(raw: str) -> Dict[str, Any]:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    data: Dict[str, Any] = {}
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            data = parsed
    except Exception:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:
                data = {}
    action = str(data.get("action") or "").strip().lower()
    calls = []
    for item in data.get("calls") or []:
        if not isinstance(item, dict):
            continue
        rid = _resolve_role_id(str(item.get("role_id") or item.get("role") or ""))
        task = str(item.get("task") or item.get("instruction") or "").strip()
        if rid and rid not in SKIP_ROLES:
            calls.append({"role_id": rid, "task": task})
    if action not in ("reply", "dispatch"):
        action = "dispatch" if calls else "reply"
    if not calls:
        for m in re.finditer(r"@([^\s@：:]{2,16})", text):
            rid = _resolve_role_id(m.group(1))
            if rid:
                calls.append({"role_id": rid, "task": text})
                action = "dispatch"
    return {
        "action": action,
        "reply": str(data.get("reply") or "").strip(),
        "calls": calls[:2],
        "raw": text,
    }


def _guess_calls(user_text: str) -> List[Dict[str, str]]:
    t = str(user_text or "")
    if any(k in t for k in ("脑图", "思维导图")):
        return [{"role_id": "mindmap-writer", "task": t}]
    if any(k in t for k in ("写用例", "用例草稿", "补用例")):
        return [{"role_id": "case-writer", "task": t}]
    if any(k in t for k in ("需求", "验收", "测试点", "版本")):
        return [{"role_id": "req-analyst", "task": t}]
    return []


def _jsonish_to_text(obj: Any, *, depth: int = 0) -> str:
    if depth > 4 or obj is None:
        return ""
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, (int, float, bool)):
        return str(obj)
    if isinstance(obj, list):
        parts = [_jsonish_to_text(x, depth=depth + 1) for x in obj[:40]]
        return "\n".join(f"- {p}" for p in parts if p)
    if isinstance(obj, dict):
        prefer = (
            "title",
            "name",
            "summary",
            "reply",
            "acceptance",
            "test_points",
            "requirements",
            "items",
            "points",
            "children",
        )
        bits = []
        for key in prefer:
            if key in obj:
                val = _jsonish_to_text(obj.get(key), depth=depth + 1)
                if val:
                    bits.append(val)
        if bits:
            return "\n".join(bits)
        out = []
        for key, val in list(obj.items())[:12]:
            text = _jsonish_to_text(val, depth=depth + 1)
            if text:
                out.append(f"{key}：{text}")
        return "\n".join(out)
    return str(obj).strip()


def _specialist_text(reply: str) -> str:
    text = str(reply or "").strip()
    if not text:
        return ""
    blob = text
    if blob.startswith("```"):
        blob = re.sub(r"^```(?:json)?\s*", "", blob)
        blob = re.sub(r"\s*```$", "", blob)
    try:
        data = json.loads(blob)
    except Exception:
        return text
    pretty = _jsonish_to_text(data)
    return pretty.strip() or text


def _catalog_block() -> str:
    lines = [f"- {row['id']}（{row['label']}）：{row['summary']}" for row in _callable_roles()]
    return "\n".join(lines) or "- req-analyst（需求分析师）"


def commander_system(base_prompt: str) -> str:
    return (
        f"{str(base_prompt or '').strip()}\n\n"
        "【本轮可调用角色】\n"
        f"{_catalog_block()}\n"
        "必须按协议输出 JSON。dispatch 时系统会真的调用该角色 prompt。"
    )


def _run_specialist(role_id: str, task: str, user_text: str, facts: str) -> tuple[str, str]:
    from server.services.ai.roles_catalog import chat_with_role, get_role

    role = get_role(role_id) or {}
    label = str(role.get("label") or role_id)
    payload = (
        "用户在 IM 指挥通道提问。用简洁中文把结果交给总指挥和用户，不要只丢 JSON。\n"
        f"【任务】{task or user_text}\n"
        f"【用户原话】{user_text}\n"
    )
    if facts:
        payload += f"【工作区材料，只许用这里的事实】\n{facts}\n材料没有的字段就写「工作区没有」，禁止编造。\n"
    else:
        payload += "工作区没有匹配到应用/需求材料。不要编造列表，说明缺什么。\n"
    out = chat_with_role(
        role_id=role_id,
        messages=[{"role": "user", "content": payload[:8000]}],
        explain_mode=False,
    )
    return label, _specialist_text(str(out.get("reply") or ""))


def run_commander_turn(
    *,
    base_prompt: str,
    history: List[Dict[str, str]],
    user_text: str,
    call_llm: Callable[..., str],
) -> str:
    facts = _workspace_brief(user_text)
    system = commander_system(base_prompt)
    user_payload = user_text if not facts else f"{user_text}\n\n【工作区材料】\n{facts}"
    raw = call_llm(system, history, user_payload, conversational=False)
    plan = _parse_plan(raw)
    calls = list(plan["calls"])
    if plan["action"] != "dispatch" or not calls:
        guessed = _guess_calls(user_text)
        if guessed:
            calls = guessed
        elif plan["action"] == "reply" or not calls:
            text = plan["reply"] or plan["raw"]
            if text.startswith("{") and "action" in text:
                return "再说一下应用名和版本，我再调对应角色。"
            return text or "再说一下应用和版本。"
    parts: List[str] = []
    lead = plan["reply"]
    if lead and not lead.startswith("{") and "role_id" not in lead:
        parts.append(lead)
    for call in calls:
        try:
            label, body = _run_specialist(call["role_id"], call.get("task") or "", user_text, facts)
        except Exception as e:
            parts.append(f"@{call['role_id']} 没接上：{e}")
            continue
        if body:
            parts.append(f"@{label}\n{body}")
        else:
            parts.append(f"@{label} 没有返回内容。")
    return "\n\n".join(p for p in parts if p).strip() or "角色没有给出结果。"
