# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""调度日志：每次大模型调用和流水线 Job 落盘，给设置页「调度」查看。"""
from __future__ import annotations

import base64
import contextvars
import json
import re
import threading
from datetime import datetime
import uuid
from pathlib import Path
from typing import Any, Optional

from server.core.database import APP_DATA_DIR

_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("dispatch_ctx", default={})
_LOCK = threading.Lock()
_MAX_ROWS = 2000
_FIELD_CAP = 8000
_DATA_URL_RE = re.compile(
    r"data:image/([A-Za-z0-9.+-]+);base64,([A-Za-z0-9+/=\s]{64,})",
    re.I,
)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _path() -> Path:
    folder = Path(APP_DATA_DIR) / "data" / "dispatch"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / "calls.jsonl"


def bind(**kwargs) -> contextvars.Token:
    cur = dict(_CTX.get() or {})
    for key, val in kwargs.items():
        if val is not None and val != "":
            cur[key] = val
    return _CTX.set(cur)


def reset(token: contextvars.Token) -> None:
    try:
        _CTX.reset(token)
    except Exception:
        pass


def ctx() -> dict:
    return dict(_CTX.get() or {})


_JOB_TO_SKILL = {
    "im_dialogue": "im.dialogue",
    "im_defect": "im.defect",
    "im.dialogue": "im.dialogue",
    "im.defect": "im.defect",
    "review_impact": "propose_atlas",
    "edit_atlas": "propose_atlas",
    "agent-restart": "agent-decide",
    "role_chat": "",
    "qa_tick": "",
    "route": "",
    "atlas_followup": "",
    "knowledge-capture": "",
    "knowledge-review": "",
}

_TRIGGER_SOURCE = {
    "qa_tick": "continue_analysis",
    "im_chat": "im_inbound",
    "settings_chat": "settings_role_chat",
    "case_run": "case_run",
    "atlas_confirm": "atlas_confirm",
    "atlas_edit": "atlas_edit",
    "atlas_reject": "atlas_reject",
    "knowledge_capture": "knowledge_capture",
    "knowledge_review": "knowledge_review",
    "conductor_route": "analyst_route",
}


def skill_from_job(job: str = "", skill: str = "") -> str:
    sid = str(skill or "").strip()
    if sid:
        return sid
    jid = str(job or "").strip()
    if jid in _JOB_TO_SKILL:
        return _JOB_TO_SKILL[jid]
    return jid


def source_from_trigger(trigger: str = "", source: str = "") -> str:
    sid = str(source or "").strip()
    if sid:
        return sid
    return _TRIGGER_SOURCE.get(str(trigger or "").strip(), str(trigger or "").strip())


