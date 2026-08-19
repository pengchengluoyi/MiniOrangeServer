# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""L0 系统层恢复：取证 → 规则匹配 → 处置（YAML 驱动）。

分工（对应 docs/plan-device-recovery-and-app-knowledge.md §1）：
  - **代码只做两件事**：采集事实（collect_evidence）与兜住止损（max_attempts / 轮数上限）；
  - **判断与处置写在 YAML**（plugins/recovery/*.yaml），新增一种系统状况不必改 Python：
      mode=deterministic  命中即按 actions 执行（省钱，高置信场景）
      mode=advise         只产出 prompt_snippet，交给 SystemAgent 看图决定（长尾场景）

复用关系：
  - 取证走 `probe_device_state` 能力（纯 YAML low_level，见 executors/low_level.py）
  - 处置动作里的 `target` 语义锚点由 `hierarchy.py` 解析成精确坐标
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from script.log import SLog

from server.services.ai.regression.schemas import EventStatus, PlanEvent

TAG = "Recovery"

_FOREGROUND_RE = re.compile(r"(?:topResumedActivity|mResumedActivity)=\S+\s+\S+\s+([\w.]+)/")


# ---------- 取证：只输出事实，不下结论 ----------


@dataclass
class Evidence:
    awake: str = "unknown"            # yes | no | unknown
    locked: str = "unknown"
    foreground_pkg: str = ""
    target_alive: str = "unknown"
    anr: str = "unknown"
    ime_shown: str = "unknown"
    top_window_pkg: str = ""
    screen_blocked: str = "unknown"   # 派生：息屏或锁屏 → yes（供规则少写一层「或」）
    app_foreground: str = "unknown"   # 派生：前台是否为被测应用
    raw: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def as_match_dict(self) -> dict[str, str]:
        return {
            "awake": self.awake,
            "locked": self.locked,
            "target_alive": self.target_alive,
            "anr": self.anr,
            "ime_shown": self.ime_shown,
            "screen_blocked": self.screen_blocked,
            "app_foreground": self.app_foreground,
        }

    def brief(self) -> str:
        return (f"awake={self.awake} locked={self.locked} fg={self.foreground_pkg or '-'} "
                f"alive={self.target_alive} anr={self.anr}")


def _yes_no(cond: Optional[bool]) -> str:
    if cond is None:
        return "unknown"
    return "yes" if cond else "no"


def collect_evidence(ctx, router, *, target_package: str = "") -> Evidence:
    """跑一次 probe_device_state 并把输出规整成事实字段。

    规整是代码的职责（提取事实），判断是规则的职责（怎么处置）。
    """
    pkg = target_package or str(getattr(ctx, "target_package", "") or "")
    event = PlanEvent(
        seq=0, capability_id="probe_device_state", event_kind="probe_device_state",
        params={"package": pkg}, needs_vlm=False,
        ai_reasoning="L0 取证", label="设备取证",
    )
    try:
        result = router.dispatch(event, run_id=getattr(ctx, "run_id", ""), case_id="",
                                 case_brief="", shared={})
    except Exception as exc:  # pragma: no cover
        return Evidence(error=f"取证 dispatch 异常: {exc}")

    low = ((result.raw_response or {}).get("low_level")) or {}
    ev = Evidence(raw=low)
    if result.status not in (EventStatus.PASS,):
        ev.error = result.error or "取证失败"

    power = low.get("power") if isinstance(low.get("power"), dict) else {}
    keyguard = low.get("keyguard") if isinstance(low.get("keyguard"), dict) else {}
    ime = low.get("ime") if isinstance(low.get("ime"), dict) else {}

    wake = str(power.get("mWakefulness") or "")
    if wake:
        ev.awake = _yes_no(wake.lower() == "awake")

    showing = keyguard.get("isKeyguardShowing")
    if showing is None:
        showing = keyguard.get("mDreamingLockscreen")
    if showing is not None:
        ev.locked = _yes_no(str(showing).lower() == "true")

    fg = low.get("foreground")
    if isinstance(fg, list) and fg:
        m = _FOREGROUND_RE.search(" ".join(str(x) for x in fg))
        if m:
            ev.foreground_pkg = m.group(1)
    if ev.foreground_pkg:
        ev.top_window_pkg = ev.foreground_pkg   # 目前用前台包近似顶层窗口包
        if pkg:
            ev.app_foreground = _yes_no(ev.foreground_pkg == pkg)

    pid = low.get("target_pid")
    if isinstance(pid, str):
        ev.target_alive = _yes_no(bool(pid.strip()))

    anr = low.get("anr_window")
    if isinstance(anr, str) and anr.strip().isdigit():
        ev.anr = _yes_no(int(anr.strip()) > 0)

    shown = ime.get("mInputShown")
    if shown is not None:
        ev.ime_shown = _yes_no(str(shown).lower() == "true")

    # 派生事实：屏幕是否处于「不可操作」状态
    if ev.awake == "no" or ev.locked == "yes":
        ev.screen_blocked = "yes"
    elif ev.awake == "yes" and ev.locked == "no":
        ev.screen_blocked = "no"

    SLog.i(TAG, f"evidence: {ev.brief()}")
    return ev


# ---------- 匹配 ----------


@dataclass
class RuleMatch:
    rule: Any
    reasons: list[str] = field(default_factory=list)

    @property
    def rule_id(self) -> str:
        return getattr(self.rule, "id", "")


def _match_conditions(match, evidence: Evidence, screen_texts: list[str]) -> tuple[bool, list[str]]:
    """规则条件全部满足才算命中（AND）；每类内部是 OR。"""
    reasons: list[str] = []
    facts = evidence.as_match_dict()

    for key, want in (match.evidence or {}).items():
        got = facts.get(key, "unknown")
        if got != str(want):
            return False, []
        reasons.append(f"{key}={got}")

    prefixes = match.top_window_pkg_prefix or []
    if prefixes:
        pkg = evidence.top_window_pkg or ""
        if not any(pkg.startswith(p) for p in prefixes):
            return False, []
        reasons.append(f"top_window={pkg}")

    texts = match.screen_text_any or []
    if texts:
        blob = " ".join(screen_texts)
        hit = next((t for t in texts if t and t in blob), None)
        if hit is None:
            return False, []
        reasons.append(f"screen_text~{hit}")

    if not (match.evidence or prefixes or texts):
        return False, []          # 空条件不允许命中一切
    return True, reasons


def screen_texts_from_hierarchy(dump) -> list[str]:
    """把层级里的可见文案抽成列表，供 screen_text_any 匹配。"""
    if dump is None or not getattr(dump, "ok", False):
        return []
    out: list[str] = []
    for n in dump.nodes:
        if n.text:
            out.append(n.text)
        if n.content_desc:
            out.append(n.content_desc)
    return out


def match_rules(evidence: Evidence, screen_texts: Optional[list[str]] = None,
                *, rules: Optional[list] = None, app_id: str = "") -> list[RuleMatch]:
    """返回所有命中的规则，按 priority 降序。

    rules 留空时从多根 store 取（app 根 > team > builtin > learned，见 packs/store.py）；
    app_id 非空则只取对该应用生效的条目。
    """
    if rules is None:
        # 从多根 store 取「生效中」的规则：已按四根优先级裁决 + 过滤 draft/停用/被覆盖
        from server.services.packs import get_store

        rules = get_store().active_objects("recovery", app_id=app_id or "")
    hits: list[RuleMatch] = []
    for rule in rules:
        ok, reasons = _match_conditions(rule.match, evidence, screen_texts or [])
        if ok:
            hits.append(RuleMatch(rule=rule, reasons=reasons))
    if hits:
        SLog.i(TAG, f"matched rules: {[(h.rule_id, h.reasons) for h in hits]}")
    return hits


# ---------- 处置 ----------


@dataclass
class RecoveryOutcome:
    rule_id: str = ""
    mode: str = ""
    applied: bool = False          # 是否真的执行了动作
    recovered: bool = False        # verify 是否通过
    attempts: int = 0
    actions: list[dict] = field(default_factory=list)
    advice: str = ""               # advise 模式产出，交给 SystemAgent
    error: str = ""

    def summary(self) -> str:
        if self.mode == "advise":
            return f"{self.rule_id}: 给出处置建议（{len(self.advice)} 字）"
        state = "已恢复" if self.recovered else "未恢复"
        return f"{self.rule_id}: 执行 {len(self.actions)} 个动作，{state}（第 {self.attempts} 次尝试）"


def _forbidden(rule, action) -> str:
    """安全护栏：动作要点的文案落在 forbid 名单里就拒绝执行。"""
    banned = [t for t in (rule.forbid.text_any or []) if t]
    if not banned:
        return ""
    probe = " ".join(str(v) for v in (action.target or {}).values())
    probe += " " + " ".join(str(v) for v in (action.params or {}).values())
    for b in banned:
        if b and b in probe:
            return b
    return ""


def apply_rule(match: RuleMatch, ctx, router, *, target_package: str = "") -> RecoveryOutcome:
    """执行一条命中的规则。advise 模式只返回建议文本，不动设备。"""
    rule = match.rule
    out = RecoveryOutcome(rule_id=rule.id, mode=rule.mode)

    if rule.mode == "advise":
        out.advice = rule.prompt_snippet.strip()
        return out

    max_attempts = max(1, int(rule.max_attempts or 1))
    for attempt in range(1, max_attempts + 1):
        out.attempts = attempt
        for idx, action in enumerate(rule.actions, 1):
            banned = _forbidden(rule, action)
            if banned:
                out.actions.append({"capability": action.capability, "skipped": f"forbid:{banned}"})
                SLog.w(TAG, f"[{rule.id}] 动作被护栏拦下（命中 forbid={banned!r}）")
                continue
            params = dict(action.params or {})
            if action.target:
                params["target"] = dict(action.target)
            if action.fallback_xy and len(action.fallback_xy) == 2:
                params.setdefault("x", int(action.fallback_xy[0]))
                params.setdefault("y", int(action.fallback_xy[1]))
            if action.capability in ("launch_app", "close_app") and target_package:
                params.setdefault("package", target_package)
            event = PlanEvent(
                seq=idx, capability_id=action.capability, event_kind=action.capability,
                params=params, needs_vlm=False,
                ai_reasoning=f"L0 恢复 {rule.id}", label=rule.title or rule.id,
            )
            res = router.dispatch(event, run_id=getattr(ctx, "run_id", ""), case_id="",
                                  case_brief="", shared={})
            out.actions.append({
                "capability": action.capability,
                "status": str(res.status.value),
                "summary": res.summary,
            })
            out.applied = True
            if res.status not in (EventStatus.PASS, EventStatus.SKIPPED):
                out.error = res.error or f"{action.capability} 执行失败"

        # verify：留空则视为「执行完就算恢复」
        if not (rule.verify.evidence or rule.verify.screen_text_any
                or rule.verify.top_window_pkg_prefix):
            out.recovered = not out.error
            return out
        ev = collect_evidence(ctx, router, target_package=target_package)
        ok, _ = _match_conditions(rule.verify, ev, [])
        if ok:
            out.recovered = True
            SLog.i(TAG, f"[{rule.id}] 恢复成功（第 {attempt} 次）：{ev.brief()}")
            return out
        SLog.w(TAG, f"[{rule.id}] 第 {attempt}/{max_attempts} 次后仍未通过 verify：{ev.brief()}")

    return out


def recover_if_needed(ctx, router, *, target_package: str = "",
                      dump=None) -> Optional[RecoveryOutcome]:
    """取证 → 匹配 → 处置第一条命中的规则。无命中返回 None。

    这是给主循环用的单一入口；轮数上限由调用方（AgentExecutor）控制。
    """
    ev = collect_evidence(ctx, router, target_package=target_package)
    texts = screen_texts_from_hierarchy(dump)
    hits = match_rules(ev, texts, app_id=str(getattr(ctx, "app_id", "") or ""))
    if not hits:
        return None
    return apply_rule(hits[0], ctx, router, target_package=target_package)
