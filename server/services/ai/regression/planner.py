# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""AI-led 回归 Plan / Replan 的高层入口。

负责：
  1. 把 RunContext → run_brief（注入 prompt）
  2. 把 capability_menu → 精简版（注入 prompt）
  3. 调 llm_client.call_chat_text 跑 chat completion
  4. 解析 LLM JSON → Pydantic 模型（PlanResult / ReplanResult）
  5. 做语义校验：capability_id 必须在菜单里；expected_executor 必须在该 cap 的 implementations 里；
     不合规直接降级为 decline，并把 parse_warnings 记下来

这是 Step 3 的 PUBLIC 入口；上层 orchestrator（Step 4+ 才实现）来调它。
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Optional

from pydantic import ValidationError
from script.log import SLog

from server.services.ai.regression import prompts as P
from server.services.ai.regression.llm_client import (
    call_chat_text,
    resolve_regression_provider,
)
from server.services.ai.regression.schemas import (
    AssertResult,
    AgentDecision,
    AgentAction,
    BaselineContext,
    CaseGoal,
    CaseCheckpoint,
    CaseSpec,
    HitlComposerResult,
    LocateResult,
    PersonaExpandResult,
    PlanEvent,
    PlanResult,
    ReplanResult,
)
from server.services.runtime import available_menu_brief
from server.services.runtime.run_context import RunContext

TAG = "RegressionPlanner"

_SCENE_CACHE: dict[str, dict[str, Any]] = {}
_SCENE_CACHE_MAX = 256


def _chat(*, job: str, provider, messages, **kwargs):
    from server.services.ai import dispatch_log as dispatch

    tok = dispatch.bind(role="test-engineer", job=job, skill=job)
    try:
        return call_chat_text(provider=provider, messages=messages, **kwargs)
    finally:
        dispatch.reset(tok)


# ---------- 输出校验 ----------


def _build_menu_index(menu: list[dict[str, Any]]) -> dict[str, set[str]]:
    """{capability_id: {executor_ids ...}}，用于事件级校验。"""
    idx: dict[str, set[str]] = {}
    for cap in menu:
        cap_id = str(cap.get("id") or "")
        if not cap_id:
            continue
        execs: set[str] = set()
        for impl in cap.get("implementations") or []:
            ex = str(impl.get("executor") or "")
            if ex:
                execs.add(ex)
        idx[cap_id] = execs
    return idx


def _validate_events(
    events: list[PlanEvent],
    menu_index: dict[str, set[str]],
) -> tuple[list[PlanEvent], list[str]]:
    """逐条事件校验 capability_id + expected_executor。返回 (合法事件, warnings)。"""
    out: list[PlanEvent] = []
    warnings: list[str] = []
    for ev in events:
        if ev.capability_id not in menu_index:
            warnings.append(
                f"event seq={ev.seq} capability_id={ev.capability_id!r} 不在菜单里，已丢弃"
            )
            continue
        allowed = menu_index[ev.capability_id]
        if ev.expected_executor and ev.expected_executor not in allowed:
            warnings.append(
                f"event seq={ev.seq} capability={ev.capability_id} "
                f"expected_executor={ev.expected_executor!r} 不在该 cap 的 implementations 里 "
                f"{sorted(allowed)}，尝试用首个可用执行器替换"
            )
            ev = ev.model_copy(update={"expected_executor": next(iter(sorted(allowed)), "")})
        if ev.fallback_executors:
            cleaned = [x for x in ev.fallback_executors if x in allowed and x != ev.expected_executor]
            if cleaned != ev.fallback_executors:
                ev = ev.model_copy(update={"fallback_executors": cleaned})
        out.append(ev)
    return out, warnings


def _parse_plan_result(raw: dict[str, Any], case_id: str, menu_index: dict[str, set[str]]) -> PlanResult:
    """LLM raw JSON → PlanResult；遇到 schema 错降级为 decline。"""
    warnings: list[str] = []
    mode = (raw.get("mode") or "plan").strip().lower()
    if mode not in {"plan", "decline"}:
        warnings.append(f"unknown mode={mode!r}, 强制 decline")
        mode = "decline"

    events: list[PlanEvent] = []
    raw_events = raw.get("events") or []
    if mode == "plan":
        if not isinstance(raw_events, list) or not raw_events:
            warnings.append("mode=plan 但 events 为空，强制 decline")
            mode = "decline"
        else:
            for idx, ev_raw in enumerate(raw_events, start=1):
                if not isinstance(ev_raw, dict):
                    warnings.append(f"events[{idx-1}] 非 dict，丢弃")
                    continue
                ev_raw.setdefault("seq", idx)
                ev_raw.setdefault("ai_reasoning", "（模型未给出 reasoning）")
                ev_raw.setdefault("capability_id", "")
                try:
                    events.append(PlanEvent.model_validate(ev_raw))
                except ValidationError as ve:
                    warnings.append(f"events[{idx-1}] schema 不合法: {ve.errors()[:1]}")
            events, more_warn = _validate_events(events, menu_index)
            warnings.extend(more_warn)
            if not events:
                warnings.append("events 全部校验失败，强制 decline")
                mode = "decline"

    if mode == "decline" and not raw.get("decline_reason"):
        raw["decline_reason"] = "模型未指明原因" if not warnings else "; ".join(warnings[:3])

    result = PlanResult(
        mode=mode,  # type: ignore[arg-type]
        case_id=raw.get("case_id") or case_id,
        ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "（模型未给出 reasoning）",
        confidence=float(raw.get("confidence") or 0.0),
        events=events,
        decline_reason=str(raw.get("decline_reason") or ""),
        open_questions=[str(x) for x in (raw.get("open_questions") or []) if str(x).strip()],
        raw_llm=raw,
        parse_warnings=warnings,
    )
    return result


