#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""策略命中去重；应用基础逻辑默认槽位可空且不注入。"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.system_settings_service import (  # noqa: E402
    PLAYBOOK_KNOWLEDGE_CATEGORY,
    PLAYBOOK_KNOWLEDGE_SLOTS,
    dedupe_knowledge_hits,
    ensure_playbook_knowledge_slots,
    match_testing_knowledge,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def main() -> None:
    titles = [s["title"] for s in PLAYBOOK_KNOWLEDGE_SLOTS]
    for t in ("如何登录", "如何退出登录", "如何切换环境", "如何判断登录态", "访客浏览", "底栏"):
        _assert(t in titles, f"missing slot {t}")

    dup = dedupe_knowledge_hits(
        [
            {"id": "a", "title": "脑图修订·x", "content": "1. foo"},
            {"id": "b", "title": "脑图修订·x", "content": "1. foo"},
            {"uid": "learned/knowledge/a", "title": "脑图修订·x", "content": "1. foo"},
            {"id": "c", "title": "已登录新人进入Agent对话页的展示规则", "content": "A"},
            {"id": "d", "title": "已登录新人进入Agent对话页的展示规则", "content": "B 不同正文"},
        ]
    )
    _assert(len(dup) == 2, f"title-level dedupe failed {dup}")
    titles = [r["title"] for r in dup]
    _assert(titles.count("脑图修订·x") == 1, titles)
    _assert(titles.count("已登录新人进入Agent对话页的展示规则") == 1, titles)

    store = [
        {
            "id": "empty",
            "title": "如何登录",
            "content": "",
            "category": PLAYBOOK_KNOWLEDGE_CATEGORY,
            "enabled": True,
            "review_status": "approved",
            "tags": [],
            "app_ids": ["app1"],
        },
        {
            "id": "k1",
            "title": "脑图修订·新人礼",
            "content": "1. 清理",
            "category": "测试脑图",
            "enabled": True,
            "review_status": "approved",
            "tags": [],
            "app_ids": ["app1"],
        },
        {
            "id": "k2",
            "title": "脑图修订·新人礼",
            "content": "1. 清理",
            "category": "测试脑图",
            "enabled": True,
            "review_status": "approved",
            "tags": [],
            "app_ids": ["app1"],
        },
    ]
    with patch(
        "server.services.system_settings_service.list_testing_knowledge",
        return_value=store,
    ):
        hits = match_testing_knowledge("脑图修订 新人礼 清理", app_id="app1", limit=5)
    _assert(len(hits) == 1, f"match should collapse dupes, got {hits}")
    _assert(hits[0]["title"] == "脑图修订·新人礼", str(hits))
    _assert(all(h["title"] != "如何登录" for h in hits), "empty playbook slot must not match")

    created: list = []

    def fake_upsert(item):
        created.append(item)
        return dict(item)

    with patch(
        "server.services.system_settings_service.list_testing_knowledge",
        return_value=[],
    ):
        with patch(
            "server.services.system_settings_service.upsert_knowledge_item",
            side_effect=fake_upsert,
        ):
            rows = ensure_playbook_knowledge_slots("app-test")
    _assert(len(rows) == len(PLAYBOOK_KNOWLEDGE_SLOTS), f"seeded {len(rows)}")
    _assert(all(r.get("content") == "" for r in rows), "slot content should be empty")
    _assert(
        all(r.get("category") == PLAYBOOK_KNOWLEDGE_CATEGORY for r in rows),
        "category",
    )

    existing = [
        {
            "id": "x",
            "title": "如何登录",
            "content": "已有说明",
            "app_ids": ["app-test"],
            "playbook_slot": "login_how",
        }
    ]
    created.clear()
    with patch(
        "server.services.system_settings_service.list_testing_knowledge",
        return_value=existing,
    ):
        with patch(
            "server.services.system_settings_service.upsert_knowledge_item",
            side_effect=fake_upsert,
        ):
            again = ensure_playbook_knowledge_slots("app-test")
    _assert(
        all(r.get("title") != "如何登录" for r in again),
        "must not overwrite existing 如何登录",
    )
    from pathlib import Path

    prompt_src = Path("server/services/ai/regression/prompts.py").read_text(encoding="utf-8")
    _assert("playbook_block" not in prompt_src, "prompts 不应再有 playbook_block")
    planner_src = Path("server/services/ai/regression/planner.py").read_text(encoding="utf-8")
    _assert("playbook_block" not in planner_src, "planner 不应再有 playbook_block")

    mixed = []
    for i, cat in enumerate(["应用基础逻辑", "业务逻辑", "UI导航", "登录注册", "其他"]):
        mixed.append({
            "id": f"top-{i}",
            "title": f"登录路径{i}",
            "content": "登录后进入首页",
            "category": cat,
            "enabled": True,
            "review_status": "approved",
            "tags": ["登录"],
            "app_ids": ["app1"],
        })
    with patch(
        "server.services.system_settings_service.list_testing_knowledge",
        return_value=mixed,
    ):
        top3 = match_testing_knowledge("登录 首页", app_id="app1", limit=3)
    _assert(len(top3) == 3, f"global top3 must be 3, got {len(top3)}")

    from server.services.knowledge_capture_service import capture_case_knowledge
    from server.services.ai.dispatch_log import infer_call_meta

    with patch(
        "server.services.system_settings_service.knowledge_capture_enabled",
        return_value=False,
    ):
        skipped = capture_case_knowledge(app_id="app1", task_id="t", case_id="c1")
    _assert(skipped == [], f"capture off must skip, got {skipped}")

    tagged = infer_call_meta(output={"tags": [], "replaces": [], "reason": "无新状态"})
    _assert(tagged.get("skill") == "account-tag", f"account-tag infer failed {tagged}")
    _assert(tagged.get("job") == "account-tag", tagged)


if __name__ == "__main__":
    main()
