# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""应用 UI 画像（ui_profile）：把「这个应用的底栏叫什么、怎么算已登录」这类事实
从通用服务的代码里搬出来，变成按应用取的数据。

为什么需要：
  case_precondition / page_context / page_navigation / copilot 这些**名字通用、实现专用**
  的服务里写死了单个被测应用的 tab 文案。换一个应用不是「效果变差」，是判定错了还告诉你
  判定对了 —— 例如 _main_tab_bar_logged_in 要求命中 3 个特定 tab，换应用永远返回 False，
  「已登录」前置检查永远失败，用例全部阻塞。

分层：
  1. 内置默认 `_default`：不含任何业务词，只有跨应用成立的通用信号
  2. 应用基础逻辑：存在 App 库里的 playbook，由 bind(override=..., playbook=...) 注入
  3. 遗留 YAML 只作空库导入种子，运行期不再按包名读文件

深层调用点拿不到 App 对象，所以用 contextvar 绑定（和 dispatch_log.bind 同一套做法）：
  tok = app_profile.bind(package="com.example.app")
  try: ...
  finally: app_profile.reset(tok)
运行期必须 `bind(override=..., playbook=...)`；`for_package` 不再按磁盘 YAML 找应用。
"""
from __future__ import annotations

import contextvars
import re
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Optional

_CTX: contextvars.ContextVar[dict] = contextvars.ContextVar("ui_profile_ctx", default={})
_LOCK = threading.Lock()
_CACHE: dict[str, "UiProfile"] = {}
_LOADED = False

# ── 通用信号：跨应用成立，**不含任何业务词**。应用画像是在这些之上「增量」，不是替换。 ──
GENERIC_LOGIN_MARKERS = (
    "一键登录",
    "本机号码",
    "验证码登录",
    "密码登录",
    "手机号登录",
    "短信登录",
    "访客浏览",
    "登录中",
    "正在登录",
)
GENERIC_HOME_MARKERS = ("推荐", "关注", "发现", "Feed", "feed")
# 只放「够强、可以不带长度守卫就判定协议全文页」的通用词。
# 泛词「用户协议」不在这里 —— 它在登录页也会出现，必须配合正文长度判断，
# 那些判断留在各自调用点（它们本来就是通用逻辑）。
GENERIC_LEGAL_MARKERS = ("平台用户协议",)
GENERIC_FOREGROUND_MARKERS = (
    "同意并继续",
    "隐私条款",
    "用户协议",
    "隐私政策",
    "访客浏览",
    "一键登录",
    "手机号登录",
)
# 太通用、不足以指示「代码和某个应用耦合」的词。verify_no_app_literals.py 扫描时跳过它们，
# 否则通用服务里合法出现的「首页」「我的」会被误报。
GENERIC_NAV_WORDS = (
    "首页",
    "我的",
    "消息",
    "想要",
    "设置",
    "搜索",
    "发现",
    "关注",
    "推荐",
    "登录",
    "注册",
)
# 几乎所有中文 App 都有的主导航文案。原来的硬编码 tab 清单里就混着这几个词，
# 于是**未接入画像的应用也能靠它们偶然识别出首页**。搬家不能把这个既有能力弄丢，
# 所以它们并入每个应用的 tab 清单（对已接入的应用是子集，集合不变）。
GENERIC_TAB_LABELS = ("首页", "消息", "我的")

# 端（surface）：(id, 展示名, 识别别名)。这些是产业通用词，和 GENERIC_NAV_WORDS 同一层级 ——
# 「运营平台」「后台」「CMS」不是某个应用的私有叫法，所以留在代码里，不算业务字面量。
# 应用只声明「我有哪几个端」，以及非标准端（小程序 / 桌面端 / 开放 API）的名字和别名。
GENERIC_SURFACES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("app", "App", ("app", "android", "ios", "mobile", "客户端", "移动端", "手机端", "无线端")),
    ("web", "Web", ("web", "h5", "pc", "admin", "cms", "ops", "console", "后台", "管理后台", "运营", "运营平台", "控制台")),
    ("e2e", "端到端", ("e2e", "端到端", "全链路")),
)
# 端到端不是一个真实的端，是「跨端串起来测」，所以它永远排最后，也不参与「有几个端」的计数。
E2E_SURFACE = "e2e"
# 没声明端的应用按这两个端拆（搬家前 _mindmap_platforms 的写死默认，保持不变）。
DEFAULT_SURFACES = ("app", "web")

# 内置默认画像的 key。判断「这个应用有没有接入画像」一律比它，别到处写字符串。
DEFAULT_KEY = "_default"


@dataclass(frozen=True)
class UiProfile:
    """一个被测应用的 UI 事实。所有字段都必须能在没有它时降级，不能假设存在。"""

    key: str = "_default"
    label: str = ""
    packages: tuple[str, ...] = ()

    # ── 导航 ────────────────────────────────────────────────
    bottom_tabs: tuple[str, ...] = ()          # 底部主导航（导航目标）
    segment_tabs: tuple[str, ...] = ()         # 顶部分段 tab（导航目标）
    segment_tab_aliases: tuple[str, ...] = ()  # 分段 tab 的额外别名（只用于识别，不作导航目标）
    home_markers: tuple[str, ...] = ()         # 首页的应用专属特征（通用的见 GENERIC_HOME_MARKERS）

    # ── 登录态 ──────────────────────────────────────────────
    # 「底栏主导航齐全 ⇒ 已登录」这条信号用的 tab 清单。和 bottom_tabs 可能不同：
    # 有些应用的首页 chrome 里混着分段 tab。为空时沿用 bottom_tabs。
    logged_in_tabs: tuple[str, ...] = ()
    logged_in_tab_hits: int = 3                # 命中几个算已登录
    logged_in_pages: tuple[str, ...] = ()      # 处于这些页面视为已登录
    login_page_markers: tuple[str, ...] = ()   # 应用专属登录页特征

    # ── 协议 / 前台判定 ─────────────────────────────────────
    legal_page_markers: tuple[str, ...] = ()   # 协议全文页的应用专属文案
    foreground_markers: tuple[str, ...] = ()   # 「应用已在前台」的应用专属文案

    # ── 端 ──────────────────────────────────────────────────
    # 这个应用实际有哪几个端，如 ("app", "web")。空 = 未声明，调用方退化到 DEFAULT_SURFACES。
    surfaces: tuple[str, ...] = ()
    # 非标准端 / 额外别名：sid -> {"label": str, "aliases": tuple}。在 GENERIC_SURFACES 之上增量。
    surface_extra: dict = field(default_factory=dict)

    # ── 术语表 ──────────────────────────────────────────────
    # term -> {"means": str, "archetype": str}
    lexicon: dict = field(default_factory=dict)

    # ── 定位覆写 ────────────────────────────────────────────
    # label_key -> {clip_query, clip_aliases, ocr_queries, region, icon_row}
    # 覆盖 clip_query_plan.COMMON_LOGIN_CHAIN 里同 key 的通用项；缺字段沿用通用值。
    clip_plans: dict = field(default_factory=dict)

    # ── 壳层（执行期喂给 decide/assert，不是登录结论）────────
    center_action: str = ""                    # 底栏正中独立入口，如「绿色加号是创作，不是首页」
    chrome_notes: tuple[str, ...] = ()         # 额外壳层说明（选中态、槽位顺序）

    # ── 生效值（通用 + 应用增量）────────────────────────────
    def login_markers(self) -> tuple[str, ...]:
        return _uniq(GENERIC_LOGIN_MARKERS + self.login_page_markers)

    def home_marker_labels(self) -> tuple[str, ...]:
        return _uniq(GENERIC_HOME_MARKERS + self.home_markers)

    def legal_markers(self) -> tuple[str, ...]:
        return _uniq(GENERIC_LEGAL_MARKERS + self.legal_page_markers)

    def foreground_marker_labels(self) -> tuple[str, ...]:
        return _uniq(GENERIC_FOREGROUND_MARKERS + self.foreground_markers)

    def brand_markers(self) -> tuple[str, ...]:
        """只有这个应用自己的品牌 / 欢迎语（不含通用词）。

        用于「当屏出现的是本应用的品牌」这类判断 —— 例如冷启动隐私弹窗＝品牌文案 + 协议文案。
        通用词不能进来，否则任何带「用户协议」的页面都会被当成品牌页。
        """
        return _uniq(self.foreground_markers)

    def home_tabs(self) -> tuple[str, ...]:
        """首页 chrome 上会出现的 tab 文案（通用主导航 + 本应用底栏 + 分段）。"""
        return _uniq(GENERIC_TAB_LABELS + self.bottom_tabs + self.segment_tabs)

    def login_signal_tabs(self) -> tuple[str, ...]:
        return _uniq(GENERIC_TAB_LABELS + (self.logged_in_tabs or self.bottom_tabs))

    def segment_tab_names(self) -> tuple[str, ...]:
        return _uniq(self.segment_tabs + self.segment_tab_aliases)

    def has_nav(self) -> bool:
        return bool(self.bottom_tabs or self.segment_tabs)

    def archetype_of(self, term: str) -> str:
        row = (self.lexicon or {}).get(str(term or "").strip()) or {}
        return str(row.get("archetype") or "")

    # ── 端（通用 + 应用增量）────────────────────────────────
    def _surface_rows(self) -> list[tuple[str, str, tuple[str, ...]]]:
        """(id, label, aliases) 全集，端到端排最后。应用增量叠加在通用项上。"""
        rows: list[tuple[str, str, tuple[str, ...]]] = []
        seen: set[str] = set()
        for sid, label, aliases in GENERIC_SURFACES:
            extra = (self.surface_extra or {}).get(sid) or {}
            rows.append(
                (
                    sid,
                    str(extra.get("label") or label),
                    _uniq(tuple(aliases) + tuple(extra.get("aliases") or ())),
                )
            )
            seen.add(sid)
        for sid, extra in (self.surface_extra or {}).items():
            if sid in seen or not str(sid).strip():
                continue
            label = str((extra or {}).get("label") or sid)
            rows.append((str(sid), label, _uniq(tuple((extra or {}).get("aliases") or ()) + (label,))))
        rows.sort(key=lambda row: row[0] == E2E_SURFACE)
        return rows

    def surface_options(self) -> tuple[tuple[str, str], ...]:
        """(id, 展示名) 全集，端到端排最后。替代写死的 MINDMAP_PLATFORM_LABELS。"""
        return tuple((sid, label) for sid, label, _aliases in self._surface_rows())

    def surface_label(self, sid: str) -> str:
        key = str(sid or "").strip()
        for cur, label, _aliases in self._surface_rows():
            if cur == key:
                return label
        return key

    def declared_surfaces(self) -> tuple[str, ...]:
        """应用声明的端；没声明就是通用默认。端到端由调用方按「有几个端」推导，不在这里。"""
        known = {sid for sid, _label, _aliases in self._surface_rows()}
        declared = _uniq(tuple(x for x in (self.surfaces or ()) if str(x).strip() in known))
        return declared or DEFAULT_SURFACES

    def surface_of(self, text: str, *, loose: bool = True) -> str:
        """文案 → 端 id。认不出返回空串（**不猜**，让调用方决定怎么兜底）。

        loose=False 只认「整段就是端名」。判断一个已知是端的字段用 loose=True；
        判断一个可能是模块名的节点用 loose=False，否则「Web 端分享入口」会被当成 Web 这个端。
        """
        raw = str(text or "").strip()
        if not raw:
            return ""
        low = raw.lower()
        rows = self._surface_rows()
        for sid, label, aliases in rows:
            if low == str(label).strip().lower() or any(low == str(a).strip().lower() for a in aliases):
                return sid
        if not loose:
            return ""
        # 端名出现在更长的文案里（「App 端」「运营平台配置」）。取最长命中，
        # 否则 web 会从 webview 里抢走一票。
        best_sid, best_len = "", 0
        for sid, _label, aliases in rows:
            for alias in aliases:
                a = str(alias).strip().lower()
                if len(a) <= best_len or not _alias_hit(a, low):
                    continue
                best_sid, best_len = sid, len(a)
        return best_sid

    # ── 喂给模型的应用事实 ────────────────────────────────
    def facts_prompt(self) -> str:
        """这个应用的事实，拼成一段喂给模型。没有画像时返回空串。

        为什么必须有这个：把应用事实从 `roles_catalog` 的 prompt 里摘走，通用化才成立；
        但摘走之后如果不从画像补回来，就等于**把应用知识删了**——模型不再知道
        「传图定制」和「创意定制」是两条要分开测的路径，只会当成两个近义词合并掉。
        术语表原本只有 `archetype_of` 一个消费点（而且没人调），价值一直没兑现。

        内容必须逐字节稳定：它走 `_ask_json` 的 stable 分片，靠前缀缓存摊掉 token 成本。
        """
        if not self.has_facts():
            return ""
        lines = [f"## 被测应用\n{self.label or self.key}（端：{'、'.join(self.surface_label(s) for s in self.declared_surfaces())}）"]
        nav = _uniq(tuple(self.bottom_tabs) + tuple(self.segment_tabs))
        if nav:
            lines.append(f"主导航：{'、'.join(nav)}")
        if self.lexicon:
            rows = ["## 业务术语", "这些是本应用的既有叫法。写模块名、路径、用例时直接用，不要另造同义词，也不要把不同的术语合并成一个。"]
            for term, row in self.lexicon.items():
                means = str((row or {}).get("means") or "").strip()
                arch = str((row or {}).get("archetype") or "").strip()
                tag = f"（页面类型：{arch}）" if arch else ""
                rows.append(f"- {term}{tag}：{means}" if means else f"- {term}{tag}")
            lines.append("\n".join(rows))
        return "\n\n".join(lines)

    def chrome_prompt(self) -> str:
        """执行期壳层：底栏 / 顶部分段 / 选中态。没有导航时返回空，不冒充某个应用。

        和 facts_prompt 分开：写作用例走术语表；看图决策和校验走壳层。
        内容必须逐字节稳定。
        """
        if not (self.bottom_tabs or self.segment_tabs or self.center_action or self.chrome_notes):
            return ""
        head = f"【本应用壳层 · {self.label}】" if self.label else "【本应用壳层】"
        lines = [head]
        if self.bottom_tabs:
            lines.append("底栏导航目标（点某一项必须点带该文案的槽）：" + "、".join(self.bottom_tabs))
        if self.segment_tabs:
            lines.append("顶部内容分段（不是底栏）：" + "、".join(self.segment_tabs))
        lines.append("选中态只看该槽自己的填色、下划线或加粗。独立入口是否算导航项，以本应用知识为准。")
        if str(self.center_action or "").strip():
            lines.append(str(self.center_action).strip())
        for note in self.chrome_notes or ():
            n = str(note).strip()
            if n:
                lines.append(n)
        return "\n".join(lines)

    def has_facts(self) -> bool:
        """有没有值得喂给模型的应用事实。未接入的应用一律返回 False —— 宁可不说，
        也不能拿通用默认冒充某个应用的事实。"""
        return bool(self.key and self.key != DEFAULT_KEY and (self.lexicon or self.bottom_tabs or self.segment_tabs))

    def all_literals(self) -> list[str]:
        """这个应用的全部业务字面量。verify_no_app_literals.py 用它扫通用服务。"""
        generic = set(
            GENERIC_LOGIN_MARKERS
            + GENERIC_HOME_MARKERS
            + GENERIC_LEGAL_MARKERS
            + GENERIC_FOREGROUND_MARKERS
            + GENERIC_NAV_WORDS
        )
        # 通用端名（App / Web / 运营平台 / 后台…）是产业词，允许出现在通用服务里
        generic |= {str(a).strip() for _sid, _label, aliases in GENERIC_SURFACES for a in aliases}
        generic |= {str(label).strip() for _sid, label, _aliases in GENERIC_SURFACES}
        out: list[str] = []
        for seq in (
            self.bottom_tabs,
            self.segment_tabs,
            self.segment_tab_aliases,
            self.home_markers,
            self.logged_in_tabs,
            self.logged_in_pages,
            self.login_page_markers,
            self.legal_page_markers,
            self.foreground_markers,
        ):
            out.extend(str(x).strip() for x in (seq or ()) if str(x).strip())
        out.extend(str(k).strip() for k in (self.lexicon or {}) if str(k).strip())
        # 应用自己补的端名和别名（「造物工坊端」）是私有的；上面的 generic 会把产业词滤掉。
        for sid, extra in (self.surface_extra or {}).items():
            out.append(str((extra or {}).get("label") or sid).strip())
            out.extend(str(x).strip() for x in ((extra or {}).get("aliases") or ()) if str(x).strip())
        if self.label:
            out.append(self.label)
        # 通用词（首页 / 我的 / 用户协议…）不算业务字面量，允许出现在通用服务里
        return sorted({x for x in out if x not in generic})


def _uniq(seq) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(x).strip() for x in (seq or ()) if str(x).strip()))


_ASCII_ALIAS = re.compile(r"^[a-z0-9]+$")


def _alias_hit(alias: str, low_text: str) -> bool:
    """别名是否出现在文案里。拉丁别名要求词边界，否则 web 会命中 webview、app 会命中 happy。"""
    if not alias or not low_text:
        return False
    if _ASCII_ALIAS.match(alias):
        return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", low_text) is not None
    return alias in low_text


# 通用默认：**不许**出现任何应用业务词。只保留跨应用成立的信号。
DEFAULT = UiProfile(key=DEFAULT_KEY, label="")


def _profile_from_mapping(row: dict[str, Any]) -> UiProfile:
    def tup(key: str) -> tuple[str, ...]:
        return tuple(str(x).strip() for x in (row.get(key) or []) if str(x).strip())

    lex: dict[str, dict] = {}
    raw_lex = row.get("lexicon")
    if isinstance(raw_lex, list):
        for item in raw_lex:
            if not isinstance(item, dict):
                continue
            term = str(item.get("term") or "").strip()
            if term:
                lex[term] = {
                    "means": str(item.get("means") or ""),
                    "archetype": str(item.get("archetype") or ""),
                }
    elif isinstance(raw_lex, dict):
        for term, val in raw_lex.items():
            if not str(term).strip():
                continue
            if isinstance(val, dict):
                lex[str(term).strip()] = {
                    "means": str(val.get("means") or ""),
                    "archetype": str(val.get("archetype") or ""),
                }
            else:
                lex[str(term).strip()] = {"means": str(val or ""), "archetype": ""}

    # surfaces 支持两种写法混排：字符串（"web"，只声明有这个端）和对象
    # （{id, label?, aliases?}，顺带补别名或定义非标准端）。
    surfaces: list[str] = []
    surface_extra: dict[str, dict] = {}
    raw_surfaces = row.get("surfaces")
    if isinstance(raw_surfaces, dict):
        raw_surfaces = [{"id": k, **(v if isinstance(v, dict) else {})} for k, v in raw_surfaces.items()]
    for item in raw_surfaces or []:
        if isinstance(item, str):
            sid = item.strip()
            if sid:
                surfaces.append(sid)
            continue
        if not isinstance(item, dict):
            continue
        sid = str(item.get("id") or item.get("key") or "").strip()
        if not sid:
            continue
        surfaces.append(sid)
        extra = {}
        if str(item.get("label") or "").strip():
            extra["label"] = str(item["label"]).strip()
        aliases = tuple(str(x).strip() for x in (item.get("aliases") or []) if str(x).strip())
        if aliases:
            extra["aliases"] = aliases
        if extra:
            surface_extra[sid] = extra
    # 已解析好的形态（merge_override 回灌时用），YAML 里一般不写这个 key
    for sid, extra in (row.get("surface_extra") or {}).items():
        if isinstance(extra, dict) and str(sid).strip():
            surface_extra.setdefault(str(sid).strip(), extra)

    hits = row.get("logged_in_tab_hits")
    return UiProfile(
        key=str(row.get("key") or "").strip() or "_default",
        label=str(row.get("label") or row.get("name") or "").strip(),
        packages=tup("packages"),
        bottom_tabs=tup("bottom_tabs"),
        segment_tabs=tup("segment_tabs"),
        segment_tab_aliases=tup("segment_tab_aliases"),
        home_markers=tup("home_markers"),
        logged_in_tabs=tup("logged_in_tabs"),
        logged_in_tab_hits=int(hits) if str(hits or "").strip().isdigit() else DEFAULT.logged_in_tab_hits,
        logged_in_pages=tup("logged_in_pages"),
        login_page_markers=tup("login_page_markers"),
        legal_page_markers=tup("legal_page_markers"),
        foreground_markers=tup("foreground_markers"),
        surfaces=_uniq(surfaces),
        surface_extra=surface_extra,
        lexicon=lex,
        clip_plans={
            str(k): v for k, v in (row.get("clip_plans") or {}).items() if isinstance(v, dict)
        },
        center_action=str(row.get("center_action") or "").strip(),
        chrome_notes=tup("chrome_notes"),
    )


def _load_all() -> dict[str, UiProfile]:
    """运行期画像不再从 YAML 按包名加载。应用事实来自库里的 playbook。"""
    global _LOADED
    with _LOCK:
        if _LOADED:
            return _CACHE
        _CACHE.clear()
        _CACHE["_default"] = DEFAULT
        _LOADED = True
        return _CACHE


def reload_profiles() -> None:
    global _LOADED
    with _LOCK:
        _LOADED = False
    _load_all()


def list_profiles() -> list[UiProfile]:
    return [p for k, p in sorted(_load_all().items()) if k != "_default"]


def for_package(package: str) -> UiProfile:
    """按包名找画像；找不到返回通用默认（**不是**某个具体应用的画像）。"""
    pkg = str(package or "").strip().lower()
    if not pkg:
        return DEFAULT
    for prof in _load_all().values():
        if any(pkg == str(p).strip().lower() for p in prof.packages):
            return prof
    return DEFAULT


def for_key(key: str) -> UiProfile:
    return _load_all().get(str(key or "").strip()) or DEFAULT


def merge_override(base: UiProfile, override: Optional[dict]) -> UiProfile:
    """把 App.automation.ui_profile 的覆写叠加到 YAML 种子上。

    覆写只支持「整字段替换」；空值 / 缺字段一律沿用底稿，避免人删一行就把内置行为清空。
    """
    if not isinstance(override, dict) or not override:
        return base
    row = {
        "key": base.key,
        "label": base.label,
        "packages": list(base.packages),
        "bottom_tabs": list(base.bottom_tabs),
        "segment_tabs": list(base.segment_tabs),
        "segment_tab_aliases": list(base.segment_tab_aliases),
        "home_markers": list(base.home_markers),
        "logged_in_tabs": list(base.logged_in_tabs),
        "logged_in_tab_hits": base.logged_in_tab_hits,
        "logged_in_pages": list(base.logged_in_pages),
        "login_page_markers": list(base.login_page_markers),
        "legal_page_markers": list(base.legal_page_markers),
        "foreground_markers": list(base.foreground_markers),
        "surfaces": list(base.surfaces),
        "surface_extra": dict(base.surface_extra or {}),
        "lexicon": dict(base.lexicon or {}),
        "clip_plans": dict(base.clip_plans or {}),
        "center_action": base.center_action,
        "chrome_notes": list(base.chrome_notes),
    }
    for key, val in override.items():
        if key not in row:
            continue
        if val is None or val == "" or val == [] or val == {}:
            continue
        row[key] = val
    return _profile_from_mapping(row)


# ---------- contextvar 绑定 ----------


def bind(
    *,
    package: str = "",
    profile: Optional[UiProfile] = None,
    override: Optional[dict] = None,
    playbook: Optional[dict] = None,
) -> contextvars.Token:
    cur = dict(_CTX.get() or {})
    prof = profile
    if prof is None and override:
        prof = merge_override(DEFAULT, override)
    elif prof is None and package:
        prof = for_package(package)
    if prof is not None and override and profile is not None:
        prof = merge_override(prof, override)
    if prof is not None:
        cur["profile"] = prof
    if package:
        cur["package"] = str(package)
    if isinstance(playbook, dict):
        cur["playbook"] = playbook
    elif "playbook" in cur:
        cur.pop("playbook", None)
    return _CTX.set(cur)


def reset(token: contextvars.Token) -> None:
    try:
        _CTX.reset(token)
    except Exception:
        pass


def current(package: str = "") -> UiProfile:
    """当前生效的画像。显式 package 优先，其次 contextvar 绑定，最后通用默认。"""
    if package:
        prof = for_package(package)
        if prof.key != "_default":
            return prof
    bound = (_CTX.get() or {}).get("profile")
    if isinstance(bound, UiProfile):
        return bound
    if package:
        return for_package(package)
    return DEFAULT


def bound_package() -> str:
    return str((_CTX.get() or {}).get("package") or "")


__all__ = [
    "UiProfile",
    "DEFAULT",
    "GENERIC_SURFACES",
    "E2E_SURFACE",
    "DEFAULT_SURFACES",
    "DEFAULT_KEY",
    "bind",
    "reset",
    "current",
    "for_package",
    "for_key",
    "list_profiles",
    "merge_override",
    "reload_profiles",
    "bound_package",
    "replace",
]