def infer_call_meta(row: dict | None = None, *, output: Any = None, system_prompt: str = "") -> dict:
    """从模型输出反推触发 / Job / 角色。执行线程没 bind 时用来补齐。"""
    blob = output if output is not None else (row or {}).get("output")
    parsed = blob if isinstance(blob, dict) else None
    if parsed is None:
        try:
            parsed = json.loads(str(blob or "").strip())
        except Exception:
            parsed = None
    parsed = parsed if isinstance(parsed, dict) else {}
    sys_l = str(system_prompt or (row or {}).get("system_prompt") or "")

    if parsed.get("goal") and (parsed.get("checkpoints") is not None or parsed.get("checkpoint") is not None):
        return {"trigger": "case_run", "job": "goal-extract", "role": "test-engineer", "skill": "goal-extract", "source": "case_run"}
    if "restart" in parsed and parsed.get("thought"):
        return {"trigger": "case_run", "job": "agent-restart", "role": "test-engineer", "skill": "agent-decide", "source": "case_run"}
    if parsed.get("thought") and any(k in parsed for k in ("action", "tool", "capability_id", "done", "x", "y")):
        return {"trigger": "case_run", "job": "agent-decide", "role": "test-engineer", "skill": "agent-decide", "source": "case_run"}
    if parsed.get("thought"):
        return {"trigger": "case_run", "job": "agent-decide", "role": "test-engineer", "skill": "agent-decide", "source": "case_run"}
    if "passed" in parsed and ("confidence" in parsed or parsed.get("ai_reasoning")):
        return {"trigger": "case_run", "job": "assert-vision", "role": "test-engineer", "skill": "assert-vision", "source": "case_run"}
    if isinstance(parsed.get("events"), list) or parsed.get("mode") in ("plan", "decline", "replan", "give_up"):
        return {"trigger": "case_run", "job": "plan-overview", "role": "test-engineer", "skill": "plan-overview", "source": "case_run"}
    if parsed.get("bbox") or ("x" in parsed and "y" in parsed):
        return {"trigger": "case_run", "job": "locate-vision", "role": "test-engineer", "skill": "locate-vision", "source": "case_run"}
    if isinstance(parsed.get("items"), list):
        return {"trigger": "knowledge_capture", "job": "knowledge-capture", "role": "version-qa-bm", "source": "knowledge_capture"}
    if "抽取目标" in sys_l or "goal-extract" in sys_l:
        return {"trigger": "case_run", "job": "goal-extract", "role": "test-engineer", "skill": "goal-extract", "source": "case_run"}
    if "是否先重开" in sys_l or "agent-restart" in sys_l:
        return {"trigger": "case_run", "job": "agent-restart", "role": "test-engineer", "skill": "agent-decide", "source": "case_run"}
    if "下一个动作" in sys_l or "agent-decide" in sys_l:
        return {"trigger": "case_run", "job": "agent-decide", "role": "test-engineer", "skill": "agent-decide", "source": "case_run"}
    if "检查点" in sys_l or "assert-vision" in sys_l:
        return {"trigger": "case_run", "job": "assert-vision", "role": "test-engineer", "skill": "assert-vision", "source": "case_run"}
    if parsed.get("action") in ("submit", "clarify", "reject") and (
        "title" in parsed or "steps" in parsed or "reply" in parsed
    ):
        return {"trigger": "im_chat", "job": "im_defect", "role": "im-defect-assistant", "skill": "im.defect", "source": "im_inbound"}
    if any(
        mark in sys_l
        for mark in (
            "在飞书 / 企业微信 / 钉钉 / Slack",
            "不在这套对话里直接建禅道单",
            "MiniOrange 的测试助手",
            "MiniOrange 的总指挥",
            "你排兵，其他角色干活",
            "请他们说「提缺陷」",
        )
    ):
        return {"trigger": "im_chat", "job": "im_dialogue", "role": "im-qa-assistant", "skill": "im.dialogue", "source": "im_inbound"}
    if any(
        mark in sys_l
        for mark in (
            "整理一张可提交到禅道的缺陷",
            "只输出 JSON，不要输出其它文字",
            '"action": "submit" | "clarify" | "reject"',
        )
    ):
        return {"trigger": "im_chat", "job": "im_defect", "role": "im-defect-assistant", "skill": "im.defect", "source": "im_inbound"}
    return {}


def decorate_row(row: dict) -> dict:
    out = dict(row or {})
    guessed = infer_call_meta(out)
    if not out.get("trigger") or out.get("trigger") == "unknown":
        out["trigger"] = guessed.get("trigger") or out.get("trigger") or "unknown"
    if not out.get("job"):
        out["job"] = guessed.get("job") or ""
    if not out.get("role"):
        out["role"] = guessed.get("role") or ""
    if not out.get("skill"):
        out["skill"] = guessed.get("skill") or skill_from_job(out.get("job") or "")
    if not out.get("source"):
        out["source"] = guessed.get("source") or source_from_trigger(out.get("trigger") or "")
    if not out.get("routed_by") and out.get("trigger") == "qa_tick":
        out["routed_by"] = "conductor"
    return out


def hydrate_row_images(row: dict) -> dict:
    out = dict(row or {})
    if out.get("images"):
        return out
    stem = str(out.get("id") or "ds")
    images: list[str] = []
    for field, suffix in (("system_prompt", "s"), ("input", "u"), ("output", "o"), ("detail", "d")):
        _, found = extract_media(out.get(field) or "", stem=f"{stem}-h{suffix}")
        for path in found:
            if path not in images:
                images.append(path)
    if images:
        out["images"] = images
    return out


def _clip(value: Any, cap: int = _FIELD_CAP) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    if len(text) <= cap:
        return text
    return text[:cap] + "…"


def _media_dir() -> Path:
    folder = Path(APP_DATA_DIR) / "uploads" / "dispatch"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _ext_for_mime(mime: str) -> str:
    name = str(mime or "png").lower().split("/")[-1].replace("jpeg", "jpg")
    return name if name in {"png", "jpg", "webp", "gif"} else "png"


