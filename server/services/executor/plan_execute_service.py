# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Plan + Execute 共享编排：离屏阻断、前置弹窗后继续规划。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from script.log import SLog

from server.services import app_automation_service as aas
from server.services import copilot_service as cs
from server.services.executor.execute_steps import execute_steps
from server.services.shared.execution_profile import ExecutionProfile, resolve_execution_profile

TAG = "PlanExecute"

FOREGROUND_DRIFT_BLOCK_KINDS = frozenset({"click", "input", "swipe"})

MAX_FOREGROUND_DRIFT_REPLANS = 2
# 单条用例步骤内 Plan→Execute 安全上限（仅防死循环；是否继续由 AI plan_complete 决定）。
MAX_COMMAND_PLAN_SAFETY_ROUNDS = 32

_OVERLAY_DISMISS_LABELS = (
    "同意",
    "不同意",
    "允许",
    "拒绝",
    "关闭",
    "取消",
    "知道了",
    "跳过",
    "暂不",
)
_REMEDIATION_REPLY_MARKERS = (
    "弹窗",
    "协议",
    "权限",
    "阻碍",
    "先点击",
    "先点",
    "继续后续",
    "以便",
    "才能",
    "之前",
    "需要先",
)


_SYSTEM_OVERLAY_PACKAGE_KEYWORDS = (
    "permissioncontroller",
    "packageinstaller",
    "securitycenter",
    "lbe.security",
    "com.android.settings",
)
_PERMISSION_REMEDIATION_HINTS = (
    "始终允许",
    "使用时允许",
    "仅在使用",
    "一律允许",
    "授予",
    "权限",
    "允许",
    "拒绝",
    "不同意",
)


def is_system_overlay_foreground_package(package: str) -> bool:
    pkg = (package or "").strip().lower()
    if not pkg:
        return False
    return any(key in pkg for key in _SYSTEM_OVERLAY_PACKAGE_KEYWORDS)


def is_overlay_remediation_step(step: Optional[Dict[str, Any]]) -> bool:
    """当前 step 是否为关闭系统/应用弹窗的前置补救动作。"""
    if not step:
        return False
    label = (step.get("label") or "").strip()
    summary = (step.get("summary") or "").strip()
    blob = f"{label} {summary}"
    if any(lb in blob for lb in _OVERLAY_DISMISS_LABELS):
        return True
    if any(hint in blob for hint in _PERMISSION_REMEDIATION_HINTS):
        return True
    if any(w in blob for w in ("隐私", "协议", "弹窗")):
        return True
    return False


