# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""把一个名字对齐到应用图谱的某个节点。

谁需要它：
  导入的脑图里写「定制页」，图谱里叫「定制工具」，两边各写各的，谁也不认识谁 ——
  于是导进来的脑图在看板上整棵挂到根下，反推图谱时又会造一堆重名分支。用例的 module
  字段、飞书分区名同样是这个问题的实例。

分层（逐级降级，命中即停）：
  1. exact  归一化后完全相同。跨应用通用的归一化：全角转半角、去空格标点、去括号
            补充说明、去「页/页面/模块/功能/Tab」这类结构性尾缀。
  2. alias  别名表命中。**只有人审通过的别名才在这里**（P1 接 m_atlas_alias 表）。
  3. fuzzy  字符相似度 + 二元组 dice。只产出「建议」，绝不静默合并 —— 一次误判会被
            后续所有导入继承，越用越歪。
  4. none   认不出。交给模型或人，不猜。

术语表当**反向守卫**用：`定制页` 和 `定制模版页` 的 dice 相似度有 0.75，模糊匹配会把
它们当成一个东西；但应用画像的 lexicon 里这是两个各有释义的术语，所以禁止合并。
术语表能拦掉的正是最像、也最容易错的那一类。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional

from server.services.ai import app_atlas as atlas

# 只去掉「结构性」尾缀 —— 它们描述的是节点在信息架构里的角色，不是业务含义。
# 业务词尾缀（「定制」「上传」）绝不能进这个表，否则「传图定制」会被削成「传图」。
_STRUCT_SUFFIXES = (
    "页面",
    "页",
    "模块",
    "功能",
    "入口",
    "流程",
    "标签页",
    "tab",
    "板块",
    "专区",
)
# 去尾缀后至少要剩这么多字符，否则「首页」会被削成「首」。
_MIN_STEM = 2
# 模糊命中的门槛。调低会多出误判建议，调高会漏掉「模版/模板」这类错别字。
FUZZY_MIN = 0.72
# 一个名字包含另一个（「定制」vs「定制页下单」）时，短的那个至少要这么长才算候选。
# 前端 findLoose 用的是 4，这里放宽到 3 但只当 fuzzy 建议，仍需人确认。
_CONTAIN_MIN = 3

_PUNCT = re.compile(r"[\s\-_/\\|·、,，.。:：;；!！?？'\"“”‘’()（）\[\]【】<>《》]+")
_BRACKET = re.compile(r"[(（\[【][^)）\]】]{0,30}[)）\]】]")


def norm_name(text: Any) -> str:
    """归一化：全角转半角 → 去括号补充 → 去标点空格 → 小写 → 去结构性尾缀。"""
    raw = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not raw:
        return ""
    raw = _BRACKET.sub("", raw)
    low = _PUNCT.sub("", raw).lower()
    if not low:
        return ""
    changed = True
    while changed:
        changed = False
        for suffix in _STRUCT_SUFFIXES:
            if low.endswith(suffix) and len(low) - len(suffix) >= _MIN_STEM:
                low = low[: -len(suffix)]
                changed = True
                break
    return low


def _bigrams(text: str) -> set[str]:
    return {text[i : i + 2] for i in range(len(text) - 1)}


def _dice(a: str, b: str) -> float:
    """二元组 dice。中文没有词边界，字符二元组比整串编辑距离更贴近「像不像」。"""
    if len(a) < 2 or len(b) < 2:
        return 1.0 if a == b else 0.0
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return 2 * len(ba & bb) / (len(ba) + len(bb))


