#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验「脑图 → 应用图谱」反推，以及导入链路的端到端行为。

盯五件事：

  1. **端不进图谱。** app_atlas 的数据模型里没有 platform 这一层，照抄脑图层级会在顶上
     造出 App / Web / 端到端三个模块，每个下面再复制一套同名子树。
  2. **测试点不进图谱**，只落在 point_count 上。
  3. **两处测试点判据一致。** atlas_from_mindmap.is_point 和
     qa_role_jobs.collect_mindmap_points 必须逐节点同意，否则 point_count 和
     understanding.points 会对不上账，而且没人会发现。
  4. **回填的 path 是图谱路径。** 前端 placeBranch 拿它去 walkPath 图谱树，写成脑图
     祖先链就定位不到，脑图还是会整棵挂到根下。
  5. **确定性决定落库方式。** 全是精确命中就直接合并；有新增或模糊就入 patch 队列，
     app_atlas 一个字都不许动。

用法：python scripts/verify_mindmap_to_atlas.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.ai import app_atlas as A  # noqa: E402
from server.services.ai import atlas_from_mindmap as AFM  # noqa: E402
from server.services.cover_import import import_cover  # noqa: E402
from server.services.qa_role_jobs import collect_mindmap_points  # noqa: E402

PKG = "com.mathmagic.zaohaowu"

BASE_ATLAS = A.normalize_atlas(
    {
        "modules": [
            {
                "name": "我的",
                "children": [{"name": "定制模版页", "features": [{"name": "本地上传提交"}]}],
            }
        ]
    }
)

# 两个端、既有节点 + 新节点、带子情况的测试点、一句话式的节点
MARKDOWN = """# 传图定制优化
## App
### 我的
#### 定制模版页
- 本地上传提交
  - 选择本地图片后可提交
  - 超过 10MB 时提示图片过大，不允许提交
- 模版列表加载
  - 首屏展示 20 条
## 运营平台
### 模型管理
- 新增模型
  - 必填校验
"""

TREE = {
    "text": "传图定制优化",
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
                        {
                            "text": "定制模版页",
                            "kind": "module",
                            "children": [
                                {
                                    "text": "本地上传提交",
                                    "kind": "feature",
                                    "children": [
                                        {"text": "选择本地图片后可提交", "kind": "point"},
                                        {"text": "取消后回到列表", "kind": "point"},
                                    ],
                                },
                                {
                                    "text": "模版列表加载",
                                    "kind": "feature",
                                    "children": [{"text": "首屏展示 20 条", "kind": "point"}],
                                },
                            ],
                        }
                    ],
                }
            ],
        },
        {
            "text": "运营平台",
            "kind": "platform",
            "platform": "web",
            "children": [
                {
                    "text": "模型管理",
                    "kind": "module",
                    "children": [
                        {
                            "text": "新增模型",
                            "kind": "feature",
                            "children": [{"text": "必填校验", "kind": "point"}],
                        }
                    ],
                }
            ],
        },
    ],
}


def walk(node):
    yield node
    for kid in node.get("children") or []:
        if isinstance(kid, dict):
            yield from walk(kid)


