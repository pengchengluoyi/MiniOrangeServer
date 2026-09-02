# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""预期质检：按句记账。一句里两句话是两道观察，红了就停，不拿进页代替球/选中态。"""
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


# 整句仍无法用看图/DOM 交代的类型。选中态 / 动画走具体事件，不在这里拆出去。
_UNVERIFIABLE = [
    ("av_haptic", r"视频自动播|自动播放|声音|音效|震动|听筒"),
    ("subjective", r"好看|高级感|沉浸|符合设计|精致|美观"),
    ("temporal", r"连续多帧|无卡死|无闪退|不卡顿"),
    ("no_baseline", r"(?<!显示为)\+1|从.+变"),
    ("pixel_perfect", r"像素|对齐|字号|色值|4px|间距精确"),
]

_UNKNOWN = re.compile(r"功能正常|逻辑正确|无异常|与设计一致")
_ABSENT = re.compile(r"不出现|不含|不可见|看不到|没有出现|未出现")
_PRESENT = re.compile(r"出现|可见「|文案")
_NUMERIC = re.compile(r"数量为|显示为\s*\d|积分为")
_PAGE = re.compile(r"进入|切换到|到达|跳转|落地页")
_LOGIN = re.compile(r"登录态|保持登录|仍在登录|未弹出登录")
_NODE = re.compile(r"可见|存在")
_TAB = re.compile(r"选中态|已选中|高亮选中")
_MEANING = re.compile(r"悬浮球|弹窗|领取|商品页|对话页|动画")
_QUOTE_RE = re.compile(r"[「『\"'][^」』\"']*[」』\"']")
_CLAUSE_SPLIT = re.compile(r"[。；;、，,\n]+")

_KIND_LABEL = {
    "text_absent": "文本不出现",
    "text_present": "文本",
    "numeric": "数量",
    "page_nav": "到页",
    "login_outcome": "登录结果",
    "node": "控件",
    "meaning": "含义",
    "tab_selected": "选中态",
    "av_haptic": "声画震动",
    "subjective": "主观观感",
    "temporal": "连续多帧",
    "no_baseline": "无基线对比",
    "pixel_perfect": "像素级对齐",
    "unknown": "无法识别",
    "skip": "不验",
}


def split_expect_clauses(text: str) -> List[str]:
    """把混合预期拆成按序观察的短句。引号内的逗号不拆。"""
    t = str(text or "").strip()
    if not t:
        return []
    held: List[str] = []

    def _keep(m: re.Match) -> str:
        held.append(m.group(0))
        return f"\x00{len(held) - 1}\x00"

    masked = _QUOTE_RE.sub(_keep, t)
    out: List[str] = []
    for part in _CLAUSE_SPLIT.split(masked):
        s = str(part or "").strip()
        if not s:
            continue
        s = re.sub(r"\x00(\d+)\x00", lambda m: held[int(m.group(1))], s)
        s = s.strip(" \t。；;、，,")
        if s:
            out.append(s)
    return out if out else [t]


def classify_expect_claim(text: str) -> ExpectClaim:
    t = str(text or "").strip()
    if not t:
        return ExpectClaim(text=t, kind="skip", code="EXPECT.SKIPPED.no_expect", label="不验", gap=True)
    for kind, pat in _UNVERIFIABLE:
        if re.search(pat, t, re.I) and not _PAGE.search(t) and not _ABSENT.search(t) and not _TAB.search(t):
            return ExpectClaim(
                text=t, kind=kind, code=f"EXPECT.UNVERIFIABLE.{kind}",
                label=_KIND_LABEL.get(kind, "无法验证"), gap=True,
            )
    if _UNKNOWN.search(t) and not _PAGE.search(t) and not _ABSENT.search(t):
        return ExpectClaim(text=t, kind="unknown", code="EXPECT.UNKNOWN", label="无法识别", gap=True)
    if _ABSENT.search(t):
        return ExpectClaim(text=t, kind="text_absent", code="EXPECT.PASS.text_absent", label="文本不出现", gap=False)
    if _TAB.search(t) and not _PAGE.search(t):
        return ExpectClaim(text=t, kind="tab_selected", code="EXPECT.PASS.tab_selected", label="选中态", gap=False)
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
    return ExpectClaim(text=t, kind="meaning", code="EXPECT.PASS.meaning", label="含义", gap=False)


def classify_expect_text(text: str) -> ExpectClass:
    t = str(text or "").strip()
    if not t:
        return ExpectClass(kind="skip", code="EXPECT.SKIPPED.no_expect", label="不验", gap=True, prompt_text="")
    claims = [classify_expect_claim(part) for part in split_expect_clauses(t)]
    if not claims:
        one = classify_expect_claim(t)
        claims = [one]
    real = [c for c in claims if not c.gap]
    skipped = [c for c in claims if c.gap]
    head = real[0] if real else claims[0]
    return ExpectClass(
        kind=head.kind,
        code=head.code,
        label=head.label,
        gap=not real,
        prompt_text="，".join(c.text for c in real),
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
