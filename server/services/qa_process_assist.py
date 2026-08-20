# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""流程建议：规则版 Job。只产出草稿，不改 gate、不写飞书。"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional

ASSIST_JOBS = ("map_cases", "classify_fail", "draft_sign", "draft_gate", "pick_regression")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _new_id() -> str:
    return f"ai-{int(time.time() * 1000):x}"[-12:]


def _djb2(text: str) -> str:
    h = 5381
    for ch in str(text or ""):
        h = ((h << 5) + h + ord(ch)) & 0xFFFFFFFF
    return format(h, "x")


def _case_req_id(row: dict) -> str:
    return str(row.get("requirement_id") or row.get("req_id") or row.get("story_id") or "").strip()


def _blob(row: dict) -> str:
    return f"{row.get('case_id') or ''} {row.get('name') or row.get('title') or ''} {row.get('module') or ''} {_case_req_id(row)}".lower()


def _suggest(text: str) -> str:
    s = str(text or "").strip()
    if not s:
        return ""
    return s if s.startswith("建议") else f"建议：{s}"


def artifact(*, job: str, payload: dict, citations: list, suggest: str, input_hash: str = "") -> dict:
    return {
        "id": _new_id(),
        "job": job,
        "status": "draft",
        "engine": "rule",
        "at": _now(),
        "input_hash": input_hash,
        "citations": [x for x in citations if x],
        "suggest": _suggest(suggest),
        "payload": payload or {},
    }


def cases_for_requirement(cases: list, req: dict) -> List[str]:
    ext = str(req.get("external_id") or "").strip().lower()
    rid = str(req.get("id") or "").strip().lower()
    if not ext and not rid:
        return []
    out = []
    for c in cases or []:
        cid = str(c.get("case_id") or "").strip()
        got = _case_req_id(c).lower()
        if cid and got and got in {ext, rid}:
            out.append(cid)
    return out


def _score_point(point: dict, row: dict) -> int:
    blob = _blob(row)
    tokens = [t.strip().lower() for t in re.split(r"[\s,，、/]+", str(point.get("text") or "")) if len(t.strip()) >= 2]
    return sum(3 for t in tokens[:12] if t in blob)


def map_cases(req: dict, cases: list) -> dict:
    exact = set(cases_for_requirement(cases, req))
    weak = set()
    tokens = []
    for p in (req.get("understanding") or {}).get("points") or []:
        tokens.extend(t.strip().lower() for t in re.split(r"[\s,，、/]+", str(p.get("text") or "")) if len(t.strip()) >= 2)
    ext = str(req.get("external_id") or req.get("id") or "").strip().lower()
    for c in cases or []:
        cid = str(c.get("case_id") or "")
        if not cid or cid in exact:
            continue
        blob = _blob(c)
        score = 8 if ext and ext in blob else 0
        score += sum(1 for t in tokens[:24] if t in blob)
        if score > 0:
            weak.add(cid)
    pool = [c for c in (cases or []) if str(c.get("case_id") or "") in exact or str(c.get("case_id") or "") in weak]
    mappings = []
    gaps = []
    for p in (req.get("understanding") or {}).get("points") or []:
        if p.get("waived"):
            continue
        hung = set(p.get("case_ids") or [])
        scored = []
        for c in pool:
            cid = str(c.get("case_id") or "")
            if cid in hung:
                continue
            score = 24 if cid in exact else 8
            score += _score_point(p, c)
            if score >= 24:
                scored.append({"case_id": cid, "title": c.get("name") or c.get("title") or cid, "score": score})
        scored.sort(key=lambda x: -x["score"])
        top = scored[:3]
        if top:
            mappings.append({"point_id": p.get("id"), "point_text": p.get("text"), "hung": list(hung), "suggest": top})
        elif not hung:
            gaps.append({
                "point_id": p.get("id"),
                "point_text": p.get("text"),
                "reason": "飞书「需求ID」列没有本需求编号" if not exact else "池里没有能对上的步骤，去飞书表补",
            })
    if gaps:
        suggest = f"{len(gaps)} 个测试点还缺用例，去飞书表补或手选"
    elif mappings:
        suggest = f"{len(mappings)} 个测试点可挂用例，采纳后才算覆盖"
    else:
        suggest = "测试点已挂满，或还没有测试点"
    return artifact(
        job="map_cases",
        suggest=suggest,
        citations=[s["case_id"] for m in mappings for s in m["suggest"]],
        payload={"mappings": mappings, "gaps": gaps, "pool_count": len(exact)},
    )


def _task_of(runs: list, kind: str, tasks: list) -> Optional[dict]:
    row = next((r for r in reversed(runs or []) if r.get("kind") == kind), None)
    if not row:
        return None
    tid = row.get("task_id")
    return next((t for t in (tasks or []) if t.get("taskId") == tid or t.get("task_id") == tid), None)


