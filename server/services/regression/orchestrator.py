# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""单用例入口：一律 AgentExecutor（看图闭环）。

本文件仍保留旧 Orchestrator 类（盲规划 + replan），用例回归不再调用。
公开入口只有 run_case()。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from script.log import SLog

from server.services.ai.regression.planner import (
    replan_single_step,
)
from server.services.ai.regression.schemas import (
    BaselineContext,
    CaseSpec,
    EventResult,
    EventStatus,
    PlanEvent,
    PlanResult,
    ReplanResult,
    RunReport,
)
from server.services.regression import case_memory
from server.services.regression.router import CapabilityRouter
from server.services.runtime.run_context import RunContext

TAG = "RegressionOrchestrator"


@dataclass
class OrchestratorOptions:
    max_replans: int = 3
    fail_fast_after_consecutive_fails: int = 2  # 连续 2 个事件失败仍未 replan 出路 → 停
    stop_on_blocked: bool = True  # HITL 出现就停（Step 5 接通后改成"等回答"）
    stop_on_declined: bool = False  # DECLINED 默认走 replan
    pause_ms_between_events: int = 200  # 事件间最小间隔，避免设备来不及刷新

    # Step 6: case memory 落盘开关
    record_trace: bool = True  # 是否把整 Run 落到 m_case_run_trace
    auto_bless_on_pass: bool = True  # PASS 时是否自动 promote 为 baseline
    # Step 6: replan 时是否注入 baseline 三段窗口
    inject_baseline_window_in_replan: bool = True


@dataclass
class _RunState:
    run_id: str
    case_id: str
    started_at_ts: float = field(default_factory=time.time)
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    replan_count: int = 0
    replans: list[dict[str, Any]] = field(default_factory=list)
    consecutive_fails: int = 0