def _decode_image(b64: str) -> bytes | None:
    try:
        raw = base64.b64decode("".join(str(b64 or "").split()), validate=False)
    except Exception:
        return None
    if len(raw) < 32:
        return None
    if raw.startswith(b"\x89PNG"):
        return raw if b"IEND" in raw else None
    if raw.startswith(b"\xff\xd8"):
        return raw if raw.endswith(b"\xff\xd9") or len(raw) > 4096 else None
    if raw.startswith(b"GIF") or raw.startswith(b"RIFF"):
        return raw
    return None


def _persist_bytes(raw: bytes, mime: str, stem: str) -> str:
    dest = _media_dir() / f"{stem}.{_ext_for_mime(mime)}"
    if not dest.exists():
        dest.write_bytes(raw)
    return f"/static/dispatch/{dest.name}"


def _as_inline_image(text: str) -> tuple[str, str] | None:
    s = str(text or "").strip()
    if not s or s.endswith("…") or s.endswith("..."):
        return None
    if s.startswith("data:image/"):
        hit = _DATA_URL_RE.match(s)
        return (hit.group(1), hit.group(2)) if hit else None
    if s.startswith("iVBORw0KGgo") and len(s) > 80:
        return "png", s
    if s.startswith("/9j/") and len(s) > 80:
        return "jpeg", s
    return None


def extract_media(value: Any, *, stem: str) -> tuple[Any, list[str]]:
    """把 base64 / data URL 存成 /static/dispatch 文件，正文里改成短路径。"""
    images: list[str] = []
    n = 0

    def persist(mime: str, b64: str) -> str | None:
        nonlocal n
        raw = _decode_image(b64)
        if raw is None:
            return None
        n += 1
        path = _persist_bytes(raw, mime, f"{stem}-{n}")
        if path not in images:
            images.append(path)
        return path

    def replace_text(text: str) -> str:
        def repl(match: re.Match) -> str:
            return persist(match.group(1), match.group(2)) or "[图]"

        return _DATA_URL_RE.sub(repl, text)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: walk(val) for key, val in node.items()}
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, str):
            return node
        found = _as_inline_image(node)
        if found:
            return persist(*found) or "[图]"
        if "data:image/" in node:
            return replace_text(node)
        return node

    return walk(value), images


def _clip_media(value: Any, *, stem: str) -> tuple[str, list[str]]:
    cleaned, images = extract_media(value, stem=stem)
    return _clip(cleaned), images


def _write(row: dict) -> dict:
    row = dict(row)
    row.setdefault("id", f"ds-{uuid.uuid4().hex[:12]}")
    row.setdefault("at", _now())
    line = json.dumps(row, ensure_ascii=False)
    with _LOCK:
        path = _path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        try:
            raw = path.read_text(encoding="utf-8").splitlines()
            if len(raw) > _MAX_ROWS:
                path.write_text("\n".join(raw[-_MAX_ROWS:]) + "\n", encoding="utf-8")
        except Exception:
            pass
    return row