def should_block_step_on_foreground_drift(
    kind: str,
    step: Optional[Dict[str, Any]] = None,
    *,
    fg_result: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    离屏时是否阻断 UI 动作。

    仅阻断「业务目标点击」（如一键登录、输入邮箱）——此时坐标易基于旧截图失效。
    弹窗/权限/协议等前置补救点击（同意、始终允许等）一律放行，即使前台是
    微信/系统权限页（com.tencent.mm、securitycenter 等）。
    """
    k = (kind or "").strip().lower()
    if k not in FOREGROUND_DRIFT_BLOCK_KINDS:
        return False
    if k == "click" and step and step.get("overlay_guard"):
        return False
    if fg_result and fg_result.get("drift") and is_overlay_remediation_step(step):
        actual_pkg = str(fg_result.get("foreground_before") or "")
        SLog.i(
            TAG,
            f"allow drift remediation click pkg={actual_pkg or '-'} "
            f"step={((step or {}).get('summary') or '')[:48]!r}",
        )
        return False
    return True


def _case_goal_phrases(command: str) -> List[str]:
    """从用例步骤原文提取可匹配的目标短语（去点击/按钮等动词包装）。"""
    cmd = (command or "").strip()
    if not cmd:
        return []
    phrases: List[str] = []
    for q in re.findall(r"[「『\"']([^」』\"']+)[」』\"']", cmd):
        q = q.strip()
        if len(q) >= 2:
            phrases.append(q)
    core = re.sub(r"^(点击|点一下|点按|tap|click)\s*", "", cmd, flags=re.I).strip()
    core = re.sub(r"(按钮|图标|链接|tab|Tab)$", "", core).strip()
    if len(core) >= 2:
        phrases.append(core)
    # 长短语优先，避免「同意」误匹配「同意并继续」
    return list(dict.fromkeys(sorted(phrases, key=len, reverse=True)))


def step_fulfills_case_command(command: str, steps: List[Dict[str, Any]]) -> bool:
    """规划动作是否就是用例步骤原文要求的目标（即使在弹窗里点也算完成）。"""
    if not steps:
        return False
    phrases = _case_goal_phrases(command)
    if not phrases:
        return False
    primary = steps[0] if isinstance(steps[0], dict) else {}
    label = (primary.get("label") or "").strip()
    summary = (primary.get("summary") or "").strip()
    blob = re.sub(r"\s+", "", f"{label} {summary}")
    for phrase in phrases:
        norm = re.sub(r"\s+", "", phrase)
        if not norm:
            continue
        if norm in blob or norm in label or norm in summary:
            return True
        # 用例「同意并继续」↔ 按钮文案「同意」
        if "同意并继续" in norm and ("同意并继续" in blob or label == "同意" or "同意" in label):
            return True
    return False


def infer_plan_complete(command: str, steps: List[Dict[str, Any]], reply: str = "") -> bool:
    """模型未返回 plan_complete 时，推断本次 plan 是否已完成用例步骤原文目标。"""
    if not steps:
        return False
    if step_fulfills_case_command(command, steps):
        return True

    cmd = (command or "").strip()
    reply_l = (reply or "").strip()
    if any(marker in reply_l for marker in _REMEDIATION_REPLY_MARKERS):
        return False

    primary = steps[0] if isinstance(steps[0], dict) else {}
    label = (primary.get("label") or "").strip()
    summary = (primary.get("summary") or "").strip()
    blob = f"{label} {summary}"

    overlay_hit = any(lb in blob for lb in _OVERLAY_DISMISS_LABELS)
    cmd_mentions_overlay = any(lb in cmd for lb in _OVERLAY_DISMISS_LABELS)
    if overlay_hit and not cmd_mentions_overlay:
        return False

    if cmd and summary and any(w in summary for w in ("隐私", "协议", "权限", "弹窗")):
        if not any(w in cmd for w in ("隐私", "协议", "权限", "弹窗")):
            return False

    if cmd:
        goal_tokens = [t for t in re.split(r"[^\w\u4e00-\u9fff]+", cmd) if len(t) >= 2]
        if goal_tokens and summary:
            if not any(tok in summary for tok in goal_tokens):
                if overlay_hit or any(w in summary for w in ("隐私", "协议", "权限")):
                    return False
    return True


def build_drift_replan_context(
    *,
    command: str,
    blocked_result: Dict[str, Any],
    previous_plan: Dict[str, Any],
    attempt: int,
    expected_package: str = "",
) -> Dict[str, Any]:
    blocked_idx = int(blocked_result.get("index") or 0)
    steps = previous_plan.get("steps") or []
    blocked_step = steps[blocked_idx] if blocked_idx < len(steps) else None

    return {
        "attempt": attempt,
        "reason": "foreground_drift_blocked",
        "command": command,
        "drift_note": blocked_result.get("foreground_note") or blocked_result.get("msg") or "",
        "expected_package": expected_package,
        "actual_package": blocked_result.get("foreground_before") or "",
        "actual_app_name": blocked_result.get("foreground_app_name") or "",
        "blocked_step_index": blocked_idx,
        "blocked_step_summary": blocked_result.get("summary") or "",
        "blocked_step_kind": blocked_result.get("kind") or "",
        "previous_reply": previous_plan.get("reply") or previous_plan.get("display_reply") or "",
        "previous_step": {
            k: v
            for k, v in (blocked_step or {}).items()
            if k not in ("data",)
        },
        "screenshot_at_block": blocked_result.get("screenshot_before") or "",
    }


def build_goal_continue_replan_context(
    *,
    command: str,
    previous_plan: Dict[str, Any],
    executed_results: List[Dict[str, Any]],
    attempt: int,
) -> Dict[str, Any]:
    """前置弹窗/阻碍动作执行成功后，构造继续完成原用例步骤的上下文。"""
    executed = next((r for r in reversed(executed_results or []) if r.get("ok")), None) or {}
    steps = previous_plan.get("steps") or []
    primary = steps[0] if steps and isinstance(steps[0], dict) else {}
    return {
        "attempt": attempt,
        "reason": "goal_not_complete",
        "command": command,
        "previous_reply": previous_plan.get("reply") or previous_plan.get("display_reply") or "",
        "previous_plan_complete": bool(previous_plan.get("plan_complete")),
        "executed_summary": executed.get("summary") or primary.get("summary") or "",
        "executed_label": executed.get("target_label") or primary.get("label") or "",
        "executed_msg": executed.get("msg") or "",
        "screenshot_after": executed.get("screenshot_after") or executed.get("screenshot_before") or "",
        "previous_step": {k: v for k, v in primary.items() if k not in ("data",)},
    }


def _merge_guard_plan_log(plan_log: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    try:
        from server.services.shared.run_context.regression_run_context import get_ctx
        from server.services.local.overlay.overlay_guard_service import merge_guard_plan_log

        gctx = get_ctx()
        guard_planned = (gctx or {}).get("guard_planned_steps") or []
        if guard_planned:
            plan_log = merge_guard_plan_log(plan_log, guard_planned)
            if gctx is not None:
                gctx["guard_planned_steps"] = []
    except Exception:
        pass
    return plan_log


def _results_for_ok_check(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not results:
        return results
    attempts = [int(r.get("plan_round") or r.get("replan_attempt") or 1) for r in results]
    last_attempt = max(attempts) if attempts else 1
    if last_attempt <= 1:
        return results
    final = [
        r
        for r in results
        if int(r.get("plan_round") or r.get("replan_attempt") or 1) == last_attempt
    ]
    return final or results


def _insert_replan_plan_log_entry(
    plan_log: List[Dict[str, Any]],
    *,
    round_i: int,
    replan_ctx: Dict[str, Any],
) -> None:
    reason = str(replan_ctx.get("reason") or "")
    if reason == "foreground_drift_blocked":
        title = f"离屏重规划 · 第 {round_i} 次"
        summary = replan_ctx.get("drift_note") or "被测应用离屏后重新规划"
        entry_type = "drift_replan"
    else:
        title = f"继续规划 · 第 {round_i} 次"
        summary = ""
        entry_type = "goal_continue_replan"
    plan_log.insert(
        0,
        {
            "type": entry_type,
            "title": title,
            "summary": summary,
            "detail": replan_ctx,
        },
    )


def _finalize_command_block(
    *,
    phase: str,
    command: str,
    plan: Dict[str, Any],
    plan_log: List[Dict[str, Any]],
    results: List[Dict[str, Any]],
    use_overlay_guard: bool,
    drift_replan_count: int = 0,
    goal_continue_count: int = 0,
    replan_history: Optional[List[Dict[str, Any]]] = None,
    goal_incomplete: bool = False,
) -> Dict[str, Any]:
    segment_errors = list(plan.get("segment_errors") or [])
    ok = aas.business_step_results_ok(_results_for_ok_check(results))
    plan_complete = bool(plan.get("plan_complete", True))
    if goal_incomplete or not plan_complete:
        ok = False
    if results and use_overlay_guard:
        guard_fail = any(
            not r.get("ok")
            for r in results
            if (r.get("phase") or "") == "overlay_guard"
            and (r.get("kind") or "") in ("overlay_guard", "click")
        )
        if guard_fail:
            ok = False
    fail_msgs = [r.get("msg") or "" for r in results if not r.get("ok")]
    if segment_errors:
        fail_msgs = segment_errors + fail_msgs
    if goal_incomplete or not plan_complete:
        fail_msgs.insert(
            0,
            "用例步骤未完成：已执行前置弹窗/阻碍动作，尚未达成步骤原文目标",
        )
    thought_meta = {
        "reply": plan.get("display_reply") or plan.get("reply") or "",
        "plan_reply": plan.get("display_reply") or plan.get("reply") or "",
        "knowledge_hints": list(plan.get("knowledge_hints") or []),
        "page_hint": plan.get("page_hint") or "",
        "segment_errors": list(segment_errors),
        "plan_log": plan_log,
        "planner": plan.get("planner") or {},
        "ai_debug": plan.get("ai_debug"),
        "drift_replan_count": drift_replan_count,
        "goal_continue_count": goal_continue_count,
        "replan_history": list(replan_history or []),
        "plan_complete": plan_complete,
    }
    return {
        "phase": phase,
        "command": command,
        "plan_log": plan_log,
        "execute_log": aas.build_execute_log(results),
        "step_results": results,
        "reply": plan.get("reply") or "",
        "ok": ok,
        "msg": "；".join(m for m in fail_msgs if m)[:400],
        "segment_errors": segment_errors,
        "plan_complete": plan_complete,
        "knowledge_hints": list(plan.get("knowledge_hints") or []),
        "page_hint": plan.get("page_hint") or "",
        "thought_meta": thought_meta,
        "planner": plan.get("planner") or {},
        "ai_debug": plan.get("ai_debug"),
        "drift_replan_count": drift_replan_count,
        "goal_continue_count": goal_continue_count,
        "replan_history": list(replan_history or []),
    }


def run_command_block(
    command: str,
    *,
    sn: str,
    platform: str,
    context: Dict[str, Any],
    icon_targets: List[Dict[str, Any]],
    phase: str,
    run_id: str = "",
    profile: Optional[ExecutionProfile] = None,
) -> Dict[str, Any]:
    """
    共享：Plan → Execute 循环。
    - 前置弹窗：执行后 plan_complete=false 时重新截图规划同一用例步骤。
    AI 模式下前台包名仅作为规划上下文，不在执行阶段阻断或触发离屏重规划。
    """
    profile = (
        profile
        or context.get("execution_profile")
        or resolve_execution_profile("case_execution")
    )
    if not command:
        return {
            "phase": phase,
            "command": "",
            "plan_log": [],
            "execute_log": [],
            "step_results": [],
            "ok": True,
        }

    expected_package = str(context.get("package") or "").strip()
    drift_replan_ctx: Optional[Dict[str, Any]] = None
    goal_continue_ctx: Optional[Dict[str, Any]] = None
    merged_plan_log: List[Dict[str, Any]] = []
    merged_results: List[Dict[str, Any]] = []
    replan_history: List[Dict[str, Any]] = []
    drift_replan_count = 0
    goal_continue_count = 0
    plan: Dict[str, Any] = {}
    use_overlay_guard = False

    round_i = 0
    while round_i < MAX_COMMAND_PLAN_SAFETY_ROUNDS:
        round_i += 1
        plan_ctx = {**context, "case_step_text": command}
        if icon_targets:
            plan_ctx["icon_targets"] = icon_targets
        if goal_continue_ctx:
            plan_ctx["goal_continue_replan"] = goal_continue_ctx
        elif drift_replan_ctx:
            plan_ctx["drift_replan"] = drift_replan_ctx

        plan = cs.plan_message(command, sn=sn, context=plan_ctx, channel="case_execution")
        plan_log = aas.build_plan_log(command, plan)
        replan_ctx = goal_continue_ctx or drift_replan_ctx
        if replan_ctx and round_i > 1:
            _insert_replan_plan_log_entry(plan_log, round_i=round_i, replan_ctx=replan_ctx)
        plan_log = _merge_guard_plan_log(plan_log)

        if plan.get("error") or not plan.get("steps"):
            return {
                "phase": phase,
                "command": command,
                "plan_log": merged_plan_log + plan_log,
                "execute_log": aas.build_execute_log(merged_results),
                "step_results": merged_results,
                "ok": False,
                "msg": plan.get("reply") or plan.get("error") or "规划失败",
                "drift_replan_count": drift_replan_count,
                "goal_continue_count": goal_continue_count,
                "replan_history": replan_history,
            }

        planner_mode = str((plan.get("planner") or {}).get("mode") or profile.mode or "local").lower()
        use_overlay_guard = (
            planner_mode != "ai"
            and not bool(context.get("skip_overlay_guard"))
        )
        results = execute_steps(
            plan.get("steps") or [],
            sn=sn,
            platform=platform,
            icon_targets=icon_targets,
            run_id=run_id,
            capture_screenshots=bool(run_id),
            app_id=str(context.get("app_id") or context.get("appId") or ""),
            skip_overlay_clear=bool(context.get("skip_overlay_clear")),
            enable_overlay_guard=use_overlay_guard,
            target_package=expected_package,
            stop_on_failure=True,
            execution_mode=planner_mode,
        )
        for row in results:
            row["plan_round"] = round_i
            row["replan_attempt"] = round_i

        round_ok = aas.business_step_results_ok(results)
        if not round_ok:
            merged_plan_log.extend(plan_log)
            merged_results.extend(results)
            return _finalize_command_block(
                phase=phase,
                command=command,
                plan=plan,
                plan_log=merged_plan_log,
                results=merged_results,
                use_overlay_guard=use_overlay_guard,
                drift_replan_count=drift_replan_count,
                goal_continue_count=goal_continue_count,
                replan_history=replan_history,
            )

        plan_complete = bool(plan.get("plan_complete", True))
        if not plan_complete and planner_mode == "ai":
            goal_continue_count += 1
            goal_continue_ctx = build_goal_continue_replan_context(
                command=command,
                previous_plan=plan,
                executed_results=results,
                attempt=round_i + 1,
            )
            drift_replan_ctx = None
            replan_history.append(
                {
                    "round": round_i,
                    "reason": "goal_not_complete",
                    "executed": results,
                    "context": goal_continue_ctx,
                }
            )
            merged_plan_log.extend(plan_log)
            merged_results.extend(results)
            SLog.i(
                TAG,
                f"goal not complete after round={round_i} "
                f"command={command[:48]!r} reply={str(plan.get('reply') or '')[:80]!r}",
            )
            continue

        merged_plan_log.extend(plan_log)
        merged_results.extend(results)
        goal_incomplete = not plan_complete and planner_mode == "ai"
        return _finalize_command_block(
            phase=phase,
            command=command,
            plan=plan,
            plan_log=merged_plan_log,
            results=merged_results,
            use_overlay_guard=use_overlay_guard,
            drift_replan_count=drift_replan_count,
            goal_continue_count=goal_continue_count,
            replan_history=replan_history,
            goal_incomplete=goal_incomplete,
        )

    SLog.w(
        TAG,
        f"command block hit safety round cap={MAX_COMMAND_PLAN_SAFETY_ROUNDS} "
        f"command={command[:48]!r}",
    )
    goal_incomplete = planner_mode == "ai" and not bool(plan.get("plan_complete", True))
    return _finalize_command_block(
        phase=phase,
        command=command,
        plan=plan,
        plan_log=merged_plan_log,
        results=merged_results,
        use_overlay_guard=use_overlay_guard,
        drift_replan_count=drift_replan_count,
        goal_continue_count=goal_continue_count,
        replan_history=replan_history,
        goal_incomplete=goal_incomplete,
    )


def execute_planned_steps_with_drift_replan(
    steps: List[Dict[str, Any]],
    *,
    instruction: str,
    sn: str,
    platform: str,
    context: Optional[Dict[str, Any]] = None,
    icon_targets: Optional[List[Dict[str, Any]]] = None,
    run_id: str = "",
    app_id: str = "",
    target_package: str = "",
    planning_mode: str = "ai",
    channel: str = "copilot",
    provider_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Copilot：执行已规划 steps，支持离屏阻断与前置弹窗后继续规划。"""
    ctx = dict(context or {})
    expected_package = (target_package or ctx.get("package") or "").strip()
    merged_results: List[Dict[str, Any]] = []
    replan_history: List[Dict[str, Any]] = []
    drift_replan_count = 0
    goal_continue_count = 0
    current_steps = list(steps or [])
    current_plan: Dict[str, Any] = {"steps": current_steps, "reply": "", "plan_complete": True}
    planner_mode = (planning_mode or "local").strip().lower()
    drift_replan_ctx: Optional[Dict[str, Any]] = None
    goal_continue_ctx: Optional[Dict[str, Any]] = None

    round_i = 0
    max_rounds = MAX_COMMAND_PLAN_SAFETY_ROUNDS if planner_mode == "ai" and instruction else 1
    while round_i < max_rounds:
        round_i += 1
        results = execute_steps(
            current_steps,
            sn=sn,
            platform=platform,
            icon_targets=icon_targets,
            run_id=run_id,
            capture_screenshots=bool(run_id),
            app_id=app_id,
            enable_overlay_guard=planner_mode != "ai",
            target_package=expected_package,
            stop_on_failure=True,
            execution_mode=planner_mode,
        )
        for row in results:
            row["plan_round"] = round_i
            row["replan_attempt"] = round_i
        merged_results.extend(results)

        if not aas.business_step_results_ok(results):
            break

        plan_complete = bool(current_plan.get("plan_complete", True))
        if not plan_complete and instruction and planner_mode == "ai":
            goal_continue_count += 1
            goal_continue_ctx = build_goal_continue_replan_context(
                command=instruction,
                previous_plan=current_plan,
                executed_results=results,
                attempt=round_i + 1,
            )
            drift_replan_ctx = None
            replan_history.append({"round": round_i, "reason": "goal_not_complete", "context": goal_continue_ctx})
            plan_ctx = {**ctx, "goal_continue_replan": goal_continue_ctx, "case_step_text": instruction}
            current_plan = cs.plan_message(
                instruction,
                sn=sn,
                context=plan_ctx,
                channel=channel,
                provider_id=provider_id,
                planning_mode=planning_mode,
            )
            if current_plan.get("error") or not current_plan.get("steps"):
                break
            current_steps = current_plan.get("steps") or []
            continue

        break

    ok_all = aas.business_step_results_ok(_results_for_ok_check(merged_results))
    if planner_mode == "ai" and not bool(current_plan.get("plan_complete", True)):
        ok_all = False
    return {
        "results": merged_results,
        "ok": ok_all,
        "msg": "全部成功" if ok_all else "部分步骤失败或用例目标未完成",
        "drift_replan_count": drift_replan_count,
        "goal_continue_count": goal_continue_count,
        "replan_history": replan_history,
        "plan_complete": bool(current_plan.get("plan_complete", True)),
    }