def _parse_replan_result(raw: dict[str, Any], menu_index: dict[str, set[str]]) -> ReplanResult:
    warnings: list[str] = []
    mode = (raw.get("mode") or "replan").strip().lower()
    if mode not in {"replan", "decline", "give_up"}:
        warnings.append(f"unknown mode={mode!r}, 强制 decline")
        mode = "decline"

    events: list[PlanEvent] = []
    raw_events = raw.get("events") or []
    if mode == "replan":
        if not isinstance(raw_events, list) or not raw_events:
            warnings.append("mode=replan 但 events 为空，强制 decline")
            mode = "decline"
        else:
            for idx, ev_raw in enumerate(raw_events, start=1):
                if not isinstance(ev_raw, dict):
                    warnings.append(f"events[{idx-1}] 非 dict，丢弃")
                    continue
                ev_raw.setdefault("seq", idx)
                ev_raw.setdefault("ai_reasoning", "（模型未给出 reasoning）")
                ev_raw.setdefault("capability_id", "")
                try:
                    events.append(PlanEvent.model_validate(ev_raw))
                except ValidationError as ve:
                    warnings.append(f"events[{idx-1}] schema 不合法: {ve.errors()[:1]}")
            events, more_warn = _validate_events(events, menu_index)
            warnings.extend(more_warn)
            if not events:
                warnings.append("events 全部校验失败，强制 decline")
                mode = "decline"

    if mode in {"decline", "give_up"} and not raw.get("decline_reason"):
        raw["decline_reason"] = "模型未指明原因" if not warnings else "; ".join(warnings[:3])

    return ReplanResult(
        mode=mode,  # type: ignore[arg-type]
        ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "（模型未给出 reasoning）",
        events=events,
        drop_remaining=bool(raw.get("drop_remaining", True)),
        decline_reason=str(raw.get("decline_reason") or ""),
        needs_human=bool(raw.get("needs_human", False)),
        raw_llm=raw,
        parse_warnings=warnings,
    )


# ---------- 公开入口 ----------


def generate_overview(
    case_spec: CaseSpec,
    *,
    run_context: RunContext,
    baseline: Optional[BaselineContext] = None,
    baseline_overview_text: str = "",
    provider_id: Optional[str] = None,
    timeout_sec: int = 120,
    app_cache_cleared: bool = False,
) -> PlanResult:
    """跑 PLAN_OVERVIEW_TEXT，输出整 case 的事件序列。

    永远返回 PlanResult（不抛异常）；LLM 不可用 / 解析失败 → mode=decline + 原因。
    """
    menu = available_menu_brief(run_context)
    if not menu:
        return PlanResult(
            mode="decline",
            case_id=case_spec.case_id,
            ai_reasoning="capability_menu 为空，当前 RunContext 没有任何可用能力。",
            confidence=0.0,
            decline_reason="empty capability menu; check connectivity probes",
        )
    menu_index = _build_menu_index(menu)

    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        return PlanResult(
            mode="decline",
            case_id=case_spec.case_id,
            ai_reasoning=f"未启用 AI 规划：{gate.get('reason')}",
            confidence=0.0,
            decline_reason=f"AI provider unavailable: {gate.get('reason')}",
        )

    messages = P.build_plan_overview_messages(
        case_spec=case_spec,
        run_brief=run_context.to_prompt_brief(app_cache_cleared=app_cache_cleared),
        menu=menu,
        baseline=baseline,
        baseline_overview_text=baseline_overview_text,
    )
    raw, meta = _chat(
        job="plan-overview",
        provider=provider,
        messages=messages,
        temperature=0.1,
        max_tokens=4096,
        timeout_sec=timeout_sec,
    )
    if raw is None:
        SLog.w(
            TAG,
            f"generate_overview LLM failed case={case_spec.case_id} "
            f"err={meta.get('error') or meta.get('finish_reason')!r}",
        )
        return PlanResult(
            mode="decline",
            case_id=case_spec.case_id,
            ai_reasoning="LLM 返回为空或 JSON 解析失败",
            confidence=0.0,
            decline_reason=str(meta.get("error") or meta.get("content_preview") or "")[:240],
            raw_llm={"meta": meta},
        )

    return _parse_plan_result(raw, case_spec.case_id, menu_index)


def replan_single_step(
    *,
    run_context: RunContext,
    completed_events: list[Any],
    failed_event: Any,
    failure_summary: str,
    remaining_events: Optional[list[Any]] = None,
    baseline: Optional[BaselineContext] = None,
    provider_id: Optional[str] = None,
    timeout_sec: int = 90,
) -> ReplanResult:
    """跑 SINGLE_STEP_REPLAN，输出新事件队列。"""
    menu = available_menu_brief(run_context)
    if not menu:
        return ReplanResult(
            mode="give_up",
            ai_reasoning="capability_menu 为空（连通性丢失），无法继续 replan。",
            decline_reason="empty capability menu",
        )
    menu_index = _build_menu_index(menu)

    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        return ReplanResult(
            mode="decline",
            ai_reasoning=f"未启用 AI 规划：{gate.get('reason')}",
            decline_reason=f"AI provider unavailable: {gate.get('reason')}",
            needs_human=True,
        )

    messages = P.build_single_step_replan_messages(
        run_brief=run_context.to_prompt_brief(),
        menu=menu,
        completed_events=completed_events,
        failed_event=failed_event,
        failure_summary=failure_summary,
        remaining_events=remaining_events,
        baseline=baseline,
    )
    raw, meta = _chat(
        job="single-step-replan",
        provider=provider,
        messages=messages,
        temperature=0.1,
        max_tokens=2048,
        timeout_sec=timeout_sec,
    )
    if raw is None:
        SLog.w(
            TAG,
            f"replan_single_step LLM failed err={meta.get('error') or meta.get('finish_reason')!r}",
        )
        return ReplanResult(
            mode="decline",
            ai_reasoning="LLM 返回为空或 JSON 解析失败",
            decline_reason=str(meta.get("error") or meta.get("content_preview") or "")[:240],
            needs_human=True,
            raw_llm={"meta": meta},
        )
    return _parse_replan_result(raw, menu_index)


# ============== VLM 子流程：LOCATE_VISION / ASSERT_VISION ==============


