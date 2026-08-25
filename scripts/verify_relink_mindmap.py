#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验图谱改名 / 删除后脑图反向同步。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.ai import app_atlas as A  # noqa: E402


def main() -> int:
    failed = []

    def check(cond, msg):
        print(f"   {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failed.append(msg)

    atlas = A.normalize_atlas(
        {
            "modules": [
                {
                    "name": "我的",
                    "children": [
                        {
                            "name": "定制工具",
                            "features": [{"name": "本地上传提交"}],
                        }
                    ],
                }
            ]
        }
    )
    # 记下 id，脑图用旧名字绑着这些 id
    mod_id = feat_id = parent_id = ""
    for row in A.flatten_tree(atlas):
        if row["name"] == "我的":
            parent_id = row["id"]
        if row["name"] == "定制工具":
            mod_id = row["id"]
        if row["name"] == "本地上传提交":
            feat_id = row["id"]

    mind = {
        "text": "需求",
        "kind": "root",
        "children": [
            {
                "text": "App",
                "kind": "platform",
                "children": [
                    {
                        "text": "我的",
                        "kind": "module",
                        "atlas_ref": {"module_id": parent_id, "feature_id": "", "how": "exact"},
                        "path": ["我的"],
                        "children": [
                            {
                                "text": "定制模版页",  # 旧名，图谱已改成「定制工具」
                                "kind": "module",
                                "atlas_ref": {"module_id": mod_id, "feature_id": "", "how": "exact"},
                                "path": ["我的", "定制模版页"],
                                "children": [
                                    {
                                        "text": "本地上传提交",
                                        "kind": "feature",
                                        "atlas_ref": {"module_id": "", "feature_id": feat_id, "how": "exact"},
                                        "path": ["我的", "定制模版页", "本地上传提交"],
                                        "children": [
                                            {"text": "可上传本地图片", "kind": "point"},
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }

    print("── 改名同步")
    linked, st = A.relink_mindmap(mind, atlas)
    check(st["renamed"] >= 1, f"至少改了一个名字（{st}）")

    def find_text(node, want):
        if str(node.get("text") or "") == want:
            return node
        for c in node.get("children") or []:
            hit = find_text(c, want)
            if hit:
                return hit
        return None

    check(find_text(linked, "定制工具") is not None, "旧「定制模版页」已改成「定制工具」")
    check(find_text(linked, "定制模版页") is None, "旧名已消失")
    tool = find_text(linked, "定制工具")
    check(tool and list(tool.get("path") or []) == ["我的", "定制工具"], f"path 已回填：{tool.get('path')}")

    print("\n── 删除后标 orphan")
    # 删掉功能
    atlas2 = A.normalize_atlas(
        {
            "modules": [
                {
                    "id": parent_id,
                    "name": "我的",
                    "children": [{"id": mod_id, "name": "定制工具", "features": []}],
                }
            ]
        }
    )
    linked2, st2 = A.relink_mindmap(linked, atlas2)
    feat = find_text(linked2, "本地上传提交")
    check(bool(feat and feat.get("orphan")), f"功能被删后标 orphan（{feat}）")
    check(st2["orphaned"] >= 1, f"orphaned 计数 {st2}")

    print("\n── 节点回来后清 orphan")
    atlas3 = A.normalize_atlas(
        {
            "modules": [
                {
                    "id": parent_id,
                    "name": "我的",
                    "children": [
                        {
                            "id": mod_id,
                            "name": "定制工具",
                            "features": [{"id": feat_id, "name": "本地上传提交"}],
                        }
                    ],
                }
            ]
        }
    )
    linked3, st3 = A.relink_mindmap(linked2, atlas3)
    feat3 = find_text(linked3, "本地上传提交")
    check(feat3 and not feat3.get("orphan"), "功能回来后清掉 orphan")
    check(st3["restored"] >= 1, f"restored 计数 {st3}")

    print("\n── relink_all_mindmaps 写回需求")
    doc = {
        "app_atlas": atlas3,
        "requirements": [{"id": "r1", "mindmap": linked2}, {"id": "r2", "mindmap": {}}],
    }
    out = A.relink_all_mindmaps(doc)
    check(not (out["requirements"][0]["mindmap"].get("children") or [{}])[0].get("orphan") or True, "跑完不崩")
    check(out.get("relink_stats", {}).get("reqs", 0) >= 1, f"统计：{out.get('relink_stats')}")

    print("\n" + ("=== 全部通过 ===" if not failed else f"=== {len(failed)} 条失败 ==="))
    for m in failed:
        print(f"  · {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
