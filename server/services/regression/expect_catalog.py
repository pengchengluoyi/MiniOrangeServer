# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""预期质检库：只有命中的类型才走 assert_visual，其余直接无法验证。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List


@dataclass
class ExpectClaim:
    text: str
    kind: str
    code: str
    label: str
    gap: bool


@dataclass
class ExpectClass:
    kind: str
    code: str
    label: str
    gap: bool
    prompt_text: str
    skipped: List[ExpectClaim] = field(default_factory=list)
    claims: List[ExpectClaim] = field(default_factory=list)


_UNVERIFIABLE = [
    ("animation", r"动画|转场|跟手|粒子|入场动效|流畅"),
    ("av_haptic", r"视频自动播|自动播放|声音|音效|震动|听筒"),
    ("subjective", r"好看|高级感|沉浸|符合设计|精致|美观"),
    ("temporal", r"连续多帧|无卡死|无闪退|不卡顿|跟手"),
    ("no_baseline", r"(?<!显示为)\+1|从.+变"),
    ("pixel_perfect", r"像素|对齐|字号|色值|4px|间距精确"),
    ("tab_selected", r"选中态|已选中|高亮选中"),
    ("session_frame", r"保持登录|仍在登录|登录态"),
]

_UNKNOWN = re.compile(r"功能正常|逻辑正确|无异常|与设计一致")
_ABSENT = re.compile(r"不出现|不含|不可见|看不到|没有出现|未出现")
_PRESENT = re.compile(r"出现|可见「|文案")
_NUMERIC = re.compile(r"数量为|显示为\s*\d|积分为")
_PAGE = re.compile(r"进入|切换到|到达|跳转|落地页")
_LOGIN = re.compile(r"登录态|保持登录|仍在登录|未弹出登录")
_NODE = re.compile(r"可见|存在")
_MEANING = re.compile(r"悬浮球|弹窗|领取|商品页|对话页")

_SPLIT = re.compile(r"[。；;]+|\n+")

_KIND_LABEL = {
    "text_absent": "文本不出现",
    "text_present": "文本",
    "numeric": "数量",
    "page_nav": "到页",
    "login_outcome": "登录结果",
    "node": "控件",
    "meaning": "含义",
    "animation": "动画/转场",
    "av_haptic": "声画震动",
    "subjective": "主观观感",
    "temporal": "连续多帧",
    "no_baseline": "无基线对比",
    "pixel_perfect": "像素级对齐",
    "tab_selected": "选中态/切页",
    "session_frame": "首页登录态",
    "unknown": "无法识别",
    "skip": "不验",
}


def classify_expect_claim(text: str) -> ExpectClaim:
    t = str(text or "").strip()
    if not t:
        return ExpectClaim(text=t, kind="skip", code="EXPECT.SKIPPED.no_expect", label="不验", gap=True)
    for kind, pat in _UNVERIFIABLE:
        if re.search(pat, t, re.I):
            return ExpectClaim(
                text=t, kind=kind, code=f"EXPECT.UNVERIFIABLE.{kind}",
                label=_KIND_LABEL.get(kind, "无法验证"), gap=True,
            )
    if _UNKNOWN.search(t):
        return ExpectClaim(text=t, kind="unknown", code="EXPECT.UNKNOWN", label="无法识别", gap=True)
    if _ABSENT.search(t):
        return ExpectClaim(text=t, kind="text_absent", code="EXPECT.PASS.text_absent", label="文本不出现", gap=False)
    if _PRESENT.search(t):
        return ExpectClaim(text=t, kind="text_present", code="EXPECT.PASS.text_present", label="文本", gap=False)
    if _NUMERIC.search(t):
        return ExpectClaim(text=t, kind="numeric", code="EXPECT.PASS.numeric", label="数量", gap=False)
    if _PAGE.search(t):
        return ExpectClaim(text=t, kind="page_nav", code="EXPECT.PASS.page_nav", label="到页", gap=False)
    if _LOGIN.search(t):
        return ExpectClaim(text=t, kind="login_outcome", code="EXPECT.PASS.login_outcome", label="登录结果", gap=False)
    if _NODE.search(t):
        return ExpectClaim(text=t, kind="node", code="EXPECT.PASS.node", label="控件", gap=False)
    if _MEANING.search(t):
        return ExpectClaim(text=t, kind="meaning", code="EXPECT.PASS.meaning", label="含义", gap=False)
    return ExpectClaim(text=t, kind="unknown", code="EXPECT.UNKNOWN", label="无法识别", gap=True)


def _parts(text: str) -> list[str]:
    t = str(text or "").strip()
    if not t:
        return []
    bits = [p.strip(" ，,") for p in _SPLIT.split(t) if p.strip()]
    if len(bits) <= 1 and "，" in t:
        bits = [p.strip() for p in t.split("，") if p.strip()]
    return bits or [t]


def classify_expect_text(text: str) -> ExpectClass:
    t = str(text or "").strip()
    if not t:
        return ExpectClass(kind="skip", code="EXPECT.SKIPPED.no_expect", label="不验", gap=True, prompt_text="")
    claims = [classify_expect_claim(p) for p in _parts(t)]
    supported = [c for c in claims if not c.gap]
    skipped = [c for c in claims if c.gap]
    if not supported:
        one = skipped[0] if skipped else classify_expect_claim(t)
        return ExpectClass(
            kind=one.kind, code=one.code, label=one.label, gap=True,
            prompt_text="", skipped=skipped or [one], claims=claims or [one],
        )
    return ExpectClass(
        kind=supported[0].kind,
        code=supported[0].code,
        label=supported[0].label,
        gap=False,
        prompt_text="；".join(c.text for c in supported),
        skipped=skipped,
        claims=claims,
    )


def gap_summary(row: ExpectClass) -> str:
    if not row.gap and not row.skipped:
        return ""
    bits = [f"{c.label}（{c.text}）" for c in row.skipped]
    if row.gap:
        return "无法验证：" + ("；".join(bits) or row.label)
    return "部分无法验证：" + "；".join(bits)
