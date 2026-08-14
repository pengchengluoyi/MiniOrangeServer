# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""AgentExecutor：目标导向的闭环执行引擎（D1–D6 改造，仅 adb 通道）。

替代旧的「整体 plan → 逐条跑 → 文本盲 replan」两段式。核心循环：

    observe(每步截图) → decide_next_action(看图直接出坐标) → router.dispatch → re-observe

- D1 用例=目标+检查点   D2 决策 VLM 直接出坐标(不走 locate VLM)   D3 每步看图
- D4 成功交给 VLM 断言   D5 允许 ask_human(走 human_* 能力+现有 HitlExecutor)
- 收尾：步数预算 + 成功断言 + 震荡检测（取代 max_replans 硬上限）

底座全复用：CapabilityRouter / executors / 通道 / screen / case_memory。
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from script.log import SLog

from server.services.ai.regression import planner
from server.services.ai.regression.schemas import (
    CaseGoal,
    CaseSpec,
    EventResult,
    EventStatus,
    PlanEvent,
    RunReport,
)
from server.services.regression.router import CapabilityRouter
from server.services.regression.screen import capture_screen
from server.services.regression import agent_stream
from server.services.runtime.run_context import RunContext

TAG = "AgentExecutor"

# ask_human 用的 human_* 能力
_HUMAN_CAPS = {
    "human_confirm", "human_choice_single", "human_choice_multiple",
    "human_input_text", "human_upload_image", "human_acknowledge",
}

# 统一失败分类标签（供 UI 展示，让同一真因永远归到同一类）
_CATEGORY_LABEL = {
    "success": "成功",
    "goal_unreachable": "目标不可达/环境不符",
    "execution_error": "执行异常(点击/截图/设备)",
    "budget_exhausted": "步数耗尽",
    "needs_human": "需人工介入",
}


@dataclass
class AgentOptions:
    max_steps: int = 25
    oscillation_window: int = 3          # 连续 N 步 (同 action + 同屏无变化) 判卡死
    pause_ms_between_steps: int = 400
    step_timeout_sec: int = 90
    history_window: int = 8              # 喂给模型的最近步数
    capture_timeout_sec: float = 15.0
    hitl_timeout_sec: int = 300
    max_false_done: int = 2              # 判 done 但成功断言未过的最大容忍次数（超过判失败）


@dataclass
class _Step:
    idx: int
    thought: str = ""
    capability_id: str = ""
    params: dict = field(default_factory=dict)
    status: str = ""          # decision.status
    result_status: str = ""   # EventResult.status
    summary: str = ""
    screen_hash: str = ""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _screen_hash(b64: str) -> str:
    return hashlib.sha1((b64 or "").encode("utf-8")).hexdigest()[:12] if b64 else ""