def _parse_locate_result(
    raw: dict[str, Any], preview_width: int, preview_height: int
) -> LocateResult:
    warnings: list[str] = []
    found = bool(raw.get("found"))
    x = int(raw.get("x") or 0)
    y = int(raw.get("y") or 0)
    if preview_width > 0 and not (0 <= x <= preview_width):
        warnings.append(f"x={x} 越界 [0,{preview_width}]，clip")
        x = max(0, min(preview_width, x))
    if preview_height > 0 and not (0 <= y <= preview_height):
        warnings.append(f"y={y} 越界 [0,{preview_height}]，clip")
        y = max(0, min(preview_height, y))
    bbox_raw = raw.get("bbox") or []
    if isinstance(bbox_raw, list) and len(bbox_raw) == 4 and all(isinstance(v, (int, float)) for v in bbox_raw):
        bbox = [int(v) for v in bbox_raw]
    else:
        bbox = []
    confidence = float(raw.get("confidence") or 0.0)
    confidence = max(0.0, min(1.0, confidence))
    if found and confidence == 0.0:
        confidence = 0.5  # 模型忘填了，给个中性值

    return LocateResult(
        found=found,
        x=x,
        y=y,
        coord_mode="preview_pixels",
        bbox=bbox,
        confidence=confidence,
        ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "（模型未给出 reasoning）",
        label_seen=str(raw.get("label_seen") or "").strip(),
        raw_llm=raw,
        parse_warnings=warnings,
    )


def _parse_assert_result(raw: dict[str, Any]) -> AssertResult:
    warnings: list[str] = []
    passed = bool(raw.get("passed"))
    confidence = float(raw.get("confidence") or 0.0)
    confidence = max(0.0, min(1.0, confidence))
    evidence = str(raw.get("evidence") or "").strip()
    if not evidence:
        warnings.append("evidence 为空，模型未遵守约束")
    return AssertResult(
        passed=passed,
        confidence=confidence,
        ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "（模型未给出 reasoning）",
        evidence=evidence,
        raw_llm=raw,
        parse_warnings=warnings,
    )


def locate_element(
    *,
    description: str,
    preview_width: int,
    preview_height: int,
    image_base64: str,
    image_mime: str = "image/jpeg",
    ai_hint: str = "",
    provider_id: Optional[str] = None,
    timeout_sec: int = 60,
) -> LocateResult:
    """LOCATE_VISION：在截图上定位一个元素，返回坐标 + 置信度。

    永远返回 LocateResult；LLM 不可用 → found=False + 原因写进 ai_reasoning。
    """
    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        return LocateResult(
            found=False, x=0, y=0, confidence=0.0,
            ai_reasoning=f"未启用 AI 视觉：{gate.get('reason')}",
            parse_warnings=["provider unavailable"],
        )
    messages = P.build_locate_vision_messages(
        description=description,
        preview_width=preview_width,
        preview_height=preview_height,
        image_base64=image_base64,
        image_mime=image_mime,
        ai_hint=ai_hint,
    )
    raw, meta = _chat(
        job="locate-vision",
        provider=provider,
        messages=messages,
        temperature=0.0,
        max_tokens=512,
        timeout_sec=timeout_sec,
    )
    if raw is None:
        return LocateResult(
            found=False, x=0, y=0, confidence=0.0,
            ai_reasoning="LLM 返回空 / JSON 解析失败",
            parse_warnings=[str(meta.get("error") or meta.get("content_preview") or "")[:160]],
            raw_llm={"meta": meta},
        )
    return _parse_locate_result(raw, preview_width, preview_height)


def assert_visual(
    *,
    expectation: str,
    image_base64: str,
    image_mime: str = "image/jpeg",
    ai_hint: str = "",
    context_block: str = "",
    provider_id: Optional[str] = None,
    timeout_sec: int = 60,
) -> AssertResult:
    """ASSERT_VISION：判断当前截图是否满足预期。"""
    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        return AssertResult(
            passed=False, confidence=0.0,
            ai_reasoning=f"未启用 AI 视觉：{gate.get('reason')}",
            evidence="",
            parse_warnings=["provider unavailable"],
        )
    messages = P.build_assert_vision_messages(
        expectation=expectation,
        image_base64=image_base64,
        image_mime=image_mime,
        ai_hint=ai_hint,
        context_block=context_block,
    )
    raw, meta = _chat(
        job="assert-vision",
        provider=provider,
        messages=messages,
        temperature=0.0,
        max_tokens=512,
        timeout_sec=timeout_sec,
    )
    if raw is None:
        return AssertResult(
            passed=False, confidence=0.0,
            ai_reasoning="LLM 返回空 / JSON 解析失败",
            evidence="",
            parse_warnings=[str(meta.get("error") or meta.get("content_preview") or "")[:160]],
            raw_llm={"meta": meta},
        )
    return _parse_assert_result(raw)


# ============== HITL Composer (Step 5) ==============


_HITL_FALLBACK_TITLES = {
    "confirm":          "需要您确认下一步操作",
    "input_text":       "需要您输入信息",
    "choice_single":    "需要您从下列选项中选择",
    "choice_multiple":  "需要您从下列选项中多选",
    "upload_image":     "需要您上传一张参考截图",
    "acknowledge":      "请知悉以下事项",
}


def _hitl_fallback(kind: str, reason: str, raw_meta: Optional[dict[str, Any]] = None) -> HitlComposerResult:
    """LLM 不可用 / 解析失败时给一个能用的兜底，避免阻塞整轮回归。"""
    title = _HITL_FALLBACK_TITLES.get(kind, "需要您介入")
    body = f"系统暂时无法生成针对性话术（原因：{reason}）。请根据当前用例上下文做出选择。"
    constraints: dict[str, Any] = {}
    options: list[dict[str, Any]] = []
    if kind == "input_text":
        constraints = {"min_len": 1, "max_len": 200}
    elif kind == "upload_image":
        constraints = {"accept_mime": ["image/png", "image/jpeg"], "max_size_kb": 4096}
    elif kind in ("choice_single", "choice_multiple"):
        options = [
            {"id": "ok", "label": "继续", "hint": None},
            {"id": "cancel", "label": "终止", "hint": None},
        ]
    return HitlComposerResult(
        title=title,
        body=body,
        options=options,
        constraints=constraints,
        default_timeout_sec=300,
        ai_reasoning=f"fallback: {reason}",
        raw_llm={"meta": raw_meta or {}},
        parse_warnings=[reason],
    )


