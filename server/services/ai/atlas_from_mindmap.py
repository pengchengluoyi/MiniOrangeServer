# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""从一棵测试脑图反推应用图谱骨架（规则版，不调模型）。

四条边界，都是踩过的坑：

1. **跳过 root 和 platform 两层。** `app_atlas` 的数据模型里没有「端」这一层 ——
   `normalize_module` / `normalize_feature` 都不含该字段，端只活在需求的
   `understanding.impact.platforms` 和脑图第一层。照抄脑图层级会在图谱顶上凭空造出
   「App」「Web」「端到端」三个模块，每个下面再复制一套同名子树，正是
   `REQ_ANALYST_IMPACT_PROMPT` 明令禁止的事。前端 `summarizeMindDiff` 早就这么跳了。

2. **测试点不进图谱。** 图谱是产品结构，测试点是覆盖清单，几百个叶子灌进去图谱就废了。
   只把数量记到 `point_count` 上，让人看出哪块功能测得薄。

3. **图谱已有的形状优先。** 脑图里某个节点只挂着测试点、看着像「功能」，但图谱里它已经
   是个模块 —— 那就用模块，不要再造一个同名功能。少一次形状打架就少一次人工对账。

4. **回填的 `path` 是图谱路径，不是脑图路径。** 前端 `placeBranch` 拿 `path` 去
   `walkPath` 图谱树；写成脑图里的祖先链就对不上，脑图还是会整棵挂到根下。所以某个节点
   对齐到了图谱别处时，它和它的子树都改用图谱那边的路径。

「测试点」的判定必须和 `qa_role_jobs.collect_mindmap_points` 逐字一致，否则
`point_count` 和 `understanding.points` 会对不上账。`scripts/verify_mindmap_to_atlas.py`
拿脑图逐节点比对两边，防止哪天单方面改了判据。
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Optional

from server.services.ai import app_atlas as atlas
from server.services.ai import atlas_align as align

_SKIP_KINDS = ("root", "platform")
_STRUCT_KINDS = ("module", "feature")


def _kids(node: Any) -> list[dict]:
    if not isinstance(node, dict):
        return []
    return [c for c in (node.get("children") or []) if isinstance(c, dict)]


def _text(node: Any) -> str:
    if not isinstance(node, dict):
        return ""
    return str(node.get("text") or node.get("title") or "").strip()


def is_point(node: Any) -> bool:
    """测试点判定。**必须**和 qa_role_jobs.collect_mindmap_points 里的判据一致。"""
    if not isinstance(node, dict):
        return False
    text = _text(node)
    kind = str(node.get("kind") or "")
    return bool(text) and kind not in _SKIP_KINDS and (
        kind == "point" or (not _kids(node) and kind not in _STRUCT_KINDS)
    )


def is_structural(node: Any) -> bool:
    """结构节点 = 有名字、不是端/根、也不是测试点。只有它才可能成为图谱上的模块或功能。"""
    if not isinstance(node, dict):
        return False
    return bool(_text(node)) and str(node.get("kind") or "") not in _SKIP_KINDS and not is_point(node)


def count_points(node: Any) -> int:
    total = 0
    for kid in _kids(node):
        total += 1 if is_point(kid) else 0
        total += count_points(kid)
    return total


def _has_structural_kid(node: Any) -> bool:
    return any(is_structural(k) for k in _kids(node))


@dataclass
class Outcome:
    """反推结果。`atlas` 是合并后的骨架，`mindmap` 是回填了 path / atlas_ref 的树。"""

    atlas: dict = field(default_factory=dict)
    mindmap: dict = field(default_factory=dict)
    points: int = 0
    nodes: int = 0
    matched: list = field(default_factory=list)   # 确定命中（exact / alias），可直接合并
    created: list = field(default_factory=list)   # 图谱里没有，新建的节点
    review: list = field(default_factory=list)    # 模糊对齐或形状冲突，必须人确认
    platforms: list = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        """有新建节点或待确认的对齐就得走人审；全是确定命中才可以直接落库。"""
        return bool(self.created or self.review)


