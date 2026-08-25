#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验别名学习：确认 → 下次 alias 命中；驳回 → 不再模糊建议同一对。

用内存 SQLite，不碰真实库。
"""
from __future__ import annotations

import contextlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from server.core.database import Base
from server.models.atlas_alias import MAtlasAlias  # noqa: F401
from server.services.ai import app_atlas as A  # noqa: E402
from server.services.ai import atlas_align as align  # noqa: E402
from server.services.ai import atlas_alias_repo as repo  # noqa: E402
from server.services.ai import atlas_from_mindmap as afm  # noqa: E402


ATLAS = A.normalize_atlas(
    {
        "modules": [
            {
                "name": "我的",
                "children": [
                    {
                        "name": "定制模版页",
                        "features": [{"name": "本地上传提交"}, {"name": "对话生成"}],
                    }
                ],
            }
        ]
    }
)


def main() -> int:
    failed = []

    def check(cond, msg):
        print(f"   {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failed.append(msg)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    @contextlib.contextmanager
    def scope():
        db = Session()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    repo.session_scope = scope

    print("── 模糊建议进入 patch.aliases，确认后 alias 命中")
    feat_id = ""
    for row in A.flatten_tree(ATLAS):
        if row["name"] == "本地上传提交":
            feat_id = row["id"]
            break
    check(bool(feat_id), f"找到功能 id：{feat_id}")

    # 「本地上传」和「本地上传提交」相似度够模糊门槛；「图片上传」不够。
    mind = {
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
                            {
                                "text": "定制模版页",
                                "kind": "module",
                                "children": [
                                    {
                                        "text": "本地上传",
                                        "kind": "feature",
                                        "children": [
                                            {"text": "选择本地图片后可提交", "kind": "point"},
                                            {"text": "超过 10MB 提示过大", "kind": "point"},
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

    out = afm.infer(ATLAS, mind, package="")
    suggestions = afm.alias_suggestions(out)
    check(out.needs_review, "模糊对齐需要人审")
    check(any(s.get("alias") == "本地上传" for s in suggestions), f"别名建议：{suggestions}")
    names = [r["name"] for r in A.flatten_tree(out.atlas)]
    check(names.count("本地上传") == 0, "after 里没有「本地上传」新节点（合并进建议目标）")
    check("本地上传提交" in names, "after 仍是图谱原名")

    with Session() as db:
        for s in suggestions:
            repo.upsert(
                db,
                app_id="app-1",
                alias=s["alias"],
                target_id=s["target_id"],
                target_kind=s.get("target_kind") or "feature",
                target_path=s.get("path") or [],
                review_status="approved",
                score=int(s.get("score") or 0),
                bump_hit=True,
            )
        db.commit()
        approved = repo.approved_map(db, "app-1")

    aligner = align.Aligner(atlas_doc=ATLAS, aliases=approved)
    hit = aligner.match("本地上传")
    check(hit.how == "alias" and hit.certain, f"确认后 how=alias（实际 {hit.how}）")
    check(hit.target_id == feat_id, f"指向同一功能（{hit.target_id}）")

    out2 = afm.infer(ATLAS, mind, package="", aliases=approved)
    check(not out2.needs_review, "有别名后可直接合并，不再进人审")
    check(any(m.get("how") == "alias" for m in out2.matched), f"matched 含 alias：{out2.matched}")

    print("\n── 驳回后模糊不再提同一对")
    with Session() as db:
        repo.upsert(
            db,
            app_id="app-2",
            alias="本地上传",
            target_id=feat_id,
            target_kind="feature",
            review_status="rejected",
            score=80,
        )
        db.commit()
        blocked = repo.rejected_pairs(db, "app-2")

    aligner2 = align.Aligner(atlas_doc=ATLAS, rejected=blocked)
    hit2 = aligner2.match("本地上传")
    check(
        hit2.how == "none" or hit2.target_id != feat_id,
        f"驳回后不再建议该对（how={hit2.how} id={hit2.target_id}）",
    )

    print("\n── normalize_patch 保留 aliases")
    patches = []
    A.enqueue_patch(
        patches,
        before=ATLAS,
        after=out.atlas,
        reason="test",
        aliases=suggestions,
        force=True,
    )
    check(bool(patches), "enqueue 成功")
    norm = A.normalize_patch(patches[0])
    check(len(norm.get("aliases") or []) == len(suggestions), "normalize 不丢 aliases")

    print("\n── 截断一致性：25 字模块名")
    long_name = "这是一个超过二十个字的模块名称用来测试"
    clipped = long_name[:20]
    with Session() as db:
        repo.upsert(db, app_id="app-3", alias=long_name, target_id="mod-x", review_status="approved")
        db.commit()
        m = repo.approved_map(db, "app-3")
    check(align.norm_name(clipped) in m, f"按截断后文本能命中（keys={list(m)}）")

    print("\n" + ("=== 全部通过 ===" if not failed else f"=== {len(failed)} 条失败 ==="))
    for msg in failed:
        print(f"  · {msg}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