def _parse_hitl_composer(raw: dict[str, Any], kind: str) -> HitlComposerResult:
    warnings: list[str] = []
    title = str(raw.get("title") or "").strip()
    body = str(raw.get("body") or "").strip()
    if not title:
        warnings.append("title 为空 → 用兜底")
        title = _HITL_FALLBACK_TITLES.get(kind, "需要您介入")
    if not body:
        warnings.append("body 为空")
        body = "请根据当前用例上下文做出选择。"
    options_raw = raw.get("options") or []
    options: list[dict[str, Any]] = []
    if isinstance(options_raw, list):
        for opt in options_raw:
            if isinstance(opt, dict) and opt.get("id"):
                options.append({
                    "id": str(opt.get("id")),
                    "label": str(opt.get("label") or opt.get("id")),
                    "hint": str(opt.get("hint") or "") or None,
                })
    if kind in ("choice_single", "choice_multiple") and not options:
        warnings.append(f"{kind} 但模型没给 options → 兜底两项 ok/cancel")
        options = [
            {"id": "ok", "label": "确认", "hint": None},
            {"id": "cancel", "label": "取消", "hint": None},
        ]
    constraints = raw.get("constraints") or {}
    if not isinstance(constraints, dict):
        warnings.append("constraints 非 dict → 丢弃")
        constraints = {}
    try:
        timeout = int(raw.get("default_timeout_sec") or 300)
    except (TypeError, ValueError):
        warnings.append("default_timeout_sec 非整数 → 用 300")
        timeout = 300
    timeout = max(1, min(3600, timeout))
    return HitlComposerResult(
        title=title[:60],
        body=body[:600],
        options=options,
        constraints=constraints,
        default_timeout_sec=timeout,
        ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "（模型未给出 reasoning）",
        raw_llm=raw,
        parse_warnings=warnings,
    )


def compose_hitl_prompt(
    *,
    hitl_kind: str,
    case_summary: str,
    event_dict: dict[str, Any],
    device_brief: Optional[dict[str, Any]] = None,
    provider_id: Optional[str] = None,
    timeout_sec: int = 45,
) -> HitlComposerResult:
    """HITL_PROMPT_COMPOSER：给即将弹出的 HITL 写出标题/正文/选项。

    永远返回 HitlComposerResult；LLM 不可用 → 兜底。
    """
    kind = (hitl_kind or "confirm").strip()
    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        SLog.w(TAG, f"HITL composer 不可用，使用兜底：{gate.get('reason')}")
        return _hitl_fallback(kind, str(gate.get("reason") or "no provider"))

    messages = P.build_hitl_composer_messages(
        hitl_kind=kind,
        case_summary=case_summary,
        event_dict=event_dict,
        device_brief=device_brief,
    )
    raw, meta = _chat(
        job="hitl-composer",
        provider=provider,
        messages=messages,
        temperature=0.2,
        max_tokens=800,
        timeout_sec=timeout_sec,
    )
    if raw is None:
        SLog.w(TAG, f"HITL composer LLM 解析失败，使用兜底：{meta.get('error')}")
        return _hitl_fallback(kind, "LLM 返回空 / JSON 解析失败", raw_meta=meta)
    return _parse_hitl_composer(raw, kind)


# ============== PERSONA_TASK (Step 7) ==============


def _parse_persona_sub_events(
    raw_events: list[Any],
    menu_index: dict[str, set[str]],
) -> tuple[list[PlanEvent], list[str]]:
    """对 sub_events 做与 PlanResult 相同的硬校验：capability_id ∈ menu，executor ∈ impls。"""
    out: list[PlanEvent] = []
    warnings: list[str] = []
    if not isinstance(raw_events, list):
        return out, ["sub_events 非数组，全部丢弃"]

    for idx, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            warnings.append(f"sub_events[{idx}] 非对象，跳过")
            continue
        cap = str(raw.get("capability_id") or "")
        if not cap or cap not in menu_index:
            warnings.append(f"sub_events[{idx}] capability_id={cap!r} 不在菜单，丢弃")
            continue
        ex = str(raw.get("expected_executor") or "")
        allowed = menu_index.get(cap, set())
        if ex and ex not in allowed:
            warnings.append(
                f"sub_events[{idx}] expected_executor={ex!r} 不在 cap={cap!r} 的 impls"
                f" {sorted(allowed)} → 清空，让 router 自选"
            )
            ex = ""
        try:
            seq = int(raw.get("seq") or (idx + 1))
        except (TypeError, ValueError):
            seq = idx + 1
        ev = PlanEvent(
            seq=seq,
            case_step_index=raw.get("case_step_index"),
            capability_id=cap,
            event_kind=str(raw.get("event_kind") or cap),
            params=raw.get("params") or {},
            needs_vlm=bool(raw.get("needs_vlm")),
            expected_executor=ex,
            fallback_executors=[
                str(x) for x in (raw.get("fallback_executors") or [])
                if isinstance(x, str) and x in allowed
            ],
            ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "（模型未给出 reasoning）",
            label=str(raw.get("label") or "")[:120],
        )
        out.append(ev)
    return out, warnings