def infer(
    current: dict | None,
    mindmap: dict | None,
    *,
    req_id: str = "",
    package: str = "",
    aliases: dict | None = None,
    rejected: set | None = None,
    app_id: str = "",
) -> Outcome:
    from server.services.ai import app_profile as ap

    before = atlas.normalize_atlas(current)
    after = copy.deepcopy(before)
    aligner = align.aligner_for(
        before,
        package=package,
        aliases=aliases,
        rejected=rejected,
        app_id=app_id if aliases is None and rejected is None else "",
    )
    prof = ap.current(package)
    tree = copy.deepcopy(mindmap) if isinstance(mindmap, dict) else {}
    out = Outcome(atlas=after, mindmap=tree)
    if not tree:
        return out

    def annotate(node: dict, path: list[str], hit: Optional[align.Match] = None, kind: str = "", target_id: str = "") -> None:
        node["path"] = list(path)
        if target_id:
            node["atlas_ref"] = {
                "module_id": target_id if kind == "module" else "",
                "feature_id": target_id if kind == "feature" else "",
                "how": (hit.how if hit and hit.hit else "new"),
                "score": (hit.score if hit else 0),
            }
        else:
            node.pop("atlas_ref", None)

    def reuse(hit: align.Match) -> Optional[tuple[str, dict]]:
        """命中就在 after 里找回那个活节点。图谱已有的形状优先。

        fuzzy 也复用目标节点写进 after（人确认 = 认可这次合并），但调用方必须把它
        放进 review，绝不能当成 certain 直接落库。
        """
        if not hit.hit or hit.how == "none":
            return None
        if hit.how not in ("exact", "alias", "fuzzy"):
            return None
        if hit.kind == "module":
            mod = atlas.find_module(after, module_id=hit.target_id)
            return ("module", mod) if mod else None
        _mod, feat = atlas.find_feature(after, feature_id=hit.target_id)
        return ("feature", feat) if feat else None

    def bump(node: Optional[dict], count: int) -> None:
        if node is not None and count:
            node["point_count"] = _as_int(node.get("point_count")) + count

    def note_platform(node: dict) -> None:
        sid = prof.surface_of(_text(node), loose=False) or prof.surface_of(node.get("platform") or "")
        label = prof.surface_label(sid) if sid else ""
        if label and label not in out.platforms:
            out.platforms.append(label)

    def visit(node: dict, parent_mod: Optional[dict], mod_depth: int, path: list[str]) -> None:
        """安置 node 的孩子。node 自己已经被上一层处理过了。`path` 是 node 的图谱路径。"""
        direct_points = 0
        for kid in _kids(node):
            if is_point(kid):
                direct_points += 1
                annotate(kid, path + [_text(kid)])
                continue
            if not is_structural(kid):
                # 端 / 根 / 没名字的中间层：自己不落地，孩子照旧挂在当前父节点上
                note_platform(kid)
                annotate(kid, path)
                visit(kid, parent_mod, mod_depth, path)
                continue

            text = _text(kid)
            # 顶层的东西一律当模块 —— 图谱的功能必须挂在模块下，把顶层节点塞进合成的
            # 「业务功能」桶里只会让骨架变形。太深了也停止嵌套，改成功能。
            as_feature = bool(parent_mod) and (
                not _has_structural_kid(kid) or mod_depth >= atlas.MAX_DEPTH - 1
            )
            hit = aligner.match(
                text,
                parent_id=(parent_mod or {}).get("id") or "",
                prefer_kind="feature" if as_feature else "module",
            )
            row = {
                "text": text,
                "how": hit.how,
                "score": hit.score,
                "atlas_name": hit.name,
                "target_id": hit.target_id,
            }

            reused = reuse(hit)
            if reused:
                kind, target = reused
                # 图谱把它当叶子功能，脑图却在它下面还有结构。用图谱的形状（子结构按
                # 测试点计数），但必须让人看见这处形状分歧，不能悄悄吃掉。
                if kind == "feature" and _has_structural_kid(kid):
                    out.review.append({**row, "kind": kind, "note": "图谱里是叶子功能，脑图里还有下级结构"})
            elif as_feature:
                kind, target = "feature", atlas.ensure_feature(after, parent_mod, text)
            else:
                kind, target = "module", atlas.ensure_module(after, text, parent=parent_mod)

            here = list(hit.path) if reused and hit.path else path + [target["name"]]
            row = {**row, "kind": kind, "path": " / ".join(here), "target_id": target["id"]}
            if reused and hit.certain:
                out.matched.append(row)
            elif reused and hit.how == "fuzzy":
                # 合并进建议目标，但必须人确认；同时产出别名建议。
                out.review.append(row)
            else:
                out.created.append(row)
            if hit.how == "fuzzy" and not reused:
                out.review.append(row)

            out.nodes += 1
            annotate(kid, here, hit, kind, target["id"])
            if req_id:
                if kind == "feature":
                    atlas.hang_req(after, req_id, feature_id=target["id"])
                else:
                    atlas.hang_req(after, req_id, module_id=target["id"])

            if kind == "module":
                visit(kid, target, mod_depth + 1, here)
            else:
                # 功能是图谱的叶子，它下面的结构节点没地方放，整棵按测试点计数
                bump(target, count_points(kid))
                _annotate_subtree(kid, here)
        bump(parent_mod, direct_points)

    def _annotate_subtree(node: dict, path: list[str]) -> None:
        """功能底下不再落图谱节点，但 path 还要写，否则前端定位不到。"""
        for kid in _kids(node):
            here = path + [_text(kid)] if _text(kid) else list(path)
            annotate(kid, here)
            _annotate_subtree(kid, here)

    visit(tree, None, 0, [])
    out.points = count_points(tree)
    return out


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def summary_line(out: Outcome) -> str:
    bits = [f"{out.nodes} 个结构节点", f"{out.points} 个测试点"]
    if out.matched:
        bits.append(f"对上图谱 {len(out.matched)} 个")
    if out.created:
        bits.append(f"新增 {len(out.created)} 个")
    if out.review:
        bits.append(f"{len(out.review)} 个待确认")
    return "，".join(bits)


def alias_suggestions(out: Outcome) -> list[dict]:
    """从待确认项抽出别名建议：脑图原文 → 图谱目标。确认后写入 m_atlas_alias。"""
    seen = set()
    rows = []
    for item in out.review or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("how") or "") != "fuzzy":
            continue
        alias = str(item.get("text") or "").strip()
        tid = str(item.get("target_id") or "").strip()
        if not alias or not tid:
            continue
        key = (align.norm_name(alias), tid)
        if key in seen or not key[0]:
            continue
        seen.add(key)
        path = item.get("path")
        if isinstance(path, str):
            path_list = [p.strip() for p in path.split("/") if p.strip()]
        else:
            path_list = list(path or [])
        rows.append(
            {
                "alias": alias,
                "atlas_name": str(item.get("atlas_name") or ""),
                "target_id": tid,
                "target_kind": str(item.get("kind") or "module"),
                "path": path_list,
                "score": int(item.get("score") or 0),
                "how": "fuzzy",
            }
        )
    return rows


__all__ = [
    "Outcome",
    "alias_suggestions",
    "count_points",
    "infer",
    "is_point",
    "is_structural",
    "summary_line",
]
