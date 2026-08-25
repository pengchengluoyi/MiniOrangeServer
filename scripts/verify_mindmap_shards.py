#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验脑图分片：骨架不含点、填点挂到功能上、代码能指出覆盖缺口。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.ai.cover import checks  # noqa: E402
from server.services.qa_role_jobs import _attach_points, _points_payload  # noqa: E402


TREE = {
    "text": "需求",
    "kind": "root",
    "children": [
        {
            "text": "App",
            "kind": "platform",
            "platform": "app",
            "children": [
                {
                    "text": "我的",
                    "kind": "module",
                    "children": [
                        {"text": "本地上传提交", "kind": "feature", "path": ["我的", "本地上传提交"], "children": []},
                        {
                            "text": "对话生成",
                            "kind": "feature",
                            "path": ["我的", "对话生成"],
                            "children": [
                                {"text": "可与 agent 对话出图", "kind": "point"},
                                {"text": "对话中断可重试", "kind": "point"},
                            ],
                        },
                    ],
                }
            ],
        }
    ],
}

REQ = {
    "understanding": {
        "new_features": [{"name": "本地上传提交", "focus": True, "platform": "app"}],
        "keep_features": [{"name": "对话生成", "platform": "app"}],
        "exceptions": ["上传失败有兜底提示"],
        "journeys": [{"entry": "我的", "via": ["定制模版页"], "page": "定制页", "platform": "app"}],
    }
}


def main() -> int:
    failed = []

    def check(cond, msg):
        print(f"   {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failed.append(msg)

    print("── 功能枝与薄枝")
    feats = {n["text"]: n for n in checks.feature_nodes(TREE)}
    check(set(feats) == {"本地上传提交", "对话生成"}, f"功能枝：{sorted(feats)}")
    thin = [n["text"] for n in checks.thin_features(TREE, min_points=2)]
    check(thin == ["本地上传提交"], f"点不够的枝：{thin}")
    check(checks.point_count(TREE) == 2, f"全树点数 {checks.point_count(TREE)}")

    print("\n── 填点挂到功能上，不另起一棵树")
    feat = feats["本地上传提交"]
    parsed = {"points": [
        {"text": "选择本地图片后可提交", "kind": "正向"},
        {"text": "超过 10MB 提示过大", "kind": "边界"},
        {"text": "选择本地图片后可提交", "kind": "正向"},
    ]}
    points = _points_payload(parsed, platform="app", parent_path=["我的", "本地上传提交"])
    added = _attach_points(feat, points)
    check(added == 2, f"去重后挂上 {added} 个点")
    check(checks.point_count(feat) == 2, "点在该功能下面")
    check(checks.thin_features(TREE, min_points=2) == [], "补完后不再是薄枝")

    print("\n── 代码校验能指出缺的覆盖")
    g = checks.gaps(REQ, TREE)
    kinds = {row["kind"] for row in g}
    names = {row["name"] for row in g}
    check("exception" in kinds, f"缺异常点：{g}")
    check(any("上传失败" in n for n in names), f"异常文案对得上：{names}")
    check("journey" in kinds, "缺 journeys 路径（定制模版页/定制页没出现）")
    check("new_feature" not in kinds and "keep_feature" not in kinds, "已有的功能不再报缺")

    empty = {"text": "空", "kind": "root", "children": []}
    g2 = checks.gaps(REQ, empty)
    check(len(g2) >= 4, f"空树上四个清单都缺（实际 {len(g2)}）：{[x['kind'] for x in g2]}")

    print("\n" + ("=== 全部通过 ===" if not failed else f"=== {len(failed)} 条失败 ==="))
    for m in failed:
        print(f"  · {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