def expand_persona_task(
    *,
    task_description: str,
    run_context: RunContext,
    template_id: str = "PERSONA_TASK",
    params: Optional[dict[str, Any]] = None,
    ai_hint: str = "",
    image_base64: str = "",
    image_mime: str = "image/jpeg",
    provider_id: Optional[str] = None,
    timeout_sec: int = 90,
    max_sub_events: int = 12,
) -> PersonaExpandResult:
    """PERSONA_TASK：把高层系统任务展开为可由 Remote 执行的拟人化子事件。

    永远返回 PersonaExpandResult（不抛异常）；任何失败 → mode=decline + 详细原因。
    """
    menu = available_menu_brief(run_context)
    if not menu:
        return PersonaExpandResult(
            mode="decline",
            ai_reasoning="capability_menu 为空，无法展开任何子事件。",
            decline_reason="empty capability menu; persona expand 不可行",
        )
    menu_index = _build_menu_index(menu)

    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        return PersonaExpandResult(
            mode="decline",
            ai_reasoning=f"未启用 AI：{gate.get('reason')}",
            decline_reason=f"AI provider unavailable: {gate.get('reason')}",
        )

    messages = P.build_persona_task_messages(
        task_description=task_description,
        device_brief=run_context.to_prompt_brief(),
        menu=menu,
        params=params or {},
        ai_hint=ai_hint,
        image_base64=image_base64,
        image_mime=image_mime,
        template_id=template_id or "PERSONA_TASK",
    )
    raw, meta = _chat(
        job="persona-task",
        provider=provider,
        messages=messages,
        temperature=0.1,
        max_tokens=3072,
        timeout_sec=timeout_sec,
    )
    if raw is None:
        SLog.w(
            TAG,
            f"persona expand LLM failed task={task_description!r} "
            f"err={meta.get('error') or meta.get('finish_reason')!r}",
        )
        return PersonaExpandResult(
            mode="decline",
            ai_reasoning="LLM 返回空 / JSON 解析失败",
            decline_reason=str(meta.get("error") or meta.get("content_preview") or "")[:240],
            raw_llm={"meta": meta},
        )

    mode = str(raw.get("mode") or "expand").lower()
    if mode == "decline":
        return PersonaExpandResult(
            mode="decline",
            ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "(模型未给出 reasoning)",
            confidence=float(raw.get("confidence") or 0.0),
            decline_reason=str(raw.get("decline_reason") or "").strip() or "模型选择 decline",
            needs_human=bool(raw.get("needs_human")),
            raw_llm=raw,
        )

    sub_events, warnings = _parse_persona_sub_events(raw.get("sub_events") or [], menu_index)
    if max_sub_events and len(sub_events) > max_sub_events:
        warnings.append(f"sub_events 数量 {len(sub_events)} > 上限 {max_sub_events}，截断")
        sub_events = sub_events[:max_sub_events]
    # 重排 seq 保证 1..N 连续
    for new_seq, ev in enumerate(sub_events, start=1):
        ev.seq = new_seq

    if not sub_events:
        return PersonaExpandResult(
            mode="decline",
            ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "模型展开为空",
            confidence=float(raw.get("confidence") or 0.0),
            decline_reason="LLM 返回的 sub_events 全被校验丢弃",
            needs_human=bool(raw.get("needs_human")),
            raw_llm=raw,
            parse_warnings=warnings,
        )

    return PersonaExpandResult(
        mode="expand",
        ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "(模型未给出 reasoning)",
        sub_events=sub_events,
        confidence=max(0.0, min(1.0, float(raw.get("confidence") or 0.5))),
        needs_human=bool(raw.get("needs_human")),
        raw_llm=raw,
        parse_warnings=warnings,
    )


# ============== Agent 执行引擎（目标导向闭环，仅 adb 通道） ==============


_PROCESS_KIND_RE = re.compile(r"加载占位|加载中|生成中|切换中|转圈|进度未|占位符|占位图")


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return []


def _checkpoint_kind(raw_kind: str, description: str) -> str:
    kind = (raw_kind or "").strip().lower()
    if kind in {"process", "transient", "mid"}:
        return "process"
    if kind in {"terminal", "final", "end"}:
        return "terminal"
    return "process" if _PROCESS_KIND_RE.search(description or "") else "terminal"


def _split_process_from_success(
    success: str, cps: list[CaseCheckpoint],
) -> tuple[list[CaseCheckpoint], str]:
    """终态标准里若混入过程态句子，挪到 process 检查点，并给终态补一句。"""
    text = (success or "").strip()
    if not text:
        return cps, text
    parts = [p.strip() for p in re.split(r"[。；;\n]+", text) if p.strip()]
    keep: list[str] = []
    moved: list[str] = []
    for part in parts:
        if _PROCESS_KIND_RE.search(part):
            moved.append(part)
        else:
            keep.append(part)
    extra = list(cps)
    for i, desc in enumerate(moved, 1):
        if any(desc in c.description or c.description in desc for c in extra):
            for c in extra:
                if desc in c.description or c.description in desc:
                    c.kind = "process"
            continue
        extra.append(CaseCheckpoint(id=f"cp_p{i}", description=desc, kind="process"))
    success_out = "。".join(keep) if keep else text
    if moved:
        success_out = success_out.rstrip("。") + "。终态只判定完成后的稳定界面，不要把加载/占位/生成中/切换中当作最终失败理由。"
    return extra, success_out


def _checkpoints_from_expected(case_spec: CaseSpec) -> list[CaseCheckpoint]:
    """检查点 = 有编号的预期原文；缺号步骤不生成检查点。"""
    from server.services.shared.semantic.case_text_semantic_service import parse_numbered_items_rules

    rows: list[tuple[int, str]] = []
    seen: set[str] = set()
    for step in case_spec.steps or []:
        t = (step.expected or "").strip()
        n = int(getattr(step, "index", 0) or 0)
        if t and n and t not in seen:
            seen.add(t)
            rows.append((n, t))
    if not rows:
        raw = (case_spec.expected or "").strip()
        for it in parse_numbered_items_rules(raw) if raw else []:
            t = str(it.get("text") or "").strip()
            n = int(it.get("num") or 0) or (len(rows) + 1)
            if t and t not in seen:
                seen.add(t)
                rows.append((n, t))
    return [
        CaseCheckpoint(
            id=f"cp{n}",
            description=desc,
            kind=_checkpoint_kind("", desc),
        )
        for n, desc in rows
    ]


def _case_scene_blob(
    *,
    name: str = "",
    steps: str = "",
    expected: str = "",
    precondition: str = "",
) -> str:
    return "\n".join([
        str(name or "").strip(),
        str(precondition or "").strip(),
        str(steps or "").strip(),
        str(expected or "").strip(),
    ])


def _case_scene_key(blob: str) -> str:
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _steps_from_spec(case_spec: CaseSpec) -> str:
    raw = case_spec.raw_row or {}
    if isinstance(raw, dict):
        text = str(raw.get("steps_raw") or "").strip()
        if text:
            return text
    lines: list[str] = []
    for i, step in enumerate(case_spec.steps or [], 1):
        inst = str(getattr(step, "instruction", "") or "").strip()
        if inst:
            lines.append(f"{i}. {inst}")
    return "\n".join(lines)