def record_llm(*, messages: list | None = None, parsed=None, raw_text: str = "", meta: dict | None = None) -> dict:
    meta = meta if isinstance(meta, dict) else {}
    env = ctx()
    system = ""
    user = ""
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content") or ""
        if role == "system" and not system:
            system = content
        elif role == "user":
            user = content
    output = parsed if parsed is not None else raw_text or meta.get("content_preview") or ""
    error = meta.get("error") or ""
    status = "error" if error and parsed is None and not raw_text else "done"
    if env.get("engine") == "none":
        status = "skipped"
    usage = meta.get("usage") if isinstance(meta.get("usage"), dict) else {}
    guessed = infer_call_meta(output=output, system_prompt=system)
    trigger = env.get("trigger") or guessed.get("trigger") or "unknown"
    job = env.get("job") or guessed.get("job") or ""
    role = env.get("role") or guessed.get("role") or ""
    skill = env.get("skill") or guessed.get("skill") or skill_from_job(job)
    source = env.get("source") or guessed.get("source") or source_from_trigger(trigger)
    routed_by = env.get("routed_by") or ("conductor" if trigger == "qa_tick" else "")
    row_id = f"ds-{uuid.uuid4().hex[:12]}"
    system_text, imgs_s = _clip_media(system, stem=f"{row_id}-s")
    input_text, imgs_u = _clip_media(user, stem=f"{row_id}-u")
    output_text, imgs_o = _clip_media(output, stem=f"{row_id}-o")
    return _write(
        {
            "id": row_id,
            "kind": "llm",
            "status": status,
            "trigger": trigger,
            "source": source,
            "app_id": env.get("app_id") or "",
            "app_name": env.get("app_name") or "",
            "pipeline_id": env.get("pipeline_id") or "",
            "step_index": env.get("step_index"),
            "step_total": env.get("step_total"),
            "role": role,
            "job": job,
            "skill": skill,
            "routed_by": routed_by,
            "model": meta.get("model") or env.get("model") or "",
            "provider_id": meta.get("provider_id") or "",
            "system_prompt": system_text,
            "input": input_text,
            "output": output_text,
            "images": imgs_s + imgs_u + imgs_o,
            "engine": "llm" if not error or parsed is not None else (env.get("engine") or "llm"),
            "elapsed_ms": int(meta.get("elapsed_ms") or 0),
            "prompt_tokens": int(meta.get("prompt_tokens") or usage.get("prompt_tokens") or 0),
            "completion_tokens": int(meta.get("completion_tokens") or usage.get("completion_tokens") or 0),
            "total_tokens": int(meta.get("total_tokens") or usage.get("total_tokens") or 0),
            "error": str(error or ""),
        }
    )


def record_job(
    *,
    status: str,
    job: str,
    role: str = "",
    skill: str = "",
    source: str = "",
    routed_by: str = "",
    routed: list | None = None,
    detail: str = "",
    input_data: Any = None,
    output_data: Any = None,
    error: str = "",
) -> dict:
    env = ctx()
    trigger = env.get("trigger") or "unknown"
    job_id = job or env.get("job") or ""
    role_id = role or env.get("role") or ""
    skill_id = skill or env.get("skill") or skill_from_job(job_id)
    source_id = source or env.get("source") or source_from_trigger(trigger)
    router = routed_by or env.get("routed_by") or ("conductor" if trigger == "qa_tick" else "")
    row_id = f"ds-{uuid.uuid4().hex[:12]}"
    input_text, imgs_u = _clip_media(input_data or env.get("input") or "", stem=f"{row_id}-u")
    output_text, imgs_o = _clip_media(output_data or detail, stem=f"{row_id}-o")
    row = {
        "id": row_id,
        "kind": "job",
        "status": status,
        "trigger": trigger,
        "source": source_id,
        "app_id": env.get("app_id") or "",
        "app_name": env.get("app_name") or "",
        "pipeline_id": env.get("pipeline_id") or "",
        "step_index": env.get("step_index"),
        "step_total": env.get("step_total"),
        "role": role_id,
        "job": job_id,
        "skill": skill_id,
        "routed_by": router,
        "system_prompt": "",
        "input": input_text,
        "output": output_text,
        "images": imgs_u + imgs_o,
        "engine": env.get("engine") or "pipeline",
        "elapsed_ms": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "error": str(error or ""),
        "detail": detail,
    }
    if routed:
        row["routed"] = routed
    return _write(row)


def new_pipeline_id() -> str:
    return f"pl-{uuid.uuid4().hex[:10]}"


def list_calls(
    *,
    limit: int = 80,
    kind: str = "",
    role: str = "",
    trigger: str = "",
    app_id: str = "",
    pipeline_id: str = "",
) -> list[dict]:
    path = _path()
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    rows = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        row = decorate_row(row)
        if kind and row.get("kind") != kind:
            continue
        if role and row.get("role") != role:
            continue
        if trigger and row.get("trigger") != trigger:
            continue
        if app_id and row.get("app_id") != app_id:
            continue
        if pipeline_id and row.get("pipeline_id") != pipeline_id:
            continue
        rows.append(row)
        if len(rows) >= max(1, min(int(limit or 80), 300)):
            break
    return rows


def get_call(call_id: str) -> Optional[dict]:
    cid = str(call_id or "").strip()
    if not cid:
        return None
    for row in list_calls(limit=300):
        if row.get("id") == cid:
            return hydrate_row_images(decorate_row(row))
    path = _path()
    if not path.exists():
        return None
    try:
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict) and row.get("id") == cid:
                return hydrate_row_images(decorate_row(row))
    except Exception:
        return None
    return None
