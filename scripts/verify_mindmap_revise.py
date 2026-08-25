#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验脑图重试：压缩上一版 + 有 children 时走 revise，而不是整树 skeleton。"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services import qa_role_jobs as jobs  # noqa: E402


PREV = {
    "title": "新人礼切换为折扣定制",
    "children": [
        {
            "id": "app-root",
            "text": "App",
            "kind": "platform",
            "platform": "app",
            "children": [
                {
                    "id": "m1",
                    "text": "首页",
                    "kind": "module",
                    "children": [
                        {
                            "id": "f1",
                            "text": "新用户banner",
                            "kind": "feature",
                            "children": [
                                {
                                    "id": "p1",
                                    "text": "新用户可见新人特惠banner",
                                    "kind": "point",
                                    "point_id": "tp1",
                                    "detail": "很长很长很长很长很长很长很长很长很长很长很长很长的说明",
                                    "case_ids": ["c1"],
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}


def main() -> int:
    fails = 0
    compact = jobs._compact_mindmap_for_prompt(PREV)
    detail = compact["children"][0]["children"][0]["children"][0]["children"][0].get("detail") or ""
    if len(detail) > 48:
        print("FAIL compact 未缩短 detail", len(detail))
        fails += 1
    else:
        print("OK   compact 缩短 detail")

    called = {"revise": 0, "generate": 0}

    def fake_revise(*a, **k):
        called["revise"] += 1
        return {
            "job": "draft_mindmap",
            "suggest": "revised",
            "engine": "llm",
            "payload": {"title": "t", "children": PREV["children"], "failures": [], "stats": {"mode": "revise"}},
        }

    def fake_generate_path(*a, **k):
        # 如果误走 skeleton，map_llm / ask 会被摸到 —— 这里直接让 draft 里 has_prev 分支必须命中
        called["generate"] += 1
        raise AssertionError("should not generate when prev exists")

    req = {
        "id": "r1",
        "title": "新人礼切换为折扣定制",
        "mindmap": PREV,
        "understanding": {"surfaces": [{"kind": "app"}], "journeys": [], "new_features": [], "keep_features": [], "exceptions": []},
    }
    with patch.object(jobs, "_draft_mindmap_revise", side_effect=fake_revise):
        out = jobs.draft_mindmap(req, [], None, user_note="入口在我的")
    if called["revise"] != 1 or out.get("suggest") != "revised":
        print("FAIL 有上一版应走 revise", called, out.get("suggest"))
        fails += 1
    else:
        print("OK   有上一版走 revise")

    req2 = {**req, "mindmap": {"title": "x", "children": []}}
    # 空 children 应走 generate：打桩 ask 路径里的 map_llm，避免真调模型
    with patch.object(jobs, "_mindmap_platforms", return_value=[("app", "App")]):
        with patch.object(jobs, "map_llm", return_value=[]):
            with patch.object(jobs.cover_jobs, "report"):
                with patch.object(jobs.cover_jobs, "inc"):
                    with patch.object(jobs.cover_checks, "gaps", return_value=[]):
                        with patch.object(jobs.cover_checks, "feature_nodes", return_value=[]):
                            with patch.object(jobs.cover_checks, "thin_features", return_value=[]):
                                art = jobs.draft_mindmap(req2, [], None, user_note="")
    if (art.get("payload") or {}).get("stats", {}).get("mode") == "revise":
        print("FAIL 空脑图不应标 revise", art.get("payload", {}).get("stats"))
        fails += 1
    else:
        print("OK   空脑图走首版生成")

    # 知识库捕获：mock upsert
    saved = {}

    def fake_upsert(item):
        saved.update(item)
        return {**item, "id": "k1"}

    with patch.dict("sys.modules", {}):
        with patch("server.services.system_settings_service.upsert_knowledge_item", side_effect=fake_upsert):
            with patch.object(jobs.dispatch, "ctx", return_value={"app_id": "app-zaohaowu"}):
                row = jobs._capture_mindmap_retry_note(note="漏了后台配置", req=req)
    if not row or saved.get("review_status") != "approved" or saved.get("content") != "漏了后台配置":
        print("FAIL 评论入库", saved, row)
        fails += 1
    else:
        print("OK   评论写入知识库 approved")

    if fails:
        print(f"\n{fails} failed")
        return 1
    print("\nall passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