class Orchestrator:
    """单条 case 的执行调度器。"""

    def __init__(
        self,
        run_context: RunContext,
        plan: PlanResult,
        *,
        options: Optional[OrchestratorOptions] = None,
        router: Optional[CapabilityRouter] = None,
        baseline: Optional[BaselineContext] = None,
        case_spec: Optional[CaseSpec] = None,
        run_id: str = "",
        case_id: str = "",
    ):
        self.run_context = run_context
        self.plan = plan
        self.options = options or OrchestratorOptions()
        self.router = router or CapabilityRouter(run_context)
        self.baseline = baseline
        self.case_spec = case_spec
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        self.case_id = case_id or plan.case_id or "(unknown case)"
        # 一次性构造 case_brief，喂给 HITL composer / persona prompt
        self.case_brief: str = self._build_case_brief()

    def _build_case_brief(self) -> str:
        """从 CaseSpec 拼成一段紧凑文本（用于 prompts）；无 CaseSpec 时给最小占位。"""
        cs = self.case_spec
        if cs is None:
            return f"case_id={self.case_id}（无 CaseSpec 上下文）"
        lines: list[str] = [
            f"case_id: {cs.case_id}",
            f"name: {cs.name}",
        ]
        if cs.priority:
            lines.append(f"priority: {cs.priority}")
        if cs.preconditions:
            lines.append(f"preconditions: {cs.preconditions}")
        if cs.steps:
            lines.append("steps:")
            for st in cs.steps[:20]:
                lines.append(f"  {st.index}. {st.instruction}" + (f"  => {st.expected}" if st.expected else ""))
        if cs.expected:
            lines.append(f"final_expected: {cs.expected}")
        return "\n".join(lines)

    def run(self) -> RunReport:
        state = _RunState(run_id=self.run_id, case_id=self.case_id)
        results: list[EventResult] = []
        events: list[PlanEvent] = list(self.plan.events)
        # 整 Run 内共享的 KV 容器（被 ExecutorContext.shared 引用，跨事件可读写）
        # HitlExecutor 把人工答案写到 ['hitl_last_answer']，下一条事件即可消费
        shared_kv: dict[str, Any] = {}
        if (self.run_context.target_package or "").strip():
            shared_kv["target_package"] = self.run_context.target_package.strip()

        if self._task_cancelled():
            return self._build_report(
                state, events, results,
                overall_status="fail",
                decline_reason="任务已取消",
            )

        if self.plan.mode == "decline":
            return self._build_report(
                state, events, results,
                overall_status="declined",
                decline_reason=self.plan.decline_reason or "PLAN_OVERVIEW declined",
            )

        if not events:
            return self._build_report(
                state, events, results,
                overall_status="declined",
                decline_reason="empty plan events",
            )

        i = 0
        while i < len(events):
            if self._task_cancelled():
                return self._build_report(
                    state, events, results,
                    overall_status="fail",
                    decline_reason="任务已取消",
                )
            ev = events[i]
            SLog.i(
                TAG,
                f"[{state.run_id}] event {ev.seq}/{len(events)} cap={ev.capability_id} "
                f"expected={ev.expected_executor or '-'} needs_vlm={ev.needs_vlm}",
            )
            result = self.router.dispatch(
                ev,
                run_id=state.run_id,
                case_id=state.case_id,
                case_brief=self.case_brief,
                shared=shared_kv,
            )
            results.append(result)

            if result.status == EventStatus.PASS:
                state.consecutive_fails = 0
                i += 1
                if self.options.pause_ms_between_events > 0:
                    time.sleep(self.options.pause_ms_between_events / 1000.0)
                continue

            if result.status == EventStatus.SKIPPED:
                state.consecutive_fails = 0
                i += 1
                continue

            if result.status == EventStatus.BLOCKED:
                # HITL：暂停整 Run
                if self.options.stop_on_blocked:
                    return self._build_report(
                        state, events, results,
                        overall_status="blocked",
                        blocked_reason=result.summary or "blocked by HITL",
                    )
                state.consecutive_fails = 0
                i += 1
                continue

            # FAIL / DECLINED → 触发 replan
            if not self._should_replan(result, state):
                break

            replan = self._call_replan(ev, results, events[i + 1 :], events, i)
            state.replans.append({
                "trigger_seq": ev.seq,
                "trigger_cap": ev.capability_id,
                "failure_summary": result.error or result.summary,
                "mode": replan.mode,
                "new_events": [e.model_dump(exclude_none=True) for e in replan.events],
                "ai_reasoning": replan.ai_reasoning,
                "needs_human": replan.needs_human,
            })
            state.replan_count += 1

            if replan.mode == "give_up":
                return self._build_report(
                    state, events, results,
                    overall_status="fail",
                    decline_reason=f"replan give_up: {replan.ai_reasoning}",
                )
            if replan.mode == "decline":
                # AI 让人介入
                return self._build_report(
                    state, events, results,
                    overall_status="blocked" if replan.needs_human else "fail",
                    blocked_reason=(
                        f"replan needs human: {replan.ai_reasoning}"
                        if replan.needs_human else ""
                    ),
                    decline_reason=(
                        "" if replan.needs_human else f"replan declined: {replan.ai_reasoning}"
                    ),
                )

            # replan.mode == "replan"
            if not replan.events:
                state.consecutive_fails += 1
                if state.consecutive_fails >= self.options.fail_fast_after_consecutive_fails:
                    break
                # 没新事件 → 退回旧 events 继续，避免死循环
                i += 1
                continue

            # 接管剩余事件
            new_events = [
                ev.model_copy(update={"seq": idx + len(events[: i + 1])}) if isinstance(ev, PlanEvent) else ev
                for idx, ev in enumerate(replan.events, start=1)
            ]
            # 注：这里只是重新连续编号，不改其它字段；replan_count 已增
            if replan.drop_remaining:
                events = list(events[: i + 1]) + new_events
            else:
                events = list(events[: i + 1]) + new_events + list(events[i + 1 :])
            state.consecutive_fails = 0
            i += 1

        overall = self._compute_overall(results)
        return self._build_report(state, events, results, overall_status=overall)

    # ---------- internal ----------

    def _should_replan(self, last_result: EventResult, state: _RunState) -> bool:
        if state.replan_count >= self.options.max_replans:
            SLog.w(TAG, f"[{state.run_id}] reached max_replans={self.options.max_replans}")
            return False
        if last_result.status == EventStatus.DECLINED and not self.options.stop_on_declined:
            return True
        if last_result.status == EventStatus.FAIL:
            return True
        return False

    def _call_replan(
        self,
        failed_event: PlanEvent,
        results: list[EventResult],
        remaining: list[PlanEvent],
        all_events: list[PlanEvent],
        failed_index: int,
    ) -> ReplanResult:
        completed = [
            r.model_dump(exclude_none=True) for r in results[:-1] if r.status == EventStatus.PASS
        ]
        failure_summary = results[-1].error or results[-1].summary
        # 三段窗口：若调用方没指定 baseline，则从 case_memory 拉
        baseline_ctx = self.baseline
        if (
            baseline_ctx is None
            and self.options.inject_baseline_window_in_replan
            and self.case_id
        ):
            try:
                baseline_ctx = case_memory.build_replan_window(
                    case_id=self.case_id,
                    device_signature=self.run_context.device_signature if self.run_context else "",
                    current_events=all_events,
                    current_index=failed_index,
                )
            except Exception as exc:  # pragma: no cover
                SLog.w(TAG, f"build_replan_window failed: {exc}")
                baseline_ctx = None
        return replan_single_step(
            run_context=self.run_context,
            completed_events=completed,
            failed_event=failed_event,
            failure_summary=failure_summary,
            remaining_events=remaining,
            baseline=baseline_ctx,
        )

    def _task_cancelled(self) -> bool:
        try:
            from server.services.regression.case_runner import is_task_cancelled

            return is_task_cancelled(self.run_id)
        except Exception:
            return False

    def _compute_overall(self, results: list[EventResult]) -> str:
        if not results:
            return "declined"
        passed = sum(1 for r in results if r.status == EventStatus.PASS)
        failed = sum(1 for r in results if r.status == EventStatus.FAIL)
        if failed == 0:
            return "pass"
        if passed == 0:
            return "fail"
        return "partial"

    def _build_report(
        self,
        state: _RunState,
        final_events: list[PlanEvent],
        results: list[EventResult],
        *,
        overall_status: str,
        decline_reason: str = "",
        blocked_reason: str = "",
    ) -> RunReport:
        finished_at = datetime.now().isoformat(timespec="seconds")
        elapsed_ms = int((time.time() - state.started_at_ts) * 1000)
        passed = sum(1 for r in results if r.status == EventStatus.PASS)
        failed = sum(1 for r in results if r.status == EventStatus.FAIL)
        skipped = sum(1 for r in results if r.status == EventStatus.SKIPPED)
        blocked = sum(1 for r in results if r.status == EventStatus.BLOCKED)
        declined = sum(1 for r in results if r.status == EventStatus.DECLINED)
        report = RunReport(
            run_id=state.run_id,
            case_id=state.case_id,
            sn=self.run_context.sn,
            overall_status=overall_status,  # type: ignore[arg-type]
            total_events=len(results),
            passed=passed,
            failed=failed,
            skipped=skipped,
            blocked=blocked,
            declined=declined,
            replan_count=state.replan_count,
            replans=state.replans,
            events=results,
            final_plan_events=final_events,
            elapsed_ms=elapsed_ms,
            started_at=state.started_at,
            finished_at=finished_at,
            decline_reason=decline_reason,
            blocked_reason=blocked_reason,
        )
        if self.options.record_trace:
            try:
                summary = case_memory.record_run_finished(
                    report=report,
                    plan=self.plan,
                    run_context=self.run_context,
                    case_id=state.case_id,
                    auto_bless_on_pass=self.options.auto_bless_on_pass,
                    blessed_by="auto",
                )
                if summary.get("promoted"):
                    SLog.i(TAG, f"[{state.run_id}] promoted to baseline")
            except Exception as exc:  # pragma: no cover
                SLog.w(TAG, f"[{state.run_id}] record_run_finished failed: {exc}")
        return report


