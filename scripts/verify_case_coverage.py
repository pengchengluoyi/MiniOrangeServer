#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验用例生成的覆盖不变量（不联网，打桩 LLM）。

守住六条：
  1. 模型正常时：零模板桩、零 failures
  2. 输出被截断时：拆半重试能把覆盖补回来，而不是整批退化成模板桩
  3. 模型一直失败时：允许落桩，但**必须**在 failures 里显式上报，且 stats 能分清真/桩
  4. replace 重试时：人工锁定（locked）的用例不许被删
  5. 补写模板 / 定点重写：已有真用例留下，只动指定范围
  6. 重试脑图不再扩散成 analyze + 整表重写用例

第 3 条是重点。以前这里静默补桩、界面覆盖率照样 100%，人只能整体重试，一轮 10 分钟 ——
这就是「跑了 50 分钟用例覆盖不全」的直接来源。

用法：python scripts/verify_case_coverage.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services import qa_role_jobs as jobs  # noqa: E402

POINTS = [
    {"text": "定制页可上传本地图片", "detail": "选图后可下单", "kind": "正向"},
    {"text": "上传失败有兜底提示", "detail": "网络失败可重试", "kind": "异常"},
    {"text": "图片格式不支持时拦截", "detail": "非法格式", "kind": "边界"},
    {"text": "创意定制仍可对话出图", "detail": "维持原逻辑", "kind": "正向"},
    {"text": "运营平台可配置模型", "detail": "后台新增模型", "kind": "正向"},
    {"text": "未登录点下单跳登录", "detail": "权限", "kind": "异常"},
]


def _req() -> dict:
    children = [
        {
            "id": f"n{i}",
            "text": p["text"],
            "kind": "point",
            "point_id": f"tp{i + 1}",
            "point_kind": p["kind"],
            "detail": p["detail"],
            "children": [],
        }
        for i, p in enumerate(POINTS)
    ]
    return {
        "id": "req-1",
        "title": "定制页工具链路优化",
        "mindmap": {"title": "定制页", "children": [
            {"id": "p-app", "text": "App", "kind": "platform", "platform": "app", "children": children}
        ]},
        "understanding": {"journeys": [{"entry": "我的", "via": ["定制模版页"], "page": "定制页"}]},
    }


def _cases_for(points: list) -> dict:
    out = []
    for p in points:
        for aspect in jobs._expected_aspects(p):
            out.append({
                "case_id": f"draft-{p['id']}-{aspect}",
                "name": f"{p['text'][:20]}·{aspect}",
                "module": "我的-定制模版页-定制页",
                "aspect": aspect,
                "precondition": "账号可用",
                "steps": "1. 打开应用\n2. 进入定制页\n3. 操作",
                "expected": "1. 符合预期",
                "point_ids": [p["id"]],
                "platform": "app",
            })
    return {"cases": out, "missing_points": []}


def _points_from_user(user: str) -> list:
    return json.loads(user).get("points") or []


def scenario(name: str, fake, *, replace=False, seed_cases=None, point_ids=None, rewrite_stubs=False):
    calls = {"n": 0}

    def patched(system, user, **kw):
        calls["n"] += 1
        return fake(_points_from_user(user), calls["n"], kw)

    orig = jobs._ask_json
    jobs._ask_json = patched
    try:
        req = _req()
        if seed_cases:
            req["draft_cases"] = seed_cases
        art = jobs.draft_cases(
            req, [], replace=replace, point_ids=point_ids, rewrite_stubs=rewrite_stubs
        )
    finally:
        jobs._ask_json = orig
    payload = art.get("payload") or {}
    st = payload.get("stats") or {}
    print(f"\n── {name}")
    print(f"   调用 {calls['n']} 次 · {art.get('suggest')}")
    print(f"   stats={st}")
    if payload.get("failures"):
        for f in payload["failures"]:
            print(f"   failure: {f['reason']} · {len(f.get('point_ids') or [])} 点 · {f.get('detail')}")
    if payload.get("aspect_gaps"):
        print(f"   aspect_gaps: {len(payload['aspect_gaps'])} 个点缺情况")
    return payload, st, calls["n"]