class AgentExecutor:
    def __init__(
        self,
        *,
        goal: CaseGoal,
        run_context: RunContext,
        router: CapabilityRouter,
        run_id: str = "",
        case_id: str = "",
        case_brief: str = "",
        provider_id: Optional[str] = None,
        options: Optional[AgentOptions] = None,
        baseline_hint: str = "",
    ):
        self.goal = goal
        self.ctx = run_context
        self.router = router
        self.run_id = run_id or f"agent-{int(time.time())}"
        self.case_id = case_id or goal.case_id
        self.case_brief = case_brief or goal.goal
        self.provider_id = provider_id
        self.opts = options or AgentOptions()
        self.baseline_hint = baseline_hint
        self.shared: dict[str, Any] = {}
        self._assert_feedback = ""   # 上次"判 done 但校验未过"的理由，回灌给下一步
        self._false_done = 0
        self.steps: list[_Step] = []
        self.results: list[EventResult] = []

    # ---------- prompt 片段 ----------

    def _checkpoints_block(self) -> str:
        if not self.goal.checkpoints:
            return "（无显式检查点，按目标自行判断进度）"
        return "\n".join(
            f"[{'x' if cp.done else ' '}] {cp.id}: {cp.description}" for cp in self.goal.checkpoints
        )

    def _history_block(self) -> str:
        recent = self.steps[-self.opts.history_window:]
        if not recent:
            return ""
        lines = []
        for s in recent:
            act = f"{s.capability_id}({s.params})" if s.capability_id else s.status
            line = f"步骤{s.idx}: {s.thought[:80]} → {act} → {s.result_status or s.status}"
            if s.summary:
                line += f"（{s.summary[:60]}）"
            lines.append(line)
        ans = self.shared.get("hitl_last_answer")
        if ans:
            lines.append(f"[人工回复] {ans.get('answer')}")
        if self._assert_feedback:
            lines.append(f"[校验未通过] 你上次判定完成，但成功标准未在屏幕出现：{self._assert_feedback}")
        return "\n".join(lines)

    def _emit(self, phase: str, *, step: int = 0, thumb: str = "", decision=None,
              result_status: str = "", summary: str = "", overall: str = "",
              failure_category: str = "", failure_label: str = "",
              elapsed_ms: int | None = None, capability_id: str = ""):
        data: dict[str, Any] = {
            "run_id": self.run_id, "case_id": self.case_id, "sn": self.ctx.sn or "",
            "phase": phase, "step": step, "goal": self.goal.goal,
        }
        if phase == "start":
            data["checkpoints"] = [{"id": c.id, "description": c.description} for c in self.goal.checkpoints]
        if decision is not None:
            data["thought"] = decision.thought
            data["status"] = decision.status
            data["expected_after"] = decision.expected_after
            data["action"] = (
                {"capability_id": decision.action.capability_id, "params": decision.action.params}
                if decision.action else None
            )
        if thumb:
            data["thumb"] = thumb
        if result_status:
            data["result_status"] = result_status
        if summary:
            data["summary"] = summary[:200]
        if overall:
            data["overall"] = overall
        if failure_category:
            data["failure_category"] = failure_category
        if failure_label:
            data["failure_label"] = failure_label
        if elapsed_ms is not None:
            data["elapsed_ms"] = int(elapsed_ms)
        if capability_id:
            data["capability_id"] = capability_id
        agent_stream.emit_agent_event(data)

    # ---------- 主循环 ----------

    def run(self) -> RunReport:
        started_ts = time.time()
        started_at = _now_iso()
        overall = "fail"
        blocked_reason = ""
        decline_reason = ""
        failure_category = ""   # success | goal_unreachable | execution_error | budget_exhausted | needs_human
        SLog.i(TAG, f"[{self.run_id}] >>> agent case={self.case_id} goal={self.goal.goal!r} "
                    f"checkpoints={len(self.goal.checkpoints)}")
        self._emit("start")

        for step_idx in range(1, self.opts.max_steps + 1):
            screen = capture_screen(
                self.ctx, prefer=self.router.capture_prefer,
                timeout_sec=self.opts.capture_timeout_sec, force_fresh=True,
            )
            if not screen.has_image():
                SLog.w(TAG, f"[{self.run_id}] step{step_idx} 截图失败: {screen.error}")
                self._record_synthetic(step_idx, EventStatus.FAIL, "capture_screen", f"截图失败: {screen.error}")
                if step_idx >= 2:  # 连续拿不到图，放弃（基础设施问题）
                    decline_reason = f"截图失败: {screen.error}"
                    failure_category = "execution_error"
                    break
                time.sleep(1.0)
                continue

            shot_hash = _screen_hash(screen.image_base64)
            thumb = agent_stream.make_thumb(screen.image_base64)
            decision = planner.decide_next_action(
                goal=self.goal.goal,
                success_criteria=self.goal.success_criteria,
                checkpoints_block=self._checkpoints_block(),
                run_context=self.ctx,
                history_block=self._history_block(),
                width=screen.width, height=screen.height,
                image_base64=screen.image_base64, image_mime=screen.image_mime,
                baseline_hint=self.baseline_hint,
                provider_id=self.provider_id, timeout_sec=self.opts.step_timeout_sec,
            )
            SLog.i(TAG, f"[{self.run_id}] step{step_idx} status={decision.status} "
                        f"act={decision.action.capability_id if decision.action else '-'} "
                        f"thought={decision.thought[:80]!r}")
            self._emit("step", step=step_idx, thumb=thumb, decision=decision)

            # ---- done：用成功标准断言；不通过则回灌理由继续，不立即失败（弥合 done/assert 分裂） ----
            if decision.status == "done":
                ok, reason = self._assert_goal(screen, step_idx)
                if ok:
                    overall = "pass"
                    failure_category = "success"
                    break
                self._false_done += 1
                self._assert_feedback = reason
                SLog.w(TAG, f"[{self.run_id}] done 但校验未过({self._false_done}/{self.opts.max_false_done}): {reason[:80]}")
                if self._false_done >= self.opts.max_false_done:
                    overall = "fail"
                    failure_category = "execution_error"
                    decline_reason = f"多次判定完成但成功标准始终未在屏幕出现：{reason[:200]}"
                    break
                time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                continue

            # ---- give_up：agent 判定客观无法完成（如应用无此功能/环境不符） ----
            if decision.status == "give_up":
                self._record_synthetic(step_idx, EventStatus.FAIL, "give_up", decision.thought[:200], shot_hash)
                decline_reason = decision.thought[:240] or "agent give_up"
                overall = "fail"
                failure_category = "goal_unreachable"
                break

            # ---- ask_human ----
            if decision.status == "ask_human":
                res = self._ask_human(decision, step_idx, shot_hash)
                if res == "blocked":
                    overall = "blocked"
                    blocked_reason = "人工未在时限内回复"
                    failure_category = "needs_human"
                    break
                time.sleep(self.opts.pause_ms_between_steps / 1000.0)
                continue

            # ---- continue：执行一个动作 ----
            if decision.action is None or not decision.action.capability_id:
                self._record_synthetic(step_idx, EventStatus.FAIL, "noop", "continue 但无有效 action", shot_hash)
                if self._is_oscillating():
                    decline_reason = "连续无有效动作"
                    failure_category = "execution_error"
                    break
                continue

            event = PlanEvent(
                seq=step_idx,
                capability_id=decision.action.capability_id,
                event_kind=decision.action.capability_id,
                params=self._normalize_action_params(
                    decision.action.capability_id, dict(decision.action.params or {})
                ),
                needs_vlm=False,  # D2：坐标已由决策 VLM 给出，不再走 locate VLM
                expected_executor="",  # 让 router 按连通性+cost 自选（adb 优先）
                ai_reasoning=decision.thought[:240] or "(agent)",
                label=decision.expected_after[:120],
            )
            result = self.router.dispatch(
                event, run_id=self.run_id, case_id=self.case_id,
                case_brief=self.case_brief, shared=self.shared,
            )
            if thumb and not getattr(result, "thumb", None):
                try:
                    result = result.model_copy(update={"thumb": thumb})
                except Exception:
                    pass
            self.results.append(result)
            self._push_step(step_idx, decision, result_status=str(result.status.value),
                            summary=result.summary or result.error, screen_hash=shot_hash)
            self._emit(
                "result",
                step=step_idx,
                result_status=str(result.status.value),
                summary=result.summary or result.error,
                elapsed_ms=int(getattr(result, "elapsed_ms", 0) or 0),
            )
            # 有实际动作推进 → 清掉上次的 done 反馈
            self._assert_feedback = ""

            if result.status == EventStatus.BLOCKED:
                overall = "blocked"
                blocked_reason = result.error or "executor blocked"
                failure_category = "needs_human"
                break

            # 震荡/卡死检测（同动作同屏无变化 → 多为点击没落地等执行问题）
            if self._is_oscillating():
                SLog.w(TAG, f"[{self.run_id}] 检测到卡死（连续 {self.opts.oscillation_window} 步同动作同屏无变化）")
                decline_reason = "检测到卡死/震荡（同一动作屏幕无变化，可能点击未落地或页面无响应）"
                overall = "fail"
                failure_category = "execution_error"
                break

            time.sleep(self.opts.pause_ms_between_steps / 1000.0)
        else:
            # for 未 break：步数耗尽
            decline_reason = f"达到步数上限 max_steps={self.opts.max_steps} 仍未完成目标"
            overall = "partial"
            failure_category = "budget_exhausted"

        return self._build_report(overall, started_at, started_ts, blocked_reason, decline_reason, failure_category)

    # ---------- 子过程 ----------

    def _assert_goal(self, screen, step_idx: int) -> tuple[bool, str]:
        t0 = time.time()
        started_at = _now_iso()
        res = planner.assert_visual(
            expectation=self.goal.success_criteria or self.goal.goal,
            image_base64=screen.image_base64, image_mime=screen.image_mime,
            provider_id=self.provider_id, timeout_sec=self.opts.step_timeout_sec,
        )
        elapsed_ms = int((time.time() - t0) * 1000)
        thumb = agent_stream.make_thumb(screen.image_base64) if screen and screen.has_image() else ""
        status = EventStatus.PASS if res.passed else EventStatus.FAIL
        summary = res.evidence or res.ai_reasoning[:120]
        self.results.append(EventResult(
            seq=step_idx, capability_id="assert_goal", event_kind="assert_visual",
            status=status,
            executor_used="vlm", summary=summary,
            error="" if res.passed else res.ai_reasoning[:240],
            vlm_meta={"confidence": res.confidence}, thumb=thumb,
            elapsed_ms=elapsed_ms,
            started_at=started_at, finished_at=_now_iso(),
        ))
        self._emit(
            "result",
            step=step_idx,
            thumb=thumb,
            result_status=str(status.value),
            summary=summary,
            elapsed_ms=elapsed_ms,
            capability_id="assert_goal",
        )
        SLog.i(TAG, f"[{self.run_id}] 成功断言 passed={res.passed} conf={res.confidence} "
                    f"elapsed={elapsed_ms}ms {res.ai_reasoning[:80]!r}")
        return res.passed, (res.ai_reasoning or res.evidence or "成功标准未满足")

    def _ask_human(self, decision, step_idx: int, shot_hash: str) -> str:
        cap = decision.action.capability_id if decision.action else ""
        if cap not in _HUMAN_CAPS:
            cap = "human_confirm"
        params = dict(decision.action.params or {}) if decision.action else {}
        params.setdefault("question", decision.thought or "需要人工确认下一步")
        event = PlanEvent(
            seq=step_idx, capability_id=cap, event_kind=cap, params=params,
            needs_vlm=False, expected_executor="hitl",
            ai_reasoning=decision.thought[:240] or "(agent ask_human)",
            label="请求人工介入",
        )
        result = self.router.dispatch(
            event, run_id=self.run_id, case_id=self.case_id,
            case_brief=self.case_brief, shared=self.shared,
        )
        self.results.append(result)
        self._push_step(step_idx, decision, result_status=str(result.status.value),
                        summary=result.summary or result.error, screen_hash=shot_hash)
        if result.status in (EventStatus.BLOCKED,):
            return "blocked"
        return "answered"

    def _push_step(self, idx: int, decision, *, result_status: str, summary: str, screen_hash: str):
        self.steps.append(_Step(
            idx=idx, thought=decision.thought,
            capability_id=decision.action.capability_id if decision.action else "",
            params=dict(decision.action.params or {}) if decision.action else {},
            status=decision.status, result_status=result_status,
            summary=summary or "", screen_hash=screen_hash,
        ))

    def _normalize_action_params(self, cap: str, params: dict) -> dict:
        """纠正应用相关动作的包名：不信任模型给的 package，强制/兜底为目标应用包名。

        修复"启动应用时启动了别的 app"：模型看不到/记不住目标包名，容易照示例或看图标
        猜一个包。测试对象就是 target_package，故 launch 类一律覆盖，其它类缺失时兜底。
        """
        tgt = str(getattr(self.ctx, "target_package", "") or "").strip()
        if not tgt:
            return params
        cap = (cap or "").lower()
        if cap in ("launch_app", "open_app", "start_app"):
            if params.get("package") != tgt:
                SLog.i(TAG, f"[{self.run_id}] 覆盖启动包名 {params.get('package')!r} → 目标 {tgt!r}")
                params["package"] = tgt
        elif cap in ("close_app", "kill_app", "clear_app_cache") and not params.get("package"):
            params["package"] = tgt
        return params

    def _record_synthetic(self, idx: int, status: EventStatus, cap: str, summary: str, screen_hash: str = ""):
        self.results.append(EventResult(
            seq=idx, capability_id=cap, event_kind=cap, status=status,
            executor_used="agent", summary=summary[:200],
            error="" if status == EventStatus.PASS else summary[:200],
            started_at=_now_iso(), finished_at=_now_iso(),
        ))
        self.steps.append(_Step(idx=idx, capability_id=cap, status=str(status.value),
                                summary=summary, screen_hash=screen_hash))

    def _is_oscillating(self) -> bool:
        w = self.opts.oscillation_window
        if len(self.steps) < w:
            return False
        tail = self.steps[-w:]
        sig = (tail[0].capability_id, str(tail[0].params), tail[0].screen_hash)
        if not sig[0]:
            return False
        return all((s.capability_id, str(s.params), s.screen_hash) == sig for s in tail)

    def _build_report(self, overall, started_at, started_ts, blocked_reason, decline_reason, failure_category="") -> RunReport:
        counts = {EventStatus.PASS: 0, EventStatus.FAIL: 0, EventStatus.SKIPPED: 0,
                  EventStatus.BLOCKED: 0, EventStatus.DECLINED: 0}
        for r in self.results:
            counts[r.status] = counts.get(r.status, 0) + 1
        report = RunReport(
            run_id=self.run_id, case_id=self.case_id, sn=self.ctx.sn or "",
            overall_status=overall,  # type: ignore[arg-type]
            total_events=len(self.results),
            passed=counts[EventStatus.PASS], failed=counts[EventStatus.FAIL],
            skipped=counts[EventStatus.SKIPPED], blocked=counts[EventStatus.BLOCKED],
            declined=counts[EventStatus.DECLINED],
            replan_count=0, events=self.results,
            elapsed_ms=int((time.time() - started_ts) * 1000),
            started_at=started_at, finished_at=_now_iso(),
            decline_reason=decline_reason, blocked_reason=blocked_reason,
        )
        # 统一失败分类（extra="allow"）：success|goal_unreachable|execution_error|budget_exhausted|needs_human
        report.failure_category = failure_category or ("success" if overall == "pass" else "")
        report.failure_label = _CATEGORY_LABEL.get(report.failure_category, "")
        SLog.i(TAG, f"[{self.run_id}] <<< agent case={self.case_id} status={overall} "
                    f"category={report.failure_category} "
                    f"steps={len(self.steps)} ({report.passed}P/{report.failed}F/{report.blocked}B "
                    f"in {report.elapsed_ms}ms)")
        self._emit("done", overall=overall, summary=(blocked_reason or decline_reason),
                   failure_category=report.failure_category, failure_label=report.failure_label)
        return report