def similarity(a: str, b: str) -> float:
    """0~1。取字符相似度和二元组 dice 的较大值；包含关系单独给一档。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    best = max(SequenceMatcher(None, a, b).ratio(), _dice(a, b))
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= _CONTAIN_MIN and short in long_:
        best = max(best, 0.82)
    return best


@dataclass(frozen=True)
class Match:
    """一次对齐的结果。`how` 决定它能不能直接合并进图谱。"""

    how: str = "none"  # exact | alias | fuzzy | none
    kind: str = ""  # module | feature
    target_id: str = ""
    path: tuple[str, ...] = ()
    score: int = 0  # 0~100
    name: str = ""  # 命中的图谱节点原名，用来提别名建议

    @property
    def hit(self) -> bool:
        return bool(self.target_id)

    @property
    def certain(self) -> bool:
        """确定的对齐可以直接合并；fuzzy 必须走人审。"""
        return self.how in ("exact", "alias")

    @property
    def module_id(self) -> str:
        return self.target_id if self.kind == "module" else ""

    @property
    def feature_id(self) -> str:
        return self.target_id if self.kind == "feature" else ""


NO_MATCH = Match()


@dataclass
class Aligner:
    """按一份图谱快照建索引，然后反复对齐名字。图谱变了就重新建一个。"""

    atlas_doc: dict = field(default_factory=dict)
    # 已审通过的别名：归一化后的名字 -> 图谱节点 id。P1 从 m_atlas_alias 表灌进来。
    aliases: dict = field(default_factory=dict)
    # 驳回过的 (alias_norm, target_id)，模糊匹配不许再提同一对。
    rejected: set = field(default_factory=set)
    # 应用术语表（app_profile.UiProfile.lexicon）。只用来拦模糊误判，不用来匹配。
    lexicon: dict = field(default_factory=dict)
    threshold: float = FUZZY_MIN

    _rows: list = field(default_factory=list, init=False, repr=False)
    _by_norm: dict = field(default_factory=dict, init=False, repr=False)
    _by_id: dict = field(default_factory=dict, init=False, repr=False)
    _terms: set = field(default_factory=set, init=False, repr=False)

    def __post_init__(self) -> None:
        for row in atlas.flatten_tree(self.atlas_doc):
            item = {
                "id": row["id"],
                "kind": row["kind"],
                "name": row["name"],
                "norm": norm_name(row["name"]),
                "path": tuple(p.strip() for p in str(row.get("path") or "").split(" / ") if p.strip()),
                "depth": int(row.get("depth") or 0),
                "parent_id": str(row.get("parent_id") or ""),
                "module_id": str(row.get("module_id") or ""),
            }
            self._rows.append(item)
            self._by_id[item["id"]] = item
            if item["norm"]:
                self._by_norm.setdefault(item["norm"], []).append(item)
        self._terms = {norm_name(t) for t in (self.lexicon or {}) if norm_name(t)}
        # dataclass 默认可能传入 list；统一成 set
        self.rejected = {(str(a), str(b)) for a, b in (self.rejected or set())}
    # ── 查询 ────────────────────────────────────────────────
    @property
    def rows(self) -> list:
        return list(self._rows)

    def has_nodes(self) -> bool:
        return bool(self._rows)

    def _pick(self, cands: Iterable[dict], *, parent_id: str, prefer_kind: str) -> Optional[dict]:
        """同名节点可能有多个。优先同一个父节点下的，其次是想要的类型，最后取最浅的。"""
        rows = list(cands)
        if not rows:
            return None
        if parent_id:
            scoped = [r for r in rows if parent_id in (r["parent_id"], r["module_id"])]
            if scoped:
                rows = scoped
        if prefer_kind:
            typed = [r for r in rows if r["kind"] == prefer_kind]
            if typed:
                rows = typed
        return sorted(rows, key=lambda r: (r["depth"], r["path"]))[0]

    def _as_match(self, row: dict, *, how: str, score: int) -> Match:
        return Match(
            how=how,
            kind=row["kind"],
            target_id=row["id"],
            path=row["path"],
            score=score,
            name=row["name"],
        )

    def _blocked_by_lexicon(self, norm: str, other: str) -> bool:
        """两边都是术语表里的词、且不是同一个词 → 它们是两个不同的东西，不许模糊合并。"""
        return norm != other and norm in self._terms and other in self._terms

    def match(self, text: Any, *, parent_id: str = "", prefer_kind: str = "") -> Match:
        norm = norm_name(text)
        if not norm or not self._rows:
            return NO_MATCH

        row = self._pick(self._by_norm.get(norm) or [], parent_id=parent_id, prefer_kind=prefer_kind)
        if row:
            return self._as_match(row, how="exact", score=100)

        target_id = str((self.aliases or {}).get(norm) or "")
        row = self._by_id.get(target_id)
        if row:
            return self._as_match(row, how="alias", score=100)

        best: Optional[dict] = None
        best_score = 0.0
        for cand in self._rows:
            if not cand["norm"] or self._blocked_by_lexicon(norm, cand["norm"]):
                continue
            if (norm, cand["id"]) in self.rejected:
                continue
            score = similarity(norm, cand["norm"])
            if score < self.threshold:
                continue
            # 同分时偏向同父节点、偏向想要的类型，避免结果随字典顺序抖动
            bonus = 0.0
            if parent_id and parent_id in (cand["parent_id"], cand["module_id"]):
                bonus += 0.03
            if prefer_kind and cand["kind"] == prefer_kind:
                bonus += 0.01
            if score + bonus > best_score:
                best, best_score = cand, score + bonus
        if best:
            return self._as_match(best, how="fuzzy", score=int(round(min(best_score, 0.99) * 100)))
        return NO_MATCH
    def match_path(self, names: Iterable[Any], *, last_is_feature: bool = False) -> Match:
        """按祖先链逐级对齐，返回**最后一级**的结果。上一级命中就用它约束下一级。

        中间任何一级断了就是 NO_MATCH。这里不返回「已经对上的最深祖先」—— 调用方问的是
        末级落在哪，拿到一个祖先只会把东西挂错层。要建缺失的中间层是 `ensure_path` 的事。
        """
        parent_id = ""
        result = NO_MATCH
        parts = [x for x in (names or []) if str(x or "").strip()]
        for idx, name in enumerate(parts):
            last = idx == len(parts) - 1
            hit = self.match(
                name,
                parent_id=parent_id,
                prefer_kind="feature" if (last and last_is_feature) else "module",
            )
            if not hit.hit:
                return NO_MATCH
            result = hit
            parent_id = hit.target_id
        return result

    def path_of(self, target_id: str) -> tuple[str, ...]:
        row = self._by_id.get(str(target_id or ""))
        return row["path"] if row else ()


def aligner_for(
    atlas_doc: dict,
    *,
    package: str = "",
    aliases: dict | None = None,
    rejected: set | None = None,
    app_id: str = "",
) -> Aligner:
    """按图谱 + 应用画像建对齐器。术语表从画像取；别名表可显式传入，或按 app_id 从表加载。"""
    from server.services.ai import app_profile as ap

    alias_map = dict(aliases or {})
    rejected_set = set(rejected or set())
    if app_id and aliases is None and rejected is None:
        try:
            from server.services.ai import atlas_alias_repo as alias_repo

            loaded, blocked = alias_repo.load_for_aligner(app_id)
            alias_map = loaded
            rejected_set = blocked
        except Exception:
            # 表未建 / DB 不可用时降级成无别名，对齐仍可用
            pass
    return Aligner(
        atlas_doc=atlas_doc or {},
        aliases=alias_map,
        rejected=rejected_set,
        lexicon=dict(ap.current(package).lexicon or {}),
    )


__all__ = [
    "Aligner",
    "Match",
    "NO_MATCH",
    "FUZZY_MIN",
    "aligner_for",
    "norm_name",
    "similarity",
]
