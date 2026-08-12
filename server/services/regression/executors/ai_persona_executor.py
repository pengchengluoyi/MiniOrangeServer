# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""AI Persona 执行通道（Step 7 真接）。

工作流
======
1. 校验上下文：
   - ctx.selected_impl 必须存在且 executor == "ai_persona"
   - ctx.dispatch_subevent 必须可调用（递进派发子事件回到 Router）
2. 选 prompt 模板：来自 selected_impl["prompt_template"]（找不到回落到 "PERSONA_TASK"）。
3. 构造 task_description：
     priority = event.params["task"] > selected_impl["description"] > 默认拼装
4. 调 planner.expand_persona_task() 拿到 PersonaExpandResult
     - mode=decline → 本 executor 返回 DECLINED 让 Router 试下一个 fallback
     - mode=expand 但 sub_events 为空 → DECLINED
5. 依次调 ctx.dispatch_subevent(child) 跑每条子事件
6. 汇总：
     - 全 PASS → PASS
     - 任一 BLOCKED → BLOCKED（透出原 reason）
     - 任一 FAIL → FAIL（停在第一条失败，不强行跑完）
     - 全 SKIPPED → SKIPPED
     - 任一 DECLINED → 整体 FAIL（persona 应该自己处理，不应该再 decline）

异常都会被吞掉并落到 EventResult.error，不抛给 Router。

