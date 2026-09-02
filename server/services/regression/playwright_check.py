# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Web 校验：能靠 URL / DOM / a11y 判的，不当看图，也不当无法验证。"""
from __future__ import annotations

import re
from typing import Optional

from server.services.regression.expect_catalog import ExpectClass


def check_expect(page, row: ExpectClass) -> Optional[tuple[bool, str]]:
    """能判则返回 (ok, reason)；看不了返回 None，交给无法验证或看图。

    不点、不填、不 evaluate。混合句按句看：一句红了就停，后面的不再判。
    """
    if page is None:
        return None
    claims = list(getattr(row, "claims", None) or [])
    if len(claims) > 1:
        reasons: list[str] = []
        any_hit = False
        for claim in claims:
            if getattr(claim, "gap", False):
                continue
            mini = ExpectClass(
                kind=claim.kind,
                code=claim.code,
                label=claim.label,
                gap=claim.gap,
                prompt_text=claim.text,
                claims=[claim],
            )
            got = _check_one(page, mini)
            if got is None:
                continue
            any_hit = True
            ok, reason = got
            reasons.append(reason)
            if not ok:
                return False, reason[:240]
        if not any_hit:
            return None
        return True, "；".join(reasons)[:240]
    return _check_one(page, row)


def _check_one(page, row: ExpectClass) -> Optional[tuple[bool, str]]:
    kind = str(getattr(row, "kind", "") or "")
    text = str(getattr(row, "prompt_text", "") or "").strip()
    if not text:
        claims = list(getattr(row, "claims", None) or [])
        text = " ".join(c.text for c in claims if not getattr(c, "gap", False)) or " ".join(
            getattr(c, "text", "") for c in claims
        )
    text = (text or "").strip()
    try:
        if re.search(r"选中态|已选中|高亮选中", text):
            tab = _tab_selected(page, text)
            if tab is not None:
                return tab
        if kind == "page_nav" or re.search(r"进入|切换到|到达|跳转", text):
            return _page_nav(page, text)
        if kind in {"text_present", "node", "meaning"}:
            return _visible(page, text, should_exist=True)
        if kind == "text_absent":
            return _visible(page, _absent_needle(text), should_exist=False)
        if kind == "tab_selected":
            return _tab_selected(page, text)
        if kind in {"login_outcome", "session_frame"}:
            return _logged_in(page)
    except Exception:
        return None
    return None


def _needle(text: str) -> str:
    t = str(text or "")
    for prefix in ("进入", "切换到", "到达", "跳转", "出现", "可见"):
        if t.startswith(prefix):
            t = t[len(prefix):]
            break
    return t.strip().strip("「」\"'。；;")


def _absent_needle(text: str) -> str:
    t = str(text or "")
    for token in ("不出现", "不含", "不可见", "看不到", "没有出现", "未出现"):
        t = t.replace(token, "")
    return t.strip().strip("「」\"'。；;")


def _visible(page, needle: str, *, should_exist: bool) -> Optional[tuple[bool, str]]:
    name = _needle(needle)
    if not name:
        return None
    loc = page.get_by_text(name)
    count = 0
    try:
        count = loc.count()
    except Exception:
        count = 0
    seen = count > 0
    if should_exist:
        return (True, f"页上可见「{name[:40]}」") if seen else (False, f"页上未见「{name[:40]}」")
    return (True, f"页上没有「{name[:40]}」") if not seen else (False, f"页上仍能看见「{name[:40]}」")


def _page_nav(page, text: str) -> tuple[bool, str]:
    name = _needle(text)
    url = str(page.url or "")
    title = ""
    try:
        title = str(page.title() or "")
    except Exception:
        pass
    blob = f"{url} {title}"
    if name and (name in url or name in title):
        return True, f"当前 {url}"
    if name:
        vis = _visible(page, name, should_exist=True)
        if vis and vis[0]:
            return True, f"当前 {url}，可见「{name[:40]}」"
        return False, f"未到「{name[:40]}」，当前 {url or title or '未知页'}"
    return (bool(url), f"当前 {url or blob}")


def _quoted_name(text: str) -> str:
    t = _needle(text)
    m = re.search(r"[「\"']([^」\"']+)[」\"']", t)
    if m:
        return m.group(1).strip()
    for token in ("底部", "导航", "为选中态", "已选中", "高亮选中", "选中态", "tab"):
        t = t.replace(token, "")
    return t.strip(" ，,。；;")


def _tab_selected(page, text: str) -> Optional[tuple[bool, str]]:
    name = _quoted_name(text)
    if not name:
        return None
    selected = page.get_by_role("tab", selected=True)
    try:
        n = selected.count()
        if n:
            labels = [selected.nth(i).inner_text() for i in range(min(n, 4))]
            hit = any(name in str(x) for x in labels)
            return (hit, f"选中 tab：{'/'.join(labels)[:80]}" if hit else f"选中的是 {'/'.join(labels)[:40]}，不是「{name[:40]}」")
    except Exception:
        pass
    loc = page.get_by_role("tab", name=name)
    if loc.count() == 0:
        loc = page.get_by_role("link", name=name)
    if loc.count() == 0:
        return None
    try:
        pressed = loc.first.get_attribute("aria-selected") or loc.first.get_attribute("aria-current")
        if str(pressed or "").lower() in {"true", "page", "true"}:
            return True, f"「{name[:40]}」为选中"
        cls = str(loc.first.get_attribute("class") or "")
        if any(k in cls.lower() for k in ("active", "selected", "current")):
            return True, f"「{name[:40]}」带选中 class"
        return False, f"「{name[:40]}」未见选中态"
    except Exception:
        return None


def _logged_in(page) -> Optional[tuple[bool, str]]:
    url = str(page.url or "")
    if any(k in url.lower() for k in ("login", "signin", "passport")):
        return False, f"仍在登录页 {url}"
    if page.get_by_text("登录").count() and page.get_by_role("button", name="登录").count():
        return False, "仍能看到登录按钮"
    return True, f"不在登录页 {url}"