def classify_fail(task: Optional[dict]) -> dict:
    items = []
    for c in (task or {}).get("cases") or []:
        st = str(c.get("status") or "").lower()
        if st not in {"fail", "failed", "blocked", "error"}:
            continue
        blob = f"{c.get('error') or ''} {c.get('message') or ''} {c.get('name') or c.get('title') or ''}".lower()
        kind = "产品"
        if re.search(r"timeout|超时|断开|offline|设备|安装失败|无网", blob):
            kind = "环境"
        elif re.search(r"找不到|locator|控件|走神|未看到|没点到|元素", blob):
            kind = "走神"
        elif re.search(r"过期|文案变|已下线|改版", blob):
            kind = "用例过期"
        items.append({
            "case_id": c.get("case_id") or c.get("caseId"),
            "title": c.get("name") or c.get("title") or "",
            "kind": kind,
            "status": c.get("status"),
        })
    wander = [i["case_id"] for i in items if i["kind"] == "走神" and i["case_id"]]
    if not items:
        suggest = "没有失败条"
    elif wander:
        suggest = f"{len(items)} 条失败已分类，{len(wander)} 条像走神可重跑"
    else:
        suggest = f"{len(items)} 条失败已分类"
    return artifact(
        job="classify_fail",
        suggest=suggest,
        citations=[i["case_id"] for i in items],
        payload={"items": items, "rerun_ids": wander},
    )


def _req_signed(req: dict) -> bool:
    return req.get("gate") == "hand" or bool(req.get("signoff"))


def _linked_ids(req: dict) -> List[str]:
    ids = []
    for p in (req.get("understanding") or {}).get("points") or []:
        ids.extend(p.get("case_ids") or [])
    ids.extend(req.get("case_ids") or [])
    return [x for x in dict.fromkeys(ids) if x]


def draft_sign(req: dict, tasks: list) -> dict:
    task = _task_of(req.get("runs") or [], "req_test", tasks)
    failed = int((task or {}).get("failed") or 0)
    blocked = int((task or {}).get("blocked") or 0)
    if not task:
        suggest = "尚未跑功能测试"
    elif failed or blocked:
        suggest = "带风险"
    else:
        suggest = "可以验收"
    fail = classify_fail(task)
    return artifact(
        job="draft_sign",
        suggest=suggest,
        citations=[(task or {}).get("taskId") or (task or {}).get("task_id"), *fail["citations"]],
        payload={
            "report": {
                "failed": failed,
                "blocked": blocked,
                "passed": int((task or {}).get("passed") or 0),
                "suggest": suggest,
                "latest_task_id": (task or {}).get("taskId") or (task or {}).get("task_id") or "",
            },
            "fails": fail["payload"],
        },
    )


def draft_gate(rel: dict, requirements: list, tasks: list) -> dict:
    reqs = [next((r for r in requirements if r.get("id") == i), None) for i in (rel.get("requirement_ids") or [])]
    reqs = [r for r in reqs if r]
    unsigned = [r.get("title") for r in reqs if not _req_signed(r)]
    task = _task_of(rel.get("runs") or [], "release_regression", tasks)
    failed = int((task or {}).get("failed") or 0)
    blocked = int((task or {}).get("blocked") or 0)
    if not task:
        suggest = "尚未跑预发回归"
    elif failed or blocked or unsigned:
        suggest = "带风险"
    else:
        suggest = "通过"
    fail = classify_fail(task)
    return artifact(
        job="draft_gate",
        suggest=suggest,
        citations=[(task or {}).get("taskId") or (task or {}).get("task_id"), *fail["citations"]],
        payload={
            "report": {
                "unsigned": unsigned,
                "failed": failed,
                "blocked": blocked,
                "case_count": len(rel.get("case_ids") or []),
                "suggest": suggest,
                "latest_task_id": (task or {}).get("taskId") or (task or {}).get("task_id") or "",
            },
            "fails": fail["payload"],
        },
    )


def pick_regression(rel: dict, requirements: list, suites: list) -> dict:
    reqs = [next((r for r in requirements if r.get("id") == i), None) for i in (rel.get("requirement_ids") or [])]
    reqs = [r for r in reqs if r]
    signed = [r for r in reqs if _req_signed(r)]
    unsigned = [r for r in reqs if not _req_signed(r)]
    smoke = next((s for s in (suites or []) if re.search(r"冒烟|smoke", str(s.get("name") or ""), re.I)), None)
    smoke_ids = list((smoke or {}).get("case_ids") or [])
    pass_ids = list(dict.fromkeys([*sum((_linked_ids(r) for r in signed), []), *smoke_ids]))
    risk_ids = list(dict.fromkeys(sum((_linked_ids(r) for r in unsigned), [])))
    if unsigned:
        suggest = f"建议回归 {len(pass_ids)} 条；还有 {len(unsigned)} 条需求未验收，它们的 {len(risk_ids)} 条用例不要自动带上"
    else:
        suggest = f"建议回归 {len(pass_ids)} 条，可圈进预发回归"
    return artifact(
        job="pick_regression",
        suggest=suggest,
        citations=pass_ids,
        payload={
            "pass_ids": pass_ids,
            "risk_ids": risk_ids,
            "unsigned": [r.get("title") for r in unsigned],
            "smoke_name": (smoke or {}).get("name") or "",
        },
    )


def run_job(job: str, *, requirement=None, release=None, requirements=None, cases=None, tasks=None, suites=None) -> dict:
    if job not in ASSIST_JOBS:
        return artifact(job=job, suggest="未知建议任务", payload={}, citations=[])
    if job == "map_cases":
        return map_cases(requirement or {}, cases or [])
    if job == "classify_fail":
        entity = requirement or release or {}
        kind = "req_test" if requirement else "release_regression"
        return classify_fail(_task_of(entity.get("runs") or [], kind, tasks or []))
    if job == "draft_sign":
        return draft_sign(requirement or {}, tasks or [])
    if job == "draft_gate":
        return draft_gate(release or {}, requirements or [], tasks or [])
    return pick_regression(release or {}, requirements or [], suites or [])
