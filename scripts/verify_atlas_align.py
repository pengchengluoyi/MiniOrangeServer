#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验对齐层：名字 → 图谱节点。

盯三件事：

  1. 归一化只削「结构性尾缀」（页/模块/功能），不许动业务词 —— 一旦把「传图定制」
     削成「传图」，两个不同的功能就会被判成同一个。
  2. 模糊匹配不越界。错别字要能兜住，不同的东西不许合并，尤其是术语表里那些
     长得很像的词（定制页 / 定制模版页）。
  3. 同名节点靠父节点消歧。社区下的「点赞」和定制页下的「点赞」是两个功能，
     对齐时不能随字典顺序乱挑一个。

用法：python scripts/verify_atlas_align.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services.ai import app_atlas as A  # noqa: E402
from server.services.ai import atlas_align as align  # noqa: E402

ATLAS = A.normalize_atlas(
    {
        "modules": [
            {
                "name": "我的",
                "children": [
                    {
                        "name": "定制模版页",
                        "features": [{"name": "本地上传提交"}, {"name": "点赞"}],
                    }
                ],
            },
            {
                "name": "社区",
                "children": [
                    {
                        "name": "帖子详情页",
                        "features": [{"name": "点赞"}, {"name": "评论"}],
                    }
                ],
            },
        ]
    }
)

# 术语表里的词彼此是不同的东西，不许模糊合并
LEXICON = {"定制页": {}, "定制模版页": {}, "传图定制": {}, "创意定制": {}}

NORM_CASES = [
    ("定制页", "定制", "去掉结构性尾缀「页」"),
    ("定制模版页面", "定制模版", "「页面」也是结构性尾缀"),
    ("帖子详情页", "帖子详情", "多字尾缀"),
    ("首页", "首页", "剩不足两字就不削，否则「首页」变「首」"),
    ("我的", "我的", "没有尾缀不动"),
    ("传图定制", "传图定制", "业务词结尾不许削"),
    ("点赞 (列表页)", "点赞", "括号里的补充说明去掉"),
    ("AI创意  Tab", "ai创意", "大小写归一 + 去空格 + 去 Tab"),
    ("商品/详情页", "商品详情", "分隔符当噪声"),
    ("上传模块", "上传", "「模块」是结构性尾缀"),
]


def main() -> int:
    failed = []

    def check(cond, msg):
        print(f"   {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failed.append(msg)

    print("── 归一化：只削结构性尾缀，不动业务词")
    for raw, want, why in NORM_CASES:
        got = align.norm_name(raw)
        check(got == want, f"{raw!r} -> {got!r}（期望 {want!r}）· {why}")

    print("\n── 精确命中")
    al = align.Aligner(atlas_doc=ATLAS, lexicon=LEXICON)
    for text, want_path in [
        ("我的", "我的"),
        ("定制模版页", "我的 / 定制模版页"),
        ("定制模版", "我的 / 定制模版页"),
        ("定制模版页面", "我的 / 定制模版页"),
        ("本地上传提交", "我的 / 定制模版页 / 本地上传提交"),
    ]:
        m = al.match(text)
        check(
            m.how == "exact" and " / ".join(m.path) == want_path,
            f"{text!r} -> {m.how} {' / '.join(m.path) or '未命中'}（期望 exact {want_path}）",
        )

    print("\n── 模糊：错别字兜住，不同的东西不许合并")
    m = al.match("定制模板页")
    check(
        m.how == "fuzzy" and " / ".join(m.path) == "我的 / 定制模版页",
        f"「定制模板页」（模版写成模板）-> {m.how} {' / '.join(m.path) or '未命中'}，score={m.score}",
    )
    m = al.match("定制页")
    check(
        not m.hit,
        f"「定制页」不许并进「定制模版页」（术语表里是两个词）-> {m.how} {' / '.join(m.path) or '未命中'}",
    )
    m = al.match("创意定制")
    check(not m.hit, f"「创意定制」不许并进「传图定制」-> {m.how} {' / '.join(m.path) or '未命中'}")
    for text in ("完全不相干的东西", "登录", ""):
        m = al.match(text)
        check(not m.hit, f"{text!r} 认不出就返回 none，不硬猜 -> {m.how}")

    print("\n── 同名节点靠父节点消歧")
    community = A.find_module(ATLAS, name="帖子详情页")
    custom = A.find_module(ATLAS, name="定制模版页")
    m1 = al.match("点赞", parent_id=community["id"], prefer_kind="feature")
    m2 = al.match("点赞", parent_id=custom["id"], prefer_kind="feature")
    check(" / ".join(m1.path) == "社区 / 帖子详情页 / 点赞", f"社区下的点赞 -> {' / '.join(m1.path)}")
    check(" / ".join(m2.path) == "我的 / 定制模版页 / 点赞", f"定制页下的点赞 -> {' / '.join(m2.path)}")
    check(m1.target_id != m2.target_id, "两个「点赞」对齐到不同的功能 id")

    print("\n── 整条路径对齐")
    m = al.match_path(["社区", "帖子详情页", "点赞"], last_is_feature=True)
    check(m.how == "exact" and m.kind == "feature", f"整条命中 -> {m.how} {m.kind} {' / '.join(m.path)}")
    m = al.match_path(["社区", "不存在的页", "点赞"], last_is_feature=True)
    check(not m.hit, f"中间断了就不返回末级 -> {m.how}")

    print("\n── certain 的判定（决定能不能直接合并进图谱）")
    check(al.match("我的").certain, "exact 是确定的")
    check(not al.match("定制模板页").certain, "fuzzy 不是确定的，必须走人审")
    aliased = align.Aligner(
        atlas_doc=ATLAS,
        lexicon=LEXICON,
        aliases={align.norm_name("定制页"): custom["id"]},
    )
    m = aliased.match("定制页")
    check(
        m.how == "alias" and m.certain and " / ".join(m.path) == "我的 / 定制模版页",
        f"人审通过的别名直接命中 -> {m.how} {' / '.join(m.path) or '未命中'}",
    )

    print("\n── 空图谱不崩")
    empty = align.Aligner(atlas_doc=A.empty_atlas())
    check(not empty.has_nodes(), "空图谱 has_nodes=False")
    check(not empty.match("任何东西").hit, "空图谱对齐返回 none")

    print("\n" + ("=== 全部通过 ===" if not failed else f"=== {len(failed)} 条失败 ==="))
    for m in failed:
        print(f"  · {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
