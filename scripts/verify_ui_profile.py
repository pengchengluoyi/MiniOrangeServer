#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验说明书覆写后，登录/首页判定与历史硬编码一致。

应用事实在库里的 playbook；本脚本用夹具模拟 bind(override=...)。

用法：python scripts/verify_ui_profile.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.playbook_fixture import OTHER_PKG, ZHW_PKG, bind_zaohaowu, zaohaowu_playbook, zaohaowu_profile  # noqa: E402
from server.services.ai import app_profile as ap  # noqa: E402
from server.services.ai.playbook_service import prompt_block  # noqa: E402
from server.services.case_precondition_service import _main_tab_bar_logged_in  # noqa: E402
from server.services.shared.page_context.page_context_service import (  # noqa: E402
    _identify_page_by_screen_keywords,
)

# 搬家前的硬编码（照抄自 git 历史，作为对照基准）
LEGACY_LOGIN_TABS = ("首页", "造物秀", "消息", "我的")
LEGACY_HOME_TABS = ("首页", "消息", "我的", "想要", "造物秀", "AI创意", "想要成真")
LEGACY_SEGMENT_NAMES = {"造物秀", "AI创意", "想要成真", "真造物秀", "怪兽", "艺术家专区"}
LEGACY_LEGAL = ("平台用户协议", "造好物 - 平台", "造好物- 平台")


def legacy_logged_in(blob: str) -> bool:
    return sum(1 for t in LEGACY_LOGIN_TABS if t in (blob or "")) >= 3


def legacy_home(blob: str) -> bool:
    hits = sum(1 for k in LEGACY_HOME_TABS if k in blob)
    return hits >= 2 or (hits >= 1 and any(k in blob for k in ("推荐", "关注", "发现", "Feed", "feed")))


SCREENS = [
    ("首页完整底栏", "首页 造物秀 消息 我的 推荐 关注 更多内容"),
    ("底栏只两个", "首页 我的 一些内容"),
    ("底栏三个", "首页 消息 我的"),
    ("只有首页+推荐", "首页 推荐 精选"),
    ("登录页", "手机号登录 获取验证码 一键登录 访客浏览 用户协议"),
    ("协议全文页", "造好物 - 平台用户协议 " + "条款正文内容 " * 40),
    ("空屏", ""),
    ("无关应用首页", "工作台 审批 通讯录 我的 推荐"),
]


def main() -> int:
    failed = []

    def check(cond, msg):
        print(f"   {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failed.append(msg)

    print("── 说明书夹具和搬家前的硬编码逐条对齐")
    z = zaohaowu_profile()
    check(z.key == "zaohaowu", f"夹具解析到造好物画像（{z.key}）")
    check(set(z.login_signal_tabs()) == set(LEGACY_LOGIN_TABS), f"登录信号 tab 一致 {z.login_signal_tabs()}")
    check(z.logged_in_tab_hits == 3, "命中阈值 3 不变")
    check(set(z.home_tabs()) == set(LEGACY_HOME_TABS), f"首页 tab 一致 {z.home_tabs()}")
    check(set(z.segment_tab_names()) == LEGACY_SEGMENT_NAMES, "分段 tab（含别名）一致")
    check(z.legal_markers() == LEGACY_LEGAL, f"协议页标记一致 {z.legal_markers()}")
    check("造物者" in z.brand_markers(), "品牌文案在画像里")
    check("用户协议" not in z.brand_markers(), "通用词没混进品牌文案（否则任何协议页都会被当品牌页）")
    block = prompt_block(zaohaowu_playbook())
    check("造物秀" in block and "应用基础逻辑" in block, "说明书 prompt 含导航术语")

    print("\n── 造好物：逐屏判定与搬家前一致")
    tok = bind_zaohaowu()
    try:
        for name, blob in SCREENS:
            got_login = _main_tab_bar_logged_in(blob)
            want_login = legacy_logged_in(blob)
            check(got_login == want_login, f"[{name}] 已登录判定 {got_login}（基准 {want_login}）")

            page = _identify_page_by_screen_keywords(blob) or {}
            got_home = page.get("label") == "首页"
            if page.get("label") in (None, "首页"):
                check(got_home == legacy_home(blob), f"[{name}] 首页判定 {got_home}（基准 {legacy_home(blob)}）")
            else:
                print(f"   SKIP  [{name}] 被前置规则识别为「{page.get('label')}」，不参与首页比对")
    finally:
        ap.reset(tok)

    print("\n── 未知应用：保留通用能力，但不借用别人的专属字面量")
    tok = ap.bind(package=OTHER_PKG)
    try:
        d = ap.current()
        check(d.key == "_default", f"落到通用默认（{d.key}）")
        check(not d.has_nav(), "没有应用专属 tab 文案")
        check(
            set(d.home_tabs()) == set(ap.GENERIC_TAB_LABELS),
            f"仍保留通用主导航词（{d.home_tabs()}）—— 搬家前未接入的应用就是靠这几个词识别首页的，不能弄丢",
        )
        check(_main_tab_bar_logged_in("首页 消息 我的 内容"), "3 个通用主导航词 → 已登录（与搬家前一致）")
        page = _identify_page_by_screen_keywords("首页 我的 推荐 内容流") or {}
        check(page.get("label") == "首页", f"通用首页判定仍可用（{page.get('label')}）")

        mixed = "首页 我的 造物秀"
        check(
            not _main_tab_bar_logged_in(mixed),
            "「2 通用 + 1 别人专属」不算已登录（专属 tab 不该为未知应用计数）",
        )
        page2 = _identify_page_by_screen_keywords("造物秀 AI创意 想要成真") or {}
        check(page2.get("label") != "首页", f"纯专属 tab 的屏不认成首页（实际 {page2.get('label') or '未识别'}）")

        generic_legal = "本应用用户协议 " + "条款正文 " * 60
        page3 = _identify_page_by_screen_keywords(generic_legal) or {}
        check(page3.get("label") == "用户协议页", f"通用协议页判定仍可用（{page3.get('label')}）")
    finally:
        ap.reset(tok)

    print("\n── 同一屏在两个应用下的判定差异（证明画像真的生效了）")
    mixed = "首页 我的 造物秀"
    tok = bind_zaohaowu()
    try:
        zhw_hit = _main_tab_bar_logged_in(mixed)
    finally:
        ap.reset(tok)
    tok = ap.bind(package=OTHER_PKG)
    try:
        other_hit = _main_tab_bar_logged_in(mixed)
    finally:
        ap.reset(tok)
    check(zhw_hit and not other_hit, f"造好物={zhw_hit} / 未知应用={other_hit}（同一屏，判定不同）")
    check(legacy_logged_in(mixed) == zhw_hit, "造好物的判定与搬家前基准一致")

    print("\n── 没有绑定时不许崩")
    check(ap.current().key == "_default", "未绑定 → 通用默认")
    check(ap.for_package(ZHW_PKG).key == "_default", "运行期 for_package 不再按 YAML 找应用")
    check(_main_tab_bar_logged_in("") is False, "空屏不崩且不误判")

    print("\n" + ("=== 全部通过 ===" if not failed else f"=== {len(failed)} 条失败 ==="))
    for m in failed:
        print(f"  · {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
