#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""离线校验：Web 槽位 / Playwright 执行器注册。不强制拉起 Chromium。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.services.plugins import registry as plugin_registry  # noqa: E402
from server.services.regression.executors import build_default_executors  # noqa: E402
from server.services.regression.expect_catalog import classify_expect_text  # noqa: E402
from server.services.regression.playwright_check import check_expect  # noqa: E402
from server.services.runtime.playwright_hub import (  # noqa: E402
    WEB_SLOT_SN,
    is_web_slot,
    probe_playwright,
)
from server.services.runtime.run_context import RunContext, device_platform_kind  # noqa: E402
from server.services.project_env import target_id_from_snapshot  # noqa: E402

_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'✅' if ok else '❌'} {name}" + ("" if ok else f"  {detail}"))
    if not ok:
        _fails.append(name)


class _FakeLocator:
    def __init__(self, count: int, text: str = "", selected: str = "", cls: str = ""):
        self._count = count
        self._text = text
        self._selected = selected
        self._cls = cls

    def count(self) -> int:
        return self._count

    @property
    def first(self):
        return self

    def nth(self, i: int):
        return self

    def inner_text(self) -> str:
        return self._text

    def get_attribute(self, name: str):
        if name in ("aria-selected", "aria-current"):
            return self._selected
        if name == "class":
            return self._cls
        return None


class _FakePage:
    def __init__(self, *, url: str = "", title: str = "", tabs: dict | None = None, texts: list[str] | None = None):
        self.url = url
        self._title = title
        self._tabs = tabs or {}
        self._texts = texts or []

    def title(self) -> str:
        return self._title

    def get_by_role(self, role: str, **kwargs):
        name = str(kwargs.get("name") or "")
        selected = kwargs.get("selected")
        if role == "tab" and selected is True:
            labels = [k for k, v in self._tabs.items() if v]
            if not labels:
                return _FakeLocator(0)
            return _FakeLocator(len(labels), text=labels[0], selected="true")
        if role == "tab" and name:
            if name not in self._tabs:
                return _FakeLocator(0)
            sel = "true" if self._tabs.get(name) else "false"
            return _FakeLocator(1, text=name, selected=sel)
        return _FakeLocator(0)

    def get_by_text(self, name: str, exact: bool = False):
        hit = any((name == t) if exact else (name in t) for t in self._texts)
        return _FakeLocator(1 if hit else 0, text=name)


def test_slot() -> None:
    print("\n[web slot]")
    check("WEB_SLOT_SN", WEB_SLOT_SN == "web-local")
    check("is_web_slot web-local", is_web_slot("web-local"))
    check("is_web_slot web-1", is_web_slot("web-1"))
    check("is_web_slot platform", is_web_slot("", "web"))
    check("pixel 不是 web", not is_web_slot("PIXEL8", "android"))
    check("device_platform_kind", device_platform_kind("web", sn="web-local") == "web")
    check(
        "base_url",
        target_id_from_snapshot({"web": {"base_url": "https://admin.test"}}, "web") == "https://admin.test",
    )
    from server.services.system_settings_service import normalize_web_compress_ratio

    check("web 压缩默认 2", normalize_web_compress_ratio(None) == 2.0)
    check("web 压缩 1 不压", normalize_web_compress_ratio(1) == 1.0)


def test_plugins() -> None:
    print("\n[plugin / executor]")
    exs = plugin_registry.list_executors()
    ids = {e.id for e in exs}
    check("yaml 有 playwright", "playwright" in ids)
    pw = next((e for e in exs if e.id == "playwright"), None)
    check("available_when", bool(pw and "playwright" in (pw.available_when or "")))
    check("platforms 含 web", bool(pw and "web" in (pw.platforms or [])))
    runtime = build_default_executors()
    check("runtime 注册 playwright", "playwright" in runtime)
    tap = next((c for c in plugin_registry.list_capabilities() if c.id == "tap_element"), None)
    tap_ex = {i.executor for i in (tap.implementations if tap else [])}
    check("tap_element 有 playwright 实现", "playwright" in tap_ex)
    ctx = RunContext(sn="web-local", platform="web")
    ctx.playwright = {"state": "available"}
    flags = ctx.connectivity_flags
    check("flags.playwright", flags.get("playwright") is True)
    check("flags.web 别名", flags.get("web") is True)
    check("flags.adb 关", flags.get("adb") is False)
    check("has_control_channel", ctx.has_control_channel is True)
    adb_only = RunContext(sn="PIXEL8", platform="android")
    check("无通道时闸门关", adb_only.has_control_channel is False)


def test_probe() -> None:
    print("\n[probe]")
    state, meta = probe_playwright()
    check("probe 返回 state", state in ("available", "disconnected"))
    print(f"    probe={state} reason={meta.get('reason') or meta.get('path') or ''}")
    if state != "available":
        print("    （未装 Chromium 时跳过真浏览器；部署后执行 playwright install chromium）")