def classify_case_scene(
    case_spec: Optional[CaseSpec] = None,
    *,
    name: str = "",
    steps: str = "",
    expected: str = "",
    precondition: str = "",
    provider_id: Optional[str] = None,
    timeout_sec: int = 30,
    run_context: Optional[RunContext] = None,
) -> dict[str, Any]:
    """开跑前理解登录闸门、设备需求和前置 kind。按原文缓存；失败则 skip，不自动登录、不占 Web。"""
    from server.services.runtime.session_gate import clamp_case_scene, fallback_case_scene

    if case_spec is not None:
        name = name or (case_spec.name or "")
        expected = expected or (case_spec.expected or "")
        precondition = precondition or (case_spec.preconditions or "")
        steps = steps or _steps_from_spec(case_spec)
    blob = _case_scene_blob(
        name=name, steps=steps, expected=expected, precondition=precondition,
    )
    key = _case_scene_key(blob)
    cached = _SCENE_CACHE.get(key)
    if cached:
        scene = clamp_case_scene(cached)
        if run_context is not None:
            run_context.case_scene = dict(scene)
        return scene

    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        scene = fallback_case_scene(
            f"未启用 AI：{gate.get('reason')}，不自动登录",
            precondition=precondition,
        )
        _put_scene_cache(key, scene, run_context)
        return scene

    messages = P.build_case_scene_messages(
        name=name, precondition=precondition, steps=steps, expected=expected,
    )
    raw, meta = _chat(
        job="case-scene",
        provider=provider,
        messages=messages,
        temperature=0.1,
        max_tokens=1200,
        timeout_sec=timeout_sec,
        response_schema=P.CASE_SCENE_JSON_SCHEMA,
    )
    if not isinstance(raw, dict):
        err = str((meta or {}).get("error") or "").strip() or "场景理解返回空"
        scene = fallback_case_scene(f"{err}，不自动登录", precondition=precondition)
        SLog.w(TAG, f"classify_case_scene failed err={err!r}")
        _put_scene_cache(key, scene, run_context)
        return scene

    scene = clamp_case_scene(raw)
    SLog.i(
        TAG,
        f"classify_case_scene prep={scene.get('session_prep')} "
        f"required={scene.get('required_session')} "
        f"device={scene.get('device_need')} how={scene.get('how')} "
        f"reason={(scene.get('reason') or '')[:80]!r}",
    )
    _put_scene_cache(key, scene, run_context)
    return scene


def _put_scene_cache(
    key: str, scene: dict[str, Any], run_context: Optional[RunContext],
) -> None:
    if len(_SCENE_CACHE) >= _SCENE_CACHE_MAX:
        _SCENE_CACHE.clear()
    _SCENE_CACHE[key] = dict(scene)
    if run_context is not None:
        run_context.case_scene = dict(scene)


def extract_goal(
    case_spec: CaseSpec,
    *,
    run_context: Optional[RunContext] = None,
    provider_id: Optional[str] = None,
    timeout_sec: int = 60,
) -> CaseGoal:
    """检查点优先用用例预期；没有预期才让模型抽。"""
    expected_cps = _checkpoints_from_expected(case_spec)
    expected_text = (case_spec.expected or "").strip()
    if expected_cps:
        success = expected_text or "\n".join(c.description for c in expected_cps)
        goal_text = (case_spec.name or success or "完成用例").strip()
        SLog.i(
            TAG,
            f"extract_goal from expected case={case_spec.case_id} cps={len(expected_cps)}",
        )
        return CaseGoal(
            case_id=case_spec.case_id,
            goal=goal_text,
            checkpoints=expected_cps,
            success_criteria=success,
            ai_reasoning="检查点来自用例预期原文，未改写",
        )

    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        return CaseGoal(
            case_id=case_spec.case_id,
            goal=(case_spec.expected or case_spec.name or "完成用例").strip(),
            ai_reasoning=f"未启用 AI：{gate.get('reason')}（用兜底 goal）",
            parse_warnings=["provider unavailable"],
        )
    messages = P.build_goal_extract_messages(case_spec=case_spec)
    raw, meta = _chat(
        job="goal-extract",
        provider=provider, messages=messages,
        temperature=0.1, max_tokens=1024, timeout_sec=timeout_sec,
    )
    if raw is None:
        SLog.w(TAG, f"extract_goal LLM failed case={case_spec.case_id} err={meta.get('error')!r}")
        return CaseGoal(
            case_id=case_spec.case_id,
            goal=(case_spec.expected or case_spec.name or "完成用例").strip(),
            ai_reasoning="LLM 返回空/解析失败，用兜底 goal",
            raw_llm={"meta": meta},
            parse_warnings=["llm failed"],
        )
    cps: list[CaseCheckpoint] = []
    for idx, cp in enumerate(raw.get("checkpoints") or [], start=1):
        if isinstance(cp, dict) and (cp.get("description") or "").strip():
            desc = str(cp.get("description")).strip()
            cps.append(CaseCheckpoint(
                id=str(cp.get("id") or f"cp{idx}"),
                description=desc,
                kind=_checkpoint_kind(str(cp.get("kind") or ""), desc),
            ))
    goal_text = str(raw.get("goal") or "").strip() or (case_spec.expected or case_spec.name or "完成用例").strip()
    success = str(raw.get("success_criteria") or "").strip() or goal_text
    cps, success = _split_process_from_success(success, cps)
    return CaseGoal(
        case_id=case_spec.case_id,
        goal=goal_text,
        checkpoints=cps,
        success_criteria=success,
        ai_reasoning=str(raw.get("ai_reasoning") or "").strip() or "（模型未给出 reasoning）",
        raw_llm=raw,
    )