def check_id_alignment(check):
    """脑图叶子在两条路径上必须算出同一个 id，否则用例挂不上测试点。

    回归的是一个真实 bug：_norm_points 用 `id or point_id`、_sync_points_from_mindmap 用
    `point_id or id`，同一个叶子算出 "n1-1-1" 和 "tp1" 两个 id，apply_cases 永远匹配不上，
    结果每个测试点都显示成「没挂用例」，coverReady 直接卡住 —— 界面上就是「用例覆盖不全测试点」。
    """
    print("\n── id 对齐（脑图叶子 → understanding.points → 用例挂载）")
    req = _req()
    req2 = jobs.apply_mindmap(req, req["mindmap"])
    und_ids = [p["id"] for p in (req2.get("understanding") or {}).get("points") or []]
    target_ids = [p["id"] for p in jobs._norm_points(jobs.collect_mindmap_points(req2.get("mindmap")))]
    print(f"   understanding.points: {und_ids}")
    print(f"   draft_cases target  : {target_ids}")
    check(und_ids == target_ids, "两条路径算出同一组测试点 id")

    orig = jobs._ask_json
    jobs._ask_json = lambda system, user, **kw: (_cases_for(_points_from_user(user)), {"engine": "llm"})
    try:
        art = jobs.draft_cases(req2, [])
    finally:
        jobs._ask_json = orig
    req3 = jobs.apply_cases(req2, art.get("payload") or {})
    points = (req3.get("understanding") or {}).get("points") or []
    unlinked = [p["id"] for p in points if not p.get("case_ids")]
    check(not unlinked, f"每个测试点都挂上了用例（未挂：{unlinked or '无'}）")