def test_dom_check() -> None:
    print("\n[DOM 校验]")
    row = classify_expect_text("进入首页，底部「首页」为选中态。")
    page = _FakePage(url="https://x/home", title="首页", tabs={"首页": True}, texts=["首页"])
    got = check_expect(page, row)
    check("选中态可判红/绿", got is not None, str(got))
    if got is not None:
        check("选中首页通过", got[0] is True, got[1])
    page_bad = _FakePage(url="https://x/home", title="首页", tabs={"首页": False, "我的": True}, texts=["首页"])
    row_tab = classify_expect_text("底部「首页」为选中态")
    got_bad = check_expect(page_bad, row_tab)
    check("选中态不匹配为校验不通过", got_bad is not None and got_bad[0] is False, str(got_bad))
    none_page = _FakePage(url="https://x/x")
    row_anim = classify_expect_text("动画流畅")
    check("动画仍无法验证", check_expect(none_page, row_anim) is None)


def test_urls() -> None:
    print("\n[url]")
    from server.services.runtime.playwright_hub import looks_like_url, pick_goto_url

    check("https", looks_like_url("https://admin.example.com"))
    check("host.com", looks_like_url("admin.example.com"))
    check("data", looks_like_url("data:text/html,<h1>x</h1>"))
    check("android 包名不是 url", not looks_like_url("com.mathmagic.zaohaowu"))
    check("ios bundle 不是 url", not looks_like_url("com.example.app.ios"))
    check(
        "优先真实 url 而不是包名",
        pick_goto_url("com.foo.bar", "https://admin.test/login", "com.foo.bar")
        == "https://admin.test/login",
    )
    check("补 https", pick_goto_url("admin.example.com") == "https://admin.example.com")


def test_prompt_and_router() -> None:
    print("\n[prompt / router]")
    from server.services.ai.regression.schemas import PlanEvent
    from server.services.regression.router import CapabilityRouter
    from server.services.runtime.menu import available_menu_brief

    ctx = RunContext(sn="web-local", platform="web")
    ctx.playwright = {"state": "available"}
    ctx.vlm = {"state": "available"}
    brief = ctx.to_prompt_brief()
    check("brief.flags.playwright", brief["flags"].get("playwright") is True)
    check("brief.channels.playwright", brief["channels"].get("playwright") == "available")
    check("advice 含 playwright", "playwright" in str(brief.get("router_advice") or ""))

    menu = available_menu_brief(ctx)
    tap = next((c for c in menu if c.get("id") == "tap_element"), None)
    check("菜单有 tap_element", tap is not None)
    if tap:
        check("网页 tap needs_vlm=false", tap.get("needs_vlm") is False)
        exs = {i.get("executor") for i in (tap.get("implementations") or [])}
        check("tap 只剩 playwright", exs == {"playwright"}, str(exs))

    router = CapabilityRouter(ctx, capture_prefer=("playwright",))
    ev = PlanEvent(
        seq=1, capability_id="tap_element",
        params={"selector_text": "登录"}, ai_reasoning="t",
    )
    check("网页不先 VLM locate", router._needs_locate(ev) is False)
    order = router._executor_order(ev)
    check("executor 首选 playwright", order[:1] == ["playwright"], str(order))

    adb_ctx = RunContext(sn="PIXEL8", platform="android")
    adb_ctx.adb = {"state": "connected", "serial": "PIXEL8"}
    adb_ctx.vlm = {"state": "available"}
    adb_router = CapabilityRouter(adb_ctx, capture_prefer=("adb", "remote"))
    ev2 = PlanEvent(
        seq=1, capability_id="tap_element",
        params={"description": "登录"}, ai_reasoning="t",
    )
    check("安卓仍走 locate", adb_router._needs_locate(ev2) is True)


def test_web_precondition() -> None:
    print("\n[web 前置]")
    from server.services.case_precondition_service import run_preconditions

    res = run_preconditions(
        "1. 应用无缓存\n2. 未登录",
        sn="web-local",
        platform="web",
        package="https://admin.example.com",
        phase="before_launch",
    )
    check("web 前置不拦跑", res.get("ok") is True, str(res))
    kinds = {str(i.get("kind")) for i in (res.get("items") or [])}
    check("含 clear_cache", "clear_cache" in kinds, str(kinds))


def test_live_chromium() -> None:
    print("\n[live chromium]")
    state, _ = probe_playwright()
    if state != "available":
        print("    skip（未安装 Chromium）")
        return
    from server.services.regression.executors.playwright_executor import PlaywrightExecutor
    from server.services.runtime.playwright_hub import get_hub

    hub = get_hub()
    sn = "web-verify"
    try:
        page = hub.open_case(sn, headed=False)
        page.set_content("<html><body><button>登录</button></body></html>")
        ex = PlaywrightExecutor()
        check("按名字点到登录", ex._click_by_name(page, "登录") is True)
        png = hub.screenshot_png(sn)
        check("能截图", isinstance(png, (bytes, bytearray)) and len(png) > 80)
    except Exception as exc:
        check("live chromium", False, str(exc))
    finally:
        try:
            hub.close_case(sn)
        except Exception:
            pass
        try:
            hub.shutdown_thread()
        except Exception:
            pass


if __name__ == "__main__":
    test_slot()
    test_plugins()
    test_probe()
    test_dom_check()
    test_urls()
    test_prompt_and_router()
    test_web_precondition()
    test_live_chromium()
    if _fails:
        print(f"\nFAILED {len(_fails)}: {', '.join(_fails)}")
        sys.exit(1)
    print("\nverify_playwright_slot: ok")