def main() -> int:
    failed = []

    def check(cond, msg):
        print(f"   {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failed.append(msg)

    print("── 反推：端和测试点都不进图谱")
    out = AFM.infer(BASE_ATLAS, TREE, req_id="req-1", package=PKG)
    rows = A.flatten_tree(out.atlas)
    paths = [r["path"] for r in rows]
    names = {r["name"] for r in rows}
    check("App" not in names and "Web" not in names and "端到端" not in names, f"图谱里没有端节点：{sorted(names)}")
    check("运营平台" not in names, "「运营平台」被认成 Web 这个端，没变成模块")
    point_texts = {p["text"] for p in collect_mindmap_points(TREE)}
    leaked = point_texts & names
    check(not leaked, f"测试点没进图谱（漏进来的：{sorted(leaked)}）")

    print("\n── 已有节点复用，新节点新建")
    check(paths[:3] == ["我的", "我的 / 定制模版页", "我的 / 定制模版页 / 本地上传提交"], f"既有骨架保持原样：{paths[:3]}")
    check("我的 / 定制模版页 / 模版列表加载" in paths, "新功能挂在对上的模块下，没另起一棵树")
    check("模型管理" in paths, "Web 端的模块直接挂顶层，不套一层「运营平台」")
    ids_before = {r["id"] for r in A.flatten_tree(BASE_ATLAS)}
    check(ids_before <= {r["id"] for r in rows}, "复用的节点 id 没变（否则挂载关系全断）")

    print("\n── 覆盖密度落在 point_count 上")
    by_path = {r["path"]: r for r in rows}
    check(by_path["我的 / 定制模版页 / 本地上传提交"]["point_count"] == 2, "本地上传提交 = 2 个点")
    check(by_path["我的 / 定制模版页 / 模版列表加载"]["point_count"] == 1, "模版列表加载 = 1 个点")
    check(out.points == len(collect_mindmap_points(TREE)), f"总点数 {out.points} 和 collect_mindmap_points 一致")
    check(sum(r["point_count"] for r in rows) == out.points, "各节点 point_count 加起来等于总点数，没重复计数")

    print("\n── 两处测试点判据必须逐节点一致")
    # collect_mindmap_points 返回的是 dict 副本，只能按文案比对
    want_texts = sorted(str(p.get("text") or "") for p in collect_mindmap_points(TREE))
    got_texts = sorted(str(n.get("text") or "") for n in walk(TREE) if AFM.is_point(n))
    check(want_texts == got_texts, f"is_point 与 collect_mindmap_points 一致（{len(got_texts)} 个点）")

    print("\n── 回填的 path 是图谱路径（前端靠它定位）")
    tagged = {str(n.get("text") or ""): n for n in walk(out.mindmap)}
    check(
        tagged["本地上传提交"].get("path") == ["我的", "定制模版页", "本地上传提交"],
        f"结构节点 path 跳过 root/platform：{tagged['本地上传提交'].get('path')}",
    )
    check(
        tagged["必填校验"].get("path") == ["模型管理", "新增模型", "必填校验"],
        f"测试点 path = 图谱路径 + 自己：{tagged['必填校验'].get('path')}",
    )
    ref = tagged["本地上传提交"].get("atlas_ref") or {}
    check(
        ref.get("feature_id") == by_path["我的 / 定制模版页 / 本地上传提交"]["id"] and ref.get("how") == "exact",
        f"atlas_ref 指向真实功能 id：{ref}",
    )
    check(not (tagged["必填校验"].get("atlas_ref") or {}), "测试点不带 atlas_ref（它不是图谱节点）")
    for node in walk(out.mindmap):
        if node.get("kind") in ("root", "platform"):
            check(not node.get("path"), f"端/根节点不写 path：{node.get('text')} -> {node.get('path')}")

    print("\n── 导入链路：全精确 -> 直接合并")
    only_known = {
        "text": "传图定制",
        "kind": "root",
        "children": [
            {
                "text": "App",
                "kind": "platform",
                "children": [
                    {
                        "text": "我的",
                        "kind": "module",
                        "children": [
                            {
                                "text": "定制模版页",
                                "kind": "module",
                                "children": [
                                    {
                                        "text": "本地上传提交",
                                        "kind": "feature",
                                        "children": [{"text": "可以提交", "kind": "point"}],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    doc = {"requirements": [{"id": "req-1", "title": "传图定制"}], "app_atlas": BASE_ATLAS, "atlas_patches": []}
    res = import_cover(
        qa_process=doc,
        kind="mindmap",
        requirement_id="req-1",
        text=json.dumps(only_known, ensure_ascii=False),
        package=PKG,
    )
    qa = res["qa_process"]
    check(res["atlas"] == "merged", f"标成 merged（实际 {res['atlas']}）")
    check(not qa.get("atlas_patches"), "没有多余的 patch")
    check(res["created"] == 0 and res["review"] == 0, f"没有新增也没有待确认（{res['created']}/{res['review']}）")
    merged = {r["path"]: r for r in A.flatten_tree(qa["app_atlas"])}
    check(merged["我的 / 定制模版页 / 本地上传提交"]["point_count"] == 1, "point_count 落库了")
    check("req-1" in merged["我的 / 定制模版页 / 本地上传提交"]["req_ids"], "需求挂上了")
    check(
        res["requirement"].get("atlas_paths") == list(merged.keys()),
        f"挂载关系回写到需求上：{res['requirement'].get('atlas_paths')}",
    )

    print("\n── 导入链路：有新增 -> 入 patch，图谱一个字不动")
    doc2 = {"requirements": [{"id": "req-1", "title": "传图定制优化"}], "app_atlas": BASE_ATLAS, "atlas_patches": []}
    res2 = import_cover(
        qa_process=doc2, kind="mindmap", requirement_id="req-1", text=MARKDOWN, package=PKG
    )
    qa2 = res2["qa_process"]
    check(res2["atlas"] == "patch", f"标成 patch（实际 {res2['atlas']}）")
    check(len(qa2.get("atlas_patches") or []) == 1, "入了一条 patch")
    check(
        A.flatten_tree(qa2["app_atlas"]) == A.flatten_tree(BASE_ATLAS),
        "app_atlas 完全没动，等人在「图谱变更」里点头",
    )
    patch = (qa2.get("atlas_patches") or [{}])[0]
    check(patch.get("status") == "pending", "patch 是 pending")
    check((patch.get("source") or {}).get("kind") == "mindmap_import", f"来源标了 mindmap_import：{patch.get('source')}")
    check(res2["points"] == 4 and res2["matched"] == 3 and res2["created"] == 3, f"统计对：{ {k: res2[k] for k in ('points','matched','created')} }")

    print("\n── 同一份脑图导两次不许堆第二条 patch")
    res3 = import_cover(
        qa_process=qa2, kind="mindmap", requirement_id="req-1", text=MARKDOWN, package=PKG
    )
    check(len(res3["qa_process"].get("atlas_patches") or []) == 1, "还是一条")
    check(res3["atlas"] == "pending", f"标成 pending（实际 {res3['atlas']}）")

    print("\n── 换个没有画像的应用不许被带偏")
    doc4 = {"requirements": [{"id": "req-1", "title": "某功能"}], "app_atlas": A.empty_atlas(), "atlas_patches": []}
    res4 = import_cover(
        qa_process=doc4, kind="mindmap", requirement_id="req-1", text=MARKDOWN, package="com.someone.else"
    )
    after4 = (res4["qa_process"].get("atlas_patches") or [{}])[0].get("after") or {}
    names4 = {r["name"] for r in A.flatten_tree(after4)}
    check("App" not in names4, f"通用端名仍然认得出，没变成模块：{sorted(names4)}")
    check(res4["points"] > 0, f"照样解析出测试点（{res4['points']} 个）")

    print("\n" + ("=== 全部通过 ===" if not failed else f"=== {len(failed)} 条失败 ==="))
    for m in failed:
        print(f"  · {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