def _parse_agent_decision(raw: dict[str, Any], width: int, height: int) -> AgentDecision:
    warnings: list[str] = []
    status = (raw.get("status") or "continue").strip().lower()
    if status not in {"continue", "done", "give_up", "ask_human"}:
        warnings.append(f"unknown status={status!r} → continue")
        status = "continue"
    action: Optional[AgentAction] = None
    raw_action = raw.get("action")
    if isinstance(raw_action, dict) and raw_action.get("capability_id"):
        params = raw_action.get("params") if isinstance(raw_action.get("params"), dict) else {}

        def _to_px(v, dim):
            # 约定 0-1000 归一化；>1000 视为模型误给的绝对像素（兜底）
            try:
                fv = float(v)
            except (TypeError, ValueError):
                return v
            px = fv if fv > 1000 else (fv / 1000.0 * dim)
            return max(0, min(dim, int(round(px))))

        for kx, ky in (("x", "y"), ("from_x", "from_y"), ("to_x", "to_y")):
            if kx in params and width > 0:
                params[kx] = _to_px(params[kx], width)
            if ky in params and height > 0:
                params[ky] = _to_px(params[ky], height)
        action = AgentAction(capability_id=str(raw_action.get("capability_id")), params=params)
    if status in {"continue", "ask_human"} and action is None:
        warnings.append(f"status={status} 但无有效 action")
    subflow = str(raw.get("subflow") or "none").strip().lower() or "none"
    if subflow in {"create", "publish", "create_publish", "generating"}:
        subflow = "create_publish"
    elif subflow not in {"none", "create_publish"}:
        subflow = "none"
    published: dict[str, Any] = {}
    raw_pub = raw.get("published")
    if isinstance(raw_pub, dict):
        published = {k: v for k, v in raw_pub.items() if v not in (None, "", [])}
    elif isinstance(raw_pub, str) and raw_pub.strip() and raw_pub.strip().lower() not in {"null", "none"}:
        published = {"note": raw_pub.strip()}
    return AgentDecision(
        thought=str(raw.get("thought") or "").strip(),
        action=action,
        expected_after=str(raw.get("expected_after") or "").strip(),
        status=status,  # type: ignore[arg-type]
        confidence=max(0.0, min(1.0, float(raw.get("confidence") or 0.0))),
        remember=_as_str_list(raw.get("remember")),
        checkpoint_ids=_as_str_list(raw.get("checkpoint_ids") or raw.get("checkpoints")),
        knowledge_ids=_as_str_list(raw.get("knowledge_ids") or raw.get("knowledge_id")),
        subflow=subflow,
        published=published,
        raw_llm=raw,
        parse_warnings=warnings,
    )


def decide_next_action(
    *,
    goal: str,
    checkpoints_block: str,
    run_context: RunContext,
    history_block: str,
    width: int,
    height: int,
    image_base64: str,
    image_mime: str = "image/png",
    hierarchy_text: str = "",
    success_criteria: str = "",
    memory_block: str = "",
    knowledge_hint: str = "",
    session_block: str = "",
    provider_id: Optional[str] = None,
    timeout_sec: int = 90,
    menu_ids: Optional[set[str]] = None,
) -> AgentDecision:
    """看图决定下一步一个动作（D2 直接出坐标 / D3 每步看图）。永远返回 AgentDecision。"""
    menu = available_menu_brief(run_context, kind="agent")
    if not menu:
        return AgentDecision(status="give_up", thought="capability_menu 为空（连通性丢失）",
                             parse_warnings=["empty menu"])
    menu = [c for c in menu if str(c.get("id") or "") not in {"assert_visual", "assert_goal", "assert"}]
    if menu_ids:
        allow = {str(x) for x in menu_ids}
        menu = [c for c in menu if str(c.get("id") or "") in allow]
        if not menu:
            return AgentDecision(status="give_up", thought="备会话菜单为空（连通性丢失）",
                                 parse_warnings=["empty session menu"])
    from server.services.plugins.tool_schema import tools_chat_payload, tools_for_menu

    tool_payload = tools_chat_payload(tools_for_menu(menu))
    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        return AgentDecision(status="ask_human", thought=f"未启用 AI 视觉：{gate.get('reason')}",
                             parse_warnings=["provider unavailable"])
    accounts_brief = str(getattr(run_context, "accounts_brief", "") or "").strip()
    messages = P.build_agent_do_messages(
        goal=goal,
        checkpoints_block=checkpoints_block,
        device_brief=run_context.to_prompt_brief(),
        menu=menu,
        history_block=history_block,
        width=width,
        height=height,
        image_base64=image_base64,
        image_mime=image_mime,
        hierarchy_text=hierarchy_text,
        target_package=str(getattr(run_context, "target_package", "") or ""),
        success_criteria=success_criteria,
        memory_block=memory_block,
        knowledge_hint=knowledge_hint,
        session_block=session_block,
        accounts_brief=accounts_brief,
    )

    # 给前端展示：我们喂给模型的“文本块/上下文”（不直接回传超大 image_base64）。
    llm_input_debug = {
        "goal": goal,
        "checkpoints_block": checkpoints_block,
        "history_block": history_block,
        "knowledge_hint": knowledge_hint,
        "session_block": session_block,
        "accounts_brief": accounts_brief,
        "memory_block": memory_block,
        "success_criteria": success_criteria,
        "device_brief": run_context.to_prompt_brief(),
        "target_package": str(getattr(run_context, "target_package", "") or ""),
        "capability_menu": menu,
        "tools": list(tool_payload.get("tools") or []),
        "image": {"width": width, "height": height, "mime": image_mime, "note": "image_base64 omitted"},
    }
    raw, meta = _chat(
        job="agent-decide",
        provider=provider, messages=messages,
        # 省略该字段时豆包默认仍是 4096；decide JSON 通常 200 token，2048 足够。
        # 再放大只会让空白熔断更久。空白输出改由流式早停处理。
        temperature=0.1, max_tokens=2048, timeout_sec=timeout_sec,
        json_mode=not bool(tool_payload.get("tools")),
        extra_payload=tool_payload or None,
    )
    if raw is None:
        err = str(meta.get("error") or "").strip() or "LLM 返回空/解析失败"
        SLog.w(TAG, f"decide_next_action LLM failed err={err!r}")
        return AgentDecision(
            status="give_up",
            thought=err[:200],
            raw_llm={"llm_input": llm_input_debug, "llm_output": None, "meta": meta},
            parse_warnings=["llm failed"],
        )

    decision = _parse_agent_decision(raw, width, height)
    allow_ids = {str(c.get("id") or "") for c in menu}
    allow_ids.add("human_input_text")
    if decision.action and str(decision.action.capability_id or "") not in allow_ids:
        cid = decision.action.capability_id
        decision.parse_warnings.append(f"capability_id={cid!r} 不在菜单，已丢弃")
        decision.action = None
    # 覆盖 raw_llm：既保留输出，也带上“喂给模型的输入”用于 UI 可视化溯源。
    decision.raw_llm = {"llm_input": llm_input_debug, "llm_output": raw, "meta": meta}
    return decision