def run_agent_case(
    case_spec: CaseSpec,
    *,
    run_context: RunContext,
    router: CapabilityRouter,
    provider_id: Optional[str] = None,
    run_id: str = "",
    options: Optional[AgentOptions] = None,
) -> RunReport:
    """端到端跑一条 case（agent 模式）：extract_goal → AgentExecutor.run。"""
    goal = planner.extract_goal(case_spec, run_context=run_context, provider_id=provider_id)
    SLog.i(TAG, f"[{run_id}] goal extracted: {goal.goal!r} cps={[c.description for c in goal.checkpoints]}")

    # P2 few-shot：加载上次成功轨迹作为提示
    from server.services.regression import agent_memory

    device_sig = getattr(run_context, "device_signature", "") or ""
    prior = agent_memory.load_trajectory(case_spec.case_id, device_sig)
    baseline_hint = agent_memory.trajectory_to_hint(prior)
    if baseline_hint:
        SLog.i(TAG, f"[{run_id}] loaded prior success trajectory ({len(prior)} steps) as hint")

    ex = AgentExecutor(
        goal=goal, run_context=run_context, router=router,
        run_id=run_id, case_id=case_spec.case_id, case_brief=goal.goal,
        provider_id=provider_id, options=options, baseline_hint=baseline_hint,
    )
    report = ex.run()

    # 成功则记下动作轨迹，供下次 few-shot
    if report.overall_status == "pass":
        traj = [
            {"capability_id": s.capability_id, "params": s.params, "thought": (s.thought or "")[:80]}
            for s in ex.steps if s.capability_id and s.capability_id not in ("give_up", "noop", "capture_screen")
        ]
        agent_memory.save_trajectory(case_spec.case_id, device_sig, traj)

    # 落 trace（best-effort，与 plan 模式共用 case_memory；agent 暂不自动 promote baseline）
    try:
        from server.services.regression import case_memory
        from server.services.ai.regression.schemas import PlanResult

        synthetic_plan = PlanResult(
            mode="plan", case_id=case_spec.case_id,
            ai_reasoning=f"agent-mode goal={goal.goal!r}", events=[],
        )
        case_memory.record_run_finished(
            report=report, plan=synthetic_plan, run_context=run_context,
            case_id=case_spec.case_id, auto_bless_on_pass=False, blessed_by="agent",
        )
    except Exception as exc:  # pragma: no cover
        SLog.w(TAG, f"[{run_id}] agent record_run_finished failed: {exc}")

    return report
