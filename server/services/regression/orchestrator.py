# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Orchestrator：顶层事件执行循环。

输入：
  - RunContext（设备 + 连通性）
  - PlanResult（已经由 generate_overview() 出过的事件序列）
  - 可选 BaselineContext（首次执行可为 None；Step 6 才大规模启用）

循环：
  while i < len(events):
    result = router.dispatch(events[i])
    record(result)
    if PASS:        → i += 1
    if BLOCKED:     → break（等 HITL；Step 5 接通后才能从这里恢复）
    if FAIL/DECLINED:
        if replan_count < max:
            replan_result = replan_single_step(...)
            if mode=give_up:  → break
            apply replan_result.events → 继续循环
        else: → break
  汇总 RunReport
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from script.log import SLog

from server.services.ai.regression.planner import (
    generate_overview,
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
    execution_mode: str = "auto",
) -> RunReport:
    """端到端跑一条 case：generate_overview → orchestrate。

    execution_mode（D1–D6 改造）：
      - "agent"：目标导向闭环引擎（每步看图决策），仅支持 adb 通道
      - "plan" ：旧的整体规划 + 逐事件 + replan（默认兼容）
      - "auto" ：adb 直连设备(非 claw)自动走 agent，其余走 plan
    """
    mode = (execution_mode or "auto").strip().lower()
    adb_ok = (run_context.adb.get("state") == "connected") if run_context else False
    is_claw = str(run_context.sn or "").startswith("claw-") if run_context else False
    use_agent = mode == "agent" or (mode == "auto" and adb_ok and not is_claw)
    if use_agent:
        if not adb_ok:
            SLog.w(TAG, f"execution_mode=agent 但 adb 未连通，回退 plan 模式 case={case_spec.case_id}")
        else:
            from server.services.regression.agent_executor import run_agent_case

            agent_router = router or CapabilityRouter(run_context)
            SLog.i(TAG, f"[{run_id}] execution_mode=agent (adb) case={case_spec.case_id}")
            return run_agent_case(
                case_spec,
                run_context=run_context,
                router=agent_router,
                provider_id=provider_id,
                run_id=run_id,
            )

    baseline_overview_text = ""
    if use_persisted_baseline and case_spec and case_spec.case_id:
        try:
            ov = case_memory.load_baseline_for_planning(
                case_id=case_spec.case_id,
                device_signature=run_context.device_signature if run_context else "",
            )
            if ov is not None:
                baseline_overview_text = ov.to_prompt_block()
                SLog.i(
                    TAG,
                    f"loaded baseline overview case={case_spec.case_id} "
                    f"events={ov.event_count} status={ov.overall_status}",
                )
        except Exception as exc:  # pragma: no cover
            SLog.w(TAG, f"load_baseline_for_planning failed: {exc}")

    plan = generate_overview(
        case_spec,
        run_context=run_context,
        baseline=baseline,
        baseline_overview_text=baseline_overview_text,
        provider_id=provider_id,
        app_cache_cleared=app_cache_cleared,
    )
    if app_cache_cleared and plan.events:
        before = len(plan.events)
        plan.events = [e for e in plan.events if e.capability_id != "clear_app_cache"]
        dropped = before - len(plan.events)
        if dropped:
            SLog.i(TAG, f"skip {dropped} clear_app_cache event(s); precondition already cleared cache")
    orch = Orchestrator(
        run_context=run_context,
        plan=plan,
        options=options,
        router=router,
        baseline=baseline,
        case_spec=case_spec,
        run_id=run_id,
        case_id=case_spec.case_id,
    )
    return orch.run()