供支持的 capability_id 集合在初始化时从 plugin registry 扫描，避免硬编码漏更新。
"""
from __future__ import annotations

import time
from typing import Any, Optional

from script.log import SLog

from server.services.ai.regression.planner import assert_visual, expand_persona_task
from server.services.ai.regression.schemas import (
    EventResult,
    EventStatus,
    PersonaExpandResult,
    PlanEvent,
)
from server.services.plugins import registry as plugin_registry
from server.services.regression.executors.base import (
    ExecutorContext,
    _now_iso,
    make_event_result,
)
from server.services.regression.screen import capture_screen, invalidate_remote_capture_cache

TAG = "AiPersonaExecutor"


class AiPersonaExecutor:
    id = "ai_persona"

    def __init__(
        self,
        *,
        provider_id: str | None = None,
        timeout_sec: int = 90,
        max_sub_events: int = 12,
    ):
        self._provider_id = provider_id
        self._timeout_sec = timeout_sec
        self._max_sub_events = max_sub_events
        self._supported_caps: set[str] = self._compute_supported()

    @classmethod
    def _compute_supported(cls) -> set[str]:
        """从 plugin registry 扫出所有"含 ai_persona impl"的 capability。"""
        out: set[str] = set()
        try:
            for cap in plugin_registry.list_capabilities():
                if any(impl.executor == "ai_persona" for impl in cap.implementations):
                    out.add(cap.id)
        except Exception as exc:  # pragma: no cover
            SLog.w(TAG, f"compute_supported failed: {exc}")
        return out

    # ---------- Executor protocol ----------

    def supports(self, capability_id: str) -> bool:
        return capability_id in self._supported_caps

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        started_at = _now_iso()
        t0 = time.time()

        impl = ctx.selected_impl or {}
        if impl.get("executor") != "ai_persona":
            return self._decline(
                event, started_at, t0,
                "ai_persona executor 被调用但 ctx.selected_impl 不是 ai_persona 实现",
            )
        if not callable(ctx.dispatch_subevent):
            return self._fail(
                event, started_at, t0,
                "ctx.dispatch_subevent 未注入，无法递进派发子事件",
            )

        template_id = str(impl.get("prompt_template") or "PERSONA_TASK")
        params = dict(event.params or {})
        ai_hint = (event.ai_reasoning or "").strip()
        if params.get("force_persona_ui"):
            ai_hint = (
                (ai_hint + "\n" if ai_hint else "")
                + "【硬性】必须通过系统设置 UI 完成，即使 adb 可用也不要 decline，不要规划 shell/adb 步骤。"
            )
        if event.capability_id == "clear_app_cache":
            pkg = str(
                params.get("package")
                or params.get("pkg")
                or ctx.shared.get("target_package")
                or getattr(ctx.run_context, "target_package", "")
                or ""
            ).strip()
            sn = str(ctx.run_context.sn or "")
            remote_on = ctx.run_context.remote.get("state") == "connected"
            if pkg and remote_on and sn.startswith("claw-"):
                from server.services.regression.persona_remote_lifecycle import open_app_details_via_exec_script

                boot_ok, boot_msg = open_app_details_via_exec_script(sn, pkg)
                if not boot_ok:
                    return self._fail(
                        event,
                        started_at,
                        t0,
                        f"EXEC_SCRIPT 打开应用详情页失败（{pkg}）: {boot_msg}",
                    )
                invalidate_remote_capture_cache(sn)
                time.sleep(0.8)
                # 清缓存是"逐屏依赖上一步"的多步动作（详情页→存储→清除缓存→确认弹窗）：
                # 一次性 expand 看不到后续屏幕上才出现的按钮，会规划不全而停在半路。
                # 改为逐屏迭代：每轮重新抓图 → 看当前屏规划"下一步"→ 执行 → 直到清空/到上限。
                return self._drive_clear_cache_iterative(
                    event, ctx, impl, template_id, pkg, boot_msg,
                    started_at=started_at, t0=t0,
                )

        task_description = self._build_task_description(event, impl)

        # ---- 1) expand ----
        expand: PersonaExpandResult = expand_persona_task(
            task_description=task_description,
            run_context=ctx.run_context,
            template_id=template_id,
            params=params,
            ai_hint=ai_hint,
            image_base64=(ctx.screen.image_base64 if ctx.screen and ctx.screen.image_base64 else ""),
            image_mime=(ctx.screen.image_mime if ctx.screen else "image/jpeg"),
            provider_id=self._provider_id,
            timeout_sec=self._timeout_sec,
            max_sub_events=self._max_sub_events,
        )

        if expand.mode == "decline":
            # AI 主动让位 → 让 Router 试下一个 executor / 或 Orchestrator replan
            return self._decline(
                event, started_at, t0,
                f"persona 展开 decline: {expand.decline_reason or expand.ai_reasoning}",
                vlm_meta=self._meta(impl, template_id, expand, []),
            )

        if not expand.sub_events:
            return self._decline(
                event, started_at, t0,
                "persona 展开为空 sub_events",
                vlm_meta=self._meta(impl, template_id, expand, []),
            )

        SLog.i(
            TAG,
            f"persona expand cap={event.capability_id} tpl={template_id} sub_count={len(expand.sub_events)}",
        )
        ctx.shared.pop("_persona_tap_history", None)

        # ---- 2) 递进派发子事件 ----
        sub_summaries: list[dict[str, Any]] = []
        first_blocked: Optional[EventResult] = None
        first_failed: Optional[EventResult] = None
        any_pass = False
        ui_caps = {"tap_element", "long_press_element", "swipe_element_to_element", "swipe_direction", "launch_app", "input_text", "press_key"}
        for child in expand.sub_events:
            try:
                sub_result = ctx.dispatch_subevent(child)
            except Exception as exc:
                SLog.e(TAG, f"sub-dispatch exception: {exc}")
                sub_summaries.append({
                    "seq": child.seq,
                    "capability_id": child.capability_id,
                    "status": "fail",
                    "summary": f"sub-dispatch 抛异常：{exc}",
                    "elapsed_ms": 0,
                })
                first_failed = first_failed or self._synthesize_sub_fail(child, str(exc))
                break

            sub_summaries.append({
                "seq": child.seq,
                "capability_id": child.capability_id,
                "event_kind": sub_result.event_kind,
                "status": str(sub_result.status.value if hasattr(sub_result.status, "value") else sub_result.status),
                "executor_used": sub_result.executor_used,
                "summary": sub_result.summary,
                "error": sub_result.error,
                "elapsed_ms": sub_result.elapsed_ms,
            })
            if sub_result.status == EventStatus.PASS:
                any_pass = True
                stuck = self._check_persona_stuck(ctx, child, sub_result)
                if stuck:
                    first_failed = make_event_result(
                        child,
                        status=EventStatus.FAIL,
                        executor_used=self.id,
                        started_at=_now_iso(),
                        elapsed_ms=0,
                        summary=stuck,
                        error=stuck,
                    )
                    sub_summaries[-1]["status"] = "fail"
                    sub_summaries[-1]["summary"] = stuck
                    break
                if child.capability_id in ui_caps:
                    invalidate_remote_capture_cache(ctx.run_context.sn)
                    time.sleep(0.55)
                continue
            if sub_result.status == EventStatus.SKIPPED:
                continue
            if sub_result.status == EventStatus.BLOCKED:
                first_blocked = sub_result
                break
            # FAIL / DECLINED → 停止；persona 应该自己负责，不要一路走到底
            first_failed = sub_result
            break

        # ---- 3) 汇总 ----
        vlm_meta = self._meta(impl, template_id, expand, sub_summaries)
        elapsed_ms = int((time.time() - t0) * 1000)

        if first_blocked is not None:
            return make_event_result(
                event,
                status=EventStatus.BLOCKED,
                executor_used=self.id,
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                summary=f"persona 子事件阻塞: {first_blocked.summary}",
                error=first_blocked.error or "sub-event blocked",
                vlm_meta=vlm_meta,
            )
        if first_failed is not None:
            return make_event_result(
                event,
                status=EventStatus.FAIL,
                executor_used=self.id,
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                summary=f"persona 子事件失败: {first_failed.summary}",
                error=first_failed.error or f"sub-event {first_failed.capability_id} failed",
                vlm_meta=vlm_meta,
            )

        # 全部跑完，没有 fail / blocked
        if not any_pass:
            # 全 SKIPPED → 视为 SKIPPED
            return make_event_result(
                event,
                status=EventStatus.SKIPPED,
                executor_used=self.id,
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                summary=f"persona 展开 {len(sub_summaries)} 条全 skipped",
                error="",
                vlm_meta=vlm_meta,
            )

        return make_event_result(
            event,
            status=EventStatus.PASS,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            summary=(
                f"persona 拟人化展开成功（{len(sub_summaries)} 子事件）: "
                + (expand.ai_reasoning[:80] if expand.ai_reasoning else "")
            ),
            error="",
            vlm_meta=vlm_meta,
        )

    # ---------- helpers ----------

    def _drive_clear_cache_iterative(
        self,
        event: PlanEvent,
        ctx: ExecutorContext,
        impl: dict[str, Any],
        template_id: str,
        pkg: str,
        boot_msg: str,
        *,
        started_at: str,
        t0: float,
        max_rounds: int = 8,
    ) -> EventResult:
        """清缓存逐屏迭代：每轮重新抓图 → 判成功 → 看当前屏规划下一步 → 执行。

        与"一次性 expand 全序列"不同：清缓存的后续按钮（存储页里的「清除缓存」、
        确认弹窗）只有点进上一屏后才出现，首图规划不出来。这里每轮都基于当前真实
        截图重新决策，走完"详情页 → 存储 → 清除缓存 → 确认"整条动作链。
        """
        sn = str(ctx.run_context.sn or "")
        app_name = str((event.params or {}).get("app_name") or ctx.shared.get("app_name") or "")
        label = app_name or pkg
        sub_summaries: list[dict[str, Any]] = []
        provider_id = self._provider_id
        # 成功判据：存储已清空 / 缓存为 0 / 回到应用信息页且缓存清零
        success_expectation = (
            f"应用 {label} 的存储或缓存已被清空"
            "（出现『已清除』『清除成功』提示，或存储/缓存大小显示为 0 B / 0MB，"
            "或确认弹窗已消失且缓存归零）"
        )
        # 每轮任务描述：强调只做"当前屏的下一步"
        task_next = (
            f"正在清空应用 {label}（{pkg}）的存储/缓存，当前处于系统设置内。"
            "只根据【当前截图】规划**下一步**要点的入口，不要一次规划多步、不要从设置首页重头导航：\n"
            "  - 在『应用信息』页 → 点『存储空间和缓存』/『存储』/『存储用量』；\n"
            "  - 在存储页 → 点『清除缓存』/『清空缓存』（若只有『清除数据/清空存储』也可点）；\n"
            "  - 出现确认弹窗 → 点『确定』/『清空』/『删除』；\n"
            "  - 已清空/缓存为 0 → 无需再操作。\n"
            "禁止 press_key=home/back 回桌面，禁止 adb pm clear。"
        )

        last_progress_sig = ""
        last_screen_sig = ""
        screen_history: list[str] = []
        stall_rounds = 0
        osc_rounds = 0
        for rnd in range(1, max_rounds + 1):
            invalidate_remote_capture_cache(sn)
            screen = capture_screen(
                ctx.run_context, prefer=ctx.capture_prefer, force_fresh=True,
            )
            if not screen or not screen.has_image():
                SLog.w(TAG, f"clear_cache 迭代抓图失败 sn={sn} pkg={pkg} round={rnd}")
                return self._finish_clear_cache(
                    event, started_at, t0, impl, template_id, sub_summaries,
                    ok=False, msg=f"第 {rnd} 轮抓图失败，无法继续清缓存",
                )
            ctx.screen = screen

            # 1) 先判是否已清空 —— 成功即收工
            av = assert_visual(
                expectation=success_expectation,
                image_base64=screen.image_base64,
                image_mime=screen.image_mime,
                provider_id=provider_id,
            )
            SLog.i(
                TAG,
                f"clear_cache round={rnd}/{max_rounds} sn={sn} pkg={pkg} "
                f"assert.passed={av.passed} conf={av.confidence:.2f} "
                f"evidence={(av.evidence or av.ai_reasoning)[:100]!r} "
                f"screen_bytes={len(screen.image_base64)}"
            )
            if av.passed:
                SLog.i(TAG, f"clear_cache 已完成 sn={sn} pkg={pkg} round={rnd}: {av.evidence[:80]}")
                return self._finish_clear_cache(
                    event, started_at, t0, impl, template_id, sub_summaries,
                    ok=True, msg=f"存储/缓存已清空（{pkg}，{rnd} 轮）: {av.evidence[:80]}",
                )

            # 2) 看当前屏规划"下一步"（只取第一条可执行 UI 子事件）
            expand = expand_persona_task(
                task_description=task_next,
                run_context=ctx.run_context,
                template_id=template_id,
                params=event.params or {},
                ai_hint=(
                    f"【前置】已在 {pkg} 的应用设置内（fg={boot_msg[:80]}）。"
                    f"这是第 {rnd}/{max_rounds} 轮，只规划当前屏的下一步。"
                ),
                image_base64=screen.image_base64,
                image_mime=screen.image_mime,
                provider_id=provider_id,
                timeout_sec=self._timeout_sec,
                max_sub_events=self._max_sub_events,
            )
            next_ev = self._first_actionable(expand.sub_events)
            _np = (next_ev.params or {}) if next_ev else {}
            SLog.i(
                TAG,
                f"clear_cache round={rnd} 规划下一步: "
                f"cap={getattr(next_ev, 'capability_id', None)} "
                f"target={_np.get('description') or _np.get('label') or getattr(next_ev, 'label', '')!r} "
                f"expand_mode={expand.mode} reason={expand.ai_reasoning[:80]!r}"
            )
            if next_ev is None:
                # 模型认为无下一步：可能已完成（assert 漏判）或卡住
                SLog.w(
                    TAG,
                    f"clear_cache 第 {rnd} 轮无可执行下一步 sn={sn} pkg={pkg} "
                    f"mode={expand.mode} reason={expand.ai_reasoning[:80]}",
                )
                return self._finish_clear_cache(
                    event, started_at, t0, impl, template_id, sub_summaries,
                    ok=False,
                    msg=f"第 {rnd} 轮无法确定下一步（{expand.decline_reason or expand.ai_reasoning[:80]}）",
                )

            # 3) 执行这一步（router 会现场 locate+tap）
            # 先清掉上一轮的定位结果，确保 last_locate 反映本轮真实点击坐标
            ctx.shared.pop("last_locate", None)
            try:
                sub_result = ctx.dispatch_subevent(next_ev)
            except Exception as exc:
                SLog.e(TAG, f"clear_cache sub-dispatch 异常 round={rnd}: {exc}")
                return self._finish_clear_cache(
                    event, started_at, t0, impl, template_id, sub_summaries,
                    ok=False, msg=f"第 {rnd} 轮执行异常：{exc}",
                )
            sub_summaries.append({
                "round": rnd,
                "seq": next_ev.seq,
                "capability_id": next_ev.capability_id,
                "status": str(sub_result.status.value if hasattr(sub_result.status, "value") else sub_result.status),
                "executor_used": sub_result.executor_used,
                "summary": sub_result.summary,
                "error": sub_result.error,
            })
            if sub_result.status not in (EventStatus.PASS, EventStatus.SKIPPED):
                return self._finish_clear_cache(
                    event, started_at, t0, impl, template_id, sub_summaries,
                    ok=False,
                    msg=f"第 {rnd} 轮子步骤失败：{sub_result.summary or sub_result.error}",
                )

            # 4) 停滞检测：坐标由 router 在 dispatch 内部现场 locate 注入到副本，
            # next_ev.params 里没有 x/y（恒 None），必须用 router 回写的真实点击坐标
            # ctx.shared["last_locate"]，并叠加"截图是否变化"作为进展判据。
            loc = ctx.shared.get("last_locate") or {}
            lx, ly = loc.get("x"), loc.get("y")
            screen_sig = f"{len(screen.image_base64)}:{screen.image_base64[-48:]}"
            SLog.i(
                TAG,
                f"clear_cache round={rnd} 已执行: sub_status="
                f"{sub_result.status.value if hasattr(sub_result.status, 'value') else sub_result.status} "
                f"located=({lx},{ly}) summary={sub_result.summary[:60]!r}"
            )
            # 4a) 屏幕完全不变（连续两轮点同一位置无反应）→ 卡死
            coord_repeat = (
                last_progress_sig != ""
                and lx is not None
                and last_progress_sig == f"{next_ev.capability_id}:{lx},{ly}"
            )
            screen_stuck = screen_sig == last_screen_sig
            # 4b) 两屏震荡：在 A↔B 两个页面间来回切换（如详情页⇄存储页反复进退），
            # 屏幕在变但没有真正推进；screen_history 里本轮签名已出现过 → 震荡
            oscillating = screen_sig in screen_history
            screen_history.append(screen_sig)
            if len(screen_history) > 4:
                screen_history.pop(0)

            if coord_repeat and screen_stuck:
                stall_rounds += 1
                if stall_rounds >= 2:
                    return self._finish_clear_cache(
                        event, started_at, t0, impl, template_id, sub_summaries,
                        ok=False, msg=f"连续 {stall_rounds + 1} 轮点相同位置且屏幕无变化，疑似卡住",
                    )
            elif oscillating:
                osc_rounds += 1
                if osc_rounds >= 3:
                    return self._finish_clear_cache(
                        event, started_at, t0, impl, template_id, sub_summaries,
                        ok=False,
                        msg=f"清缓存在 {osc_rounds + 1} 个页面间反复横跳未推进（疑似点错入口/找不到清除按钮）",
                    )
            else:
                stall_rounds = 0
                osc_rounds = 0
            # 每轮都更新基线（无论是否重复），供下一轮比对
            if lx is not None:
                last_progress_sig = f"{next_ev.capability_id}:{lx},{ly}"
            last_screen_sig = screen_sig
            time.sleep(0.6)

        return self._finish_clear_cache(
            event, started_at, t0, impl, template_id, sub_summaries,
            ok=False, msg=f"清缓存达 {max_rounds} 轮上限仍未确认清空",
        )

    @staticmethod
    def _first_actionable(sub_events: list[PlanEvent]) -> Optional[PlanEvent]:
        """取展开结果里第一条可执行的 UI 子事件（清缓存迭代每轮只走一步）。"""
        ui_caps = {"tap_element", "long_press_element", "input_text", "swipe_direction"}
        for ev in sub_events or []:
            if ev.capability_id in ui_caps:
                return ev
        return None

    def _finish_clear_cache(
        self,
        event: PlanEvent,
        started_at: str,
        t0: float,
        impl: dict[str, Any],
        template_id: str,
        sub_summaries: list[dict[str, Any]],
        *,
        ok: bool,
        msg: str,
    ) -> EventResult:
        elapsed_ms = int((time.time() - t0) * 1000)
        vlm_meta = {
            "kind": "persona",
            "impl_id": impl.get("id"),
            "prompt_template": template_id,
            "clear_cache_iterative": True,
            "rounds": len(sub_summaries),
            "sub_events": sub_summaries,
        }
        return make_event_result(
            event,
            status=EventStatus.PASS if ok else EventStatus.FAIL,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            summary=(f"清缓存完成: {msg}" if ok else f"清缓存失败: {msg}"),
            error="" if ok else msg,
            vlm_meta=vlm_meta,
        )


    @staticmethod
    def _check_persona_stuck(
        ctx: ExecutorContext,
        child: PlanEvent,
        sub_result: EventResult,
    ) -> str:
        """同一区域连续点击仍 PASS 但界面无进展 → 提前失败，避免死循环。"""
        if child.capability_id != "tap_element" or sub_result.status != EventStatus.PASS:
            return ""
        params = child.params or {}
        try:
            x, y = int(params.get("x")), int(params.get("y"))
        except (TypeError, ValueError):
            return ""
        history: list[tuple[int, int]] = ctx.shared.setdefault("_persona_tap_history", [])
        history.append((x, y))
        if len(history) < 4:
            return ""
        recent = history[-4:]
        rx, ry = recent[0]
        if all(abs(px - rx) <= 24 and abs(py - ry) <= 24 for px, py in recent):
            return (
                f"拟人步骤重复点击相近坐标 ({x},{y}) 无进展"
                "（可能未进入设置页或 VLM 定位在桌面图标上）"
            )
        return ""

    @staticmethod
    def _build_task_description(event: PlanEvent, impl: dict[str, Any]) -> str:
        params = event.params or {}
        if params.get("task"):
            return str(params["task"])
        # 业务习惯：装包 / 清缓存这类 cap 自带 package / apk_path 信息
        cap = event.capability_id
        if cap == "clear_app_cache":
            pkg = params.get("package") or "<未指定 package>"
            name = params.get("app_name") or pkg
            return (
                f"从当前屏幕继续清空应用 {name}（{pkg}）存储："
                "已在应用信息页则直接点存储入口；已看到清空按钮则直接点并确认；"
                "按截图分步推进，不要从设置首页重头导航"
            )
        if cap == "install_apk":
            target = params.get("file_name") or params.get("url") or "<未指定 apk>"
            return f"驱动安装弹窗完成 APK 安装：{target}"
        if cap in {"close_app", "kill_app"}:
            pkg = params.get("package") or "<未指定 package>"
            name = params.get("app_name") or pkg
            return (
                f"强制停止应用 {name}（{pkg}）："
                "设置 → 应用 → 应用信息 → 强制停止 → 确认"
            )
        desc = impl.get("description") or impl.get("display_name") or ""
        if desc:
            return f"{desc}（capability={cap}, params={params}）"
        return f"完成系统级能力 {cap}（params={params}）"

    @staticmethod
    def _meta(
        impl: dict[str, Any],
        template_id: str,
        expand: PersonaExpandResult,
        sub_summaries: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "kind": "persona",
            "impl_id": impl.get("id", ""),
            "prompt_template": template_id,
            "expand_mode": expand.mode,
            "expand_confidence": expand.confidence,
            "expand_ai_reasoning": expand.ai_reasoning,
            "expand_needs_human": expand.needs_human,
            "expand_decline_reason": expand.decline_reason,
            "expand_warnings": expand.parse_warnings,
            "sub_events": sub_summaries,
            "sub_event_count": len(sub_summaries),
        }

    def _decline(
        self,
        event: PlanEvent,
        started_at: str,
        t0: float,
        reason: str,
        vlm_meta: Optional[dict[str, Any]] = None,
    ) -> EventResult:
        return make_event_result(
            event,
            status=EventStatus.DECLINED,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000),
            summary=reason,
            error="",
            vlm_meta=vlm_meta or {"kind": "persona", "decline": True},
        )

    def _fail(
        self,
        event: PlanEvent,
        started_at: str,
        t0: float,
        msg: str,
    ) -> EventResult:
        return make_event_result(
            event,
            status=EventStatus.FAIL,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000),
            summary=msg,
            error=msg,
            vlm_meta={"kind": "persona", "fail": True},
        )

    def _synthesize_sub_fail(self, child: PlanEvent, err: str) -> EventResult:
        return EventResult(
            seq=child.seq,
            capability_id=child.capability_id,
            event_kind=child.event_kind or child.capability_id,
            status=EventStatus.FAIL,
            executor_used="(sub-dispatch raised)",
            elapsed_ms=0,
            summary=f"子事件派发异常：{err}",
            error=err,
            ai_reasoning=child.ai_reasoning,
        )