def main() -> int:
    failed = []

    def check(cond, msg):
        print(f"   {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failed.append(msg)

    # 1) 模型正常
    payload, st, _ = scenario("模型正常", lambda pts, n, kw: (_cases_for(pts), {"engine": "llm"}))
    check(st.get("stub_cases") == 0, "零模板桩")
    check(not payload.get("failures"), "零 failures")
    check(st.get("covered_points") == len(POINTS), f"真实覆盖全部 {len(POINTS)} 个点")
    check(not payload.get("aspect_gaps"), "无情况缺口")

    # 2) 一次要 3 个以上点就截断 → 拆半重试应补回覆盖
    def truncating(pts, n, kw):
        if len(pts) > 2:
            partial = _cases_for(pts[:1])
            return partial, {
                "engine": "llm",
                "truncated": True,
                "salvaged": True,
                "fail_kind": "",
                "completion_tokens": kw["max_tokens"],
            }
        return _cases_for(pts), {"engine": "llm"}

    payload, st, n = scenario("输出截断（>2 个点就截）", truncating)
    check(st.get("stub_cases") == 0, "拆半重试补回覆盖，零模板桩")
    check(st.get("covered_points") == len(POINTS), "覆盖全部点")
    check(n > 2, f"确实发生了拆半重试（调用 {n} 次）")

    # 3) 模型一直失败（HTTP 层）→ 允许落桩，但必须上报，且原因要分类正确
    payload, st, _ = scenario(
        "模型一直失败（HTTP）",
        lambda pts, n, kw: (None, {"engine": "llm", "error": "http: timeout", "fail_kind": "http"}),
    )
    check(st.get("stub_cases") == st.get("cases"), "全部是模板桩")
    check(st.get("covered_points") == 0, "真实覆盖为 0（不许把桩算成覆盖）")
    reported = {pid for f in payload.get("failures") or [] for pid in (f.get("point_ids") or [])}
    check(len(reported) == len(POINTS), f"全部 {len(POINTS)} 个点都进了 failures（实际 {len(reported)}）")
    reasons = {f.get("reason") for f in payload.get("failures") or []}
    check(reasons == {"llm_error"}, f"失败原因分类正确（llm_error，实际 {reasons}）")
    check(len(payload.get("aspect_gaps") or []) == len(POINTS), "情况缺口如实上报")

    # 3b) 截断且救不回来 → 原因必须是 truncated，不能混成 parse_failed
    payload, st, _ = scenario(
        "截断且救不回",
        lambda pts, n, kw: (None, {"engine": "llm", "truncated": True, "fail_kind": "truncated"}),
    )
    reasons = {f.get("reason") for f in payload.get("failures") or []}
    check(reasons == {"truncated"}, f"截断被单独识别（实际 {reasons}）")

    # 4) replace 重试不许删人工锁定用例
    locked = [{
        "case_id": "draft-human-1", "name": "人工写的关键用例", "aspect": "正向",
        "steps": "1. 人工步骤", "expected": "1. 人工预期",
        "point_ids": ["tp1"], "platform": "app", "origin": "human", "locked": True,
    }]
    payload, st, _ = scenario(
        "replace 重试 + 人工锁定用例", lambda pts, n, kw: (_cases_for(pts), {"engine": "llm"}),
        replace=True, seed_cases=locked,
    )
    kept = [c for c in payload.get("cases") or [] if c.get("case_id") == "draft-human-1"]
    check(len(kept) == 1, "人工锁定用例被保留")
    check(st.get("locked_kept") == 1, "stats 记录了保留数")
    check(
        st.get("covered_points") <= st.get("points"),
        f"覆盖数不超过测试点总数（{st.get('covered_points')}/{st.get('points')}）",
    )

    # 5) id 对齐
    check_id_alignment(check)

    # 6) 定点重试：已有真用例留下，模板桩被扔掉再补；指定测试点只动那几个
    good = [{
        "case_id": "draft-keep-1", "name": "已有真用例", "aspect": "正向",
        "steps": "1. 已写", "expected": "1. 已写",
        "point_ids": ["tp1"], "platform": "app", "origin": "llm",
    }]
    stub = [{
        "case_id": "draft-stub-1", "name": "模板", "aspect": "正向",
        "steps": "1. 打开应用", "expected": "1. 符合预期",
        "point_ids": ["tp2"], "platform": "app", "origin": "stub",
    }]
    payload, st, n = scenario(
        "补写模板桩，已有真用例不动",
        lambda pts, n, kw: (_cases_for(pts), {"engine": "llm"}),
        seed_cases=good + stub,
        rewrite_stubs=True,
    )
    ids = {c.get("case_id") for c in payload.get("cases") or []}
    check("draft-keep-1" in ids, "已有真用例还在")
    check("draft-stub-1" not in ids, "模板桩被扔掉了")

    orig = jobs._ask_json
    jobs._ask_json = lambda system, user, **kw: (_cases_for(_points_from_user(user)), {"engine": "llm"})
    try:
        req = _req()
        req["draft_cases"] = good + [{
            "case_id": "draft-tp3", "name": "要被重写", "aspect": "正向",
            "steps": "旧", "expected": "旧", "point_ids": ["tp3"], "origin": "llm",
        }]
        art = jobs.draft_cases(req, [], replace=True, point_ids=["tp3"])
    finally:
        jobs._ask_json = orig
    ids = {c.get("case_id") for c in (art.get("payload") or {}).get("cases") or []}
    check("draft-keep-1" in ids, "定点重写没有动其他点的用例")
    check("draft-tp3" not in ids, "被指定的测试点旧用例丢掉了")

    print("\n── 重试脑图不再扩散成整表重写用例")
    called = []
    orig_an = jobs.analyze_req
    orig_mm = jobs.draft_mindmap
    orig_cs = jobs.draft_cases

    def fake_mm(req, *a, **k):
        called.append("draft_mindmap")
        tree = req.get("mindmap") or {"text": "root", "kind": "root", "children": []}
        return {"payload": tree, "engine": "llm", "suggest": "ok"}

    def fake_cs(req, *a, **k):
        called.append(("draft_cases", dict(k)))
        return {"payload": {"cases": req.get("draft_cases") or []}, "engine": "llm", "suggest": "ok"}

    def fake_an(req, *a, **k):
        called.append("analyze_req")
        return {"payload": {}, "engine": "llm", "suggest": "ok"}

    jobs.analyze_req = fake_an
    jobs.draft_mindmap = fake_mm
    jobs.draft_cases = fake_cs
    try:
        req = _req()
        req["source_text"] = "需求原文若干"
        req["draft_cases"] = good
        doc = {"requirements": [req], "autonomy": {"enabled": True}}
        jobs._tick_body(
            qa_process=doc,
            jobs=["draft_mindmap"],
            user_note="漏了后台配置",
            force=True,
            requirement_id="req-1",
        )
    finally:
        jobs.analyze_req = orig_an
        jobs.draft_mindmap = orig_mm
        jobs.draft_cases = orig_cs
    check(called == ["draft_mindmap"], f"只跑脑图（实际 {called}）")

    print("\n" + ("=== 全部通过 ===" if not failed else f"=== {len(failed)} 条失败 ==="))
    for m in failed:
        print(f"  · {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