# ============== 顶层一键入口 ==============


def run_case(
    case_spec: CaseSpec,
    *,
    run_context: RunContext,
    baseline: Optional[BaselineContext] = None,
    options: Optional[OrchestratorOptions] = None,
    provider_id: Optional[str] = None,
    router: Optional[CapabilityRouter] = None,
    run_id: str = "",
    use_persisted_baseline: bool = True,
    app_cache_cleared: bool = False,
    execution_mode: str = "agent",
) -> RunReport:
    """端到端跑一条 case：一律 Agent（看图闭环）。

    旧 Plan 循环（generate_overview + Orchestrator）已停用，不再按
    execution_mode / adb / claw 分叉。execution_mode 参数仅兼容旧调用方，忽略。
    """
    del baseline, options, use_persisted_baseline, app_cache_cleared, execution_mode
    from server.services.regression.agent_executor import run_agent_case

    flags = run_context.connectivity_flags if run_context else {}
    has_channel = bool(
        flags.get("adb") or flags.get("remote") or flags.get("ios_wda")
    )
    if not has_channel:
        SLog.e(TAG, f"[{run_id}] agent refused: no control channel case={case_spec.case_id}")
        return RunReport(
            run_id=run_id or "",
            case_id=case_spec.case_id,
            sn=getattr(run_context, "sn", "") or "",
            overall_status="fail",
            decline_reason="无可用执行通道（adb / remote / ios_wda）",
        )

    agent_router = router or CapabilityRouter(run_context)
    SLog.i(TAG, f"[{run_id}] agent case={case_spec.case_id}")
    return run_agent_case(
        case_spec,
        run_context=run_context,
        router=agent_router,
        provider_id=provider_id,
        run_id=run_id,
    )