def decide_restart_app(
    *,
    goal: str,
    preconditions: str = "",
    target_package: str = "",
    target_app_name: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
    provider_id: Optional[str] = None,
    timeout_sec: int = 60,
) -> tuple[bool, str]:
    """用例开场：看图决定是否 force-stop + launch 目标应用。失败时默认不重启。"""
    if not image_base64:
        return False, "无截图，跳过重启判断"
    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        return False, f"未启用 AI：{gate.get('reason')}，跳过重启"
    messages = P.build_restart_decide_messages(
        goal=goal,
        preconditions=preconditions,
        target_package=target_package,
        target_app_name=target_app_name,
        image_base64=image_base64,
        image_mime=image_mime,
    )
    raw, meta = _chat(
        job="agent-restart",
        provider=provider, messages=messages,
        temperature=0.1, max_tokens=512, timeout_sec=timeout_sec,
    )
    if not isinstance(raw, dict):
        SLog.w(TAG, f"decide_restart_app LLM failed err={meta.get('error')!r}")
        return False, "LLM 返回空/解析失败，跳过重启"
    restart = raw.get("restart")
    if isinstance(restart, str):
        restart = restart.strip().lower() in {"true", "1", "yes"}
    thought = str(raw.get("thought") or "").strip() or "（未说明）"
    return bool(restart), thought


def _parse_inspect_session_raw(raw: dict[str, Any]) -> dict[str, Any]:
    session = str(raw.get("session") or "unknown").strip().lower()
    if session not in {"logged_out", "logged_in", "unknown"}:
        session = "unknown"
    identity = str(raw.get("identity") or "unknown").strip().lower()
    if identity not in {"match", "mismatch", "unknown"}:
        identity = "unknown"
    nxt = str(raw.get("next") or "keep").strip().lower()
    if nxt not in {"keep", "logout", "login", "switch", "human"}:
        nxt = "keep"
    probe = raw.get("probe")
    if isinstance(probe, str):
        probe = probe.strip().lower() in {"true", "1", "yes"}
    seen = str(raw.get("seen") or "").strip()[:240]
    reason = str(raw.get("reason") or "").strip()[:240] or "（未说明）"
    if session == "unknown" and nxt == "human" and not re.search(
        r"登录|注册|验证码|退出|昵称|手机号|账号", f"{seen} {reason}"
    ):
        nxt = "keep"
    return {
        "session": session,
        "identity": identity,
        "seen": seen,
        "probe": bool(probe),
        "next": nxt,
        "reason": reason,
        "ok": True,
    }


def inspect_session(
    *,
    required_session: str = "",
    knowledge_hint: str = "",
    accounts_brief: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
    provider_id: Optional[str] = None,
    timeout_sec: int = 60,
) -> dict[str, Any]:
    """观察当前屏登录态。空输出/解析失败重试；耗尽后 ok=False，不当成功。"""
    empty = {
        "session": "unknown",
        "identity": "unknown",
        "seen": "",
        "probe": False,
        "next": "keep",
        "reason": "未观察",
        "ok": False,
    }
    if not image_base64:
        empty["reason"] = "无截图，跳过会话观察"
        return empty
    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        empty["reason"] = f"未启用 AI：{gate.get('reason')}"
        return empty
    messages = P.build_inspect_session_messages(
        required_session=required_session,
        knowledge_hint=knowledge_hint,
        accounts_brief=accounts_brief,
        image_base64=image_base64,
        image_mime=image_mime,
    )
    last_err = "LLM 返回空/解析失败"
    attempts = 3
    for attempt in range(1, attempts + 1):
        raw, meta = _chat(
            job="inspect-session",
            provider=provider, messages=messages,
            temperature=0.1, max_tokens=512, timeout_sec=timeout_sec,
        )
        if isinstance(raw, dict) and str(raw.get("session") or "").strip():
            row = _parse_inspect_session_raw(raw)
            if attempt > 1:
                SLog.i(TAG, f"inspect_session recovered on attempt {attempt}")
            return row
        last_err = str((meta or {}).get("error") or "").strip() or "LLM 返回空/解析失败"
        SLog.w(TAG, f"inspect_session attempt {attempt}/{attempts} failed err={last_err!r}")
        if attempt < attempts:
            time.sleep(0.4)
    empty["reason"] = last_err
    return empty


def _parse_inspect_env_raw(raw: dict[str, Any]) -> dict[str, Any]:
    from server.services.runtime.env_gate import canon_run_env

    env_raw = str(raw.get("env") or "").strip()
    env = canon_run_env(env_raw) or ("unknown" if env_raw.lower() == "unknown" or not env_raw else env_raw)
    if env not in ("dev", "test", "pre", "prod", "unknown"):
        env = "unknown"
    return {
        "env": env,
        "seen": str(raw.get("seen") or "").strip()[:200],
        "reason": str(raw.get("reason") or "").strip()[:240],
        "ok": True,
    }


def inspect_env(
    *,
    wanted_env: str = "",
    wanted_label: str = "",
    knowledge_hint: str = "",
    image_base64: str = "",
    image_mime: str = "image/png",
    provider_id: Optional[str] = None,
    timeout_sec: int = 45,
) -> dict[str, Any]:
    """观察当前屏客户端环境。空输出当 unknown，不当成功匹配。"""
    empty = {
        "env": "unknown",
        "seen": "",
        "reason": "未观察",
        "ok": False,
    }
    if not image_base64:
        empty["reason"] = "无截图，无法查看环境"
        return empty
    provider, gate = resolve_regression_provider(provider_id)
    if provider is None:
        empty["reason"] = f"未启用 AI：{gate.get('reason')}"
        return empty
    messages = P.build_inspect_env_messages(
        wanted_env=wanted_env,
        wanted_label=wanted_label,
        knowledge_hint=knowledge_hint,
        image_base64=image_base64,
        image_mime=image_mime,
    )
    last_err = "LLM 返回空/解析失败"
    attempts = 3
    for attempt in range(1, attempts + 1):
        raw, meta = _chat(
            job="inspect-env",
            provider=provider, messages=messages,
            temperature=0.1, max_tokens=400, timeout_sec=timeout_sec,
        )
        if isinstance(raw, dict) and str(raw.get("env") or "").strip():
            row = _parse_inspect_env_raw(raw)
            if attempt > 1:
                SLog.i(TAG, f"inspect_env recovered on attempt {attempt}")
            return row
        last_err = str((meta or {}).get("error") or "").strip() or "LLM 返回空/解析失败"
        SLog.w(TAG, f"inspect_env attempt {attempt}/{attempts} failed err={last_err!r}")
        if attempt < attempts:
            time.sleep(0.4)
    empty["reason"] = last_err
    return empty
