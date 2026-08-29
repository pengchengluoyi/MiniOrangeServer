# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""CapabilityRouter：把 PlanEvent 分发到合适的 Executor。

职责清单（每条事件都按这个顺序走一遍）：
  1. 决定本事件**应该用哪个 executor**：
       expected_executor (from AI) → fallback_executors (from AI) → executor 自己 supports() 兜底
       并按当前 RunContext 的 connectivity 把不可用的 executor 过滤掉。
  2. 若该 event needs_vlm=True：
       a) 调 capture_screen() 抓一张图（按 prefer：adb / remote / ios_wda / playwright）
       b) 把截图 attach 到 ExecutorContext.screen
       c) 若 event 是 tap/long_press/swipe_element_to_element/input_text 这种"看图后再做"型，
          先调 locate_element() 拿坐标 → 写回 event.params.x/y / from_x/y / to_x/y
  3. 调 executor.execute(event, ctx)，拿到 EventResult。
  4. 失败时：按 fallback 顺序换 executor 再来一次；全失败 → 返回最后那个 fail 结果。

不在这里做的事：
  - replan：失败累计后由 Orchestrator 调 replan_single_step（router 只管"这一条事件能不能跑"）
  - HITL 阻塞：直接把 BLOCKED 透传给 Orchestrator
"""
from __future__ import annotations

import copy
from typing import Any, Optional

from script.log import SLog

from server.services.ai.regression.planner import locate_element
from server.services.ai.regression.schemas import EventResult, EventStatus, PlanEvent
from server.services.plugins import registry as plugin_registry
from server.services.regression.agent_stream import make_thumb
from server.services.regression.executors import (
    Executor,
    ExecutorContext,
    build_default_executors,
)
from server.services.regression.screen import (
    CapturedScreen,
    capture_screen,
    invalidate_remote_capture_cache,
    screenshot_failure_meta,
)
from server.services.runtime.run_context import RunContext

TAG = "CapabilityRouter"

# 这些 capability 的 needs_vlm=True 意味着"先看后做"，需要 router 在 dispatch 前
# 调 locate_element 把目标坐标注入 event.params。其他 needs_vlm=True 的事件
# （如 assert_visual / wait_screen_ready）则由 VlmExecutor 自己用截图判断，不需要预定位。
_VLM_LOCATE_NEEDED: set[str] = {
    "tap_element",
    "long_press_element",
    "input_text",
    "swipe_element_to_element",
}


class CapabilityRouter:
    def __init__(
        self,
        run_context: RunContext,
        *,
        executors: Optional[dict[str, Executor]] = None,
        capture_prefer: tuple[str, ...] = ("adb", "remote"),  # web 传入 ("playwright",)
    ):
        self.run_context = run_context
        self.executors: dict[str, Executor] = executors or build_default_executors()
        self.capture_prefer = capture_prefer
        # 缓存 capability_id → 该 cap 在菜单里允许的 executor 列表（用于校验 AI 输出 + 兜底）
        self._menu_index = self._build_menu_index()
        # 当前连通的 executor 集合
        self._connected = self._available_executors()
        # (capability_id, executor_id) → Implementation 字典（plugin yaml 的原始字段）
        # Step 7 起：AiPersonaExecutor 需要拿到 prompt_template / expands_to_events 等字段
        self._impl_index = self._build_impl_index()

    # ---------- public ----------

    def dispatch(
        self,
        event: PlanEvent,
        *,
        run_id: str = "",
        case_id: str = "",
        case_brief: str = "",
        shared: Optional[dict[str, Any]] = None,
    ) -> EventResult:
        """跑一条事件，按 expected → fallback 顺序尝试。"""
        ordered = self._executor_order(event)
        if not ordered:
            return self._synthetic_fail(event, "router: 当前 connectivity 没有任何 executor 能处理该 capability")

        shared_kv = shared if shared is not None else {}

        def _sub_dispatch(child_event: PlanEvent) -> EventResult:
            """供 AiPersonaExecutor 等"展开型"执行器递进调用。"""
            return self.dispatch(
                child_event,
                run_id=run_id,
                case_id=case_id,
                case_brief=case_brief,
                shared=shared_kv,
            )

        ctx = ExecutorContext(
            run_context=self.run_context,
            run_id=run_id,
            case_id=case_id,
            case_brief=case_brief,
            shared=shared_kv,
            capture_prefer=self.capture_prefer,
            dispatch_subevent=_sub_dispatch,
        )

        # 若需要 VLM "先看"
        prepared_event = event
        if self._needs_locate(event):
            screen, locate_err = self._ensure_screen_for_locate(ctx, event)
            if locate_err is not None:
                return locate_err  # 无图无坐标 → 直接 fail
            prepared_event = self._inject_locate_coords(event, screen, ctx)
            if prepared_event is None:
                return self._with_thumb(
                    self._synthetic_fail(
                        event,
                        f"router: VLM locate 未能在截图上找到目标 '{(event.params or {}).get('description') or event.label}'",
                        screenshot_path=screen.image_path if screen else "",
                    ),
                    screen,
                )

        # 若是纯 VLM 看图事件（assert_visual / wait_screen_ready），也要抓图
        if event.capability_id in {"assert_visual", "wait_screen_ready"} and ctx.screen is None:
            screen = capture_screen(self.run_context, prefer=self.capture_prefer)
            ctx.screen = screen

        # ai_persona 展开需要一张初始图；子步骤 tap 各自 force_fresh 抓图
        if "ai_persona" in ordered and ctx.screen is None:
            ctx.screen = capture_screen(self.run_context, prefer=self.capture_prefer, force_fresh=True)

        # 依次尝试 executor
        last_result: Optional[EventResult] = None
        for ex_id in ordered:
            executor = self.executors.get(ex_id)
            if executor is None:
                continue
            if not executor.supports(prepared_event.capability_id):
                continue
            # 把"本次选用的 Implementation"传给 executor（持 prompt_template / expands_to_events 等）
            ctx.selected_impl = self._impl_index.get((prepared_event.capability_id, ex_id))
            result = executor.execute(prepared_event, ctx)
            last_result = self._with_thumb(result, ctx.screen)
            # PASS / BLOCKED / DECLINED 立即返回（DECLINED 也走 fallback）
            if result.status == EventStatus.PASS or result.status == EventStatus.BLOCKED:
                return last_result
            if result.status == EventStatus.DECLINED:
                # DECLINED = 这个 executor 主动让位，尝试下一个 fallback
                SLog.i(
                    TAG,
                    f"executor {ex_id} declined cap={event.capability_id}: {result.summary}",
                )
                continue
            # FAIL / SKIPPED → 尝试 fallback
            SLog.i(
                TAG,
                f"executor {ex_id} failed cap={event.capability_id} status={result.status}: {result.error[:120]}",
            )
        # 全部尝试完仍未 PASS
        return last_result or self._synthetic_fail(event, "router: 所有可用 executor 都拒绝执行")

    @staticmethod
    def _with_thumb(result: Optional[EventResult], screen) -> Optional[EventResult]:
        """把当前屏缩略图挂到 EventResult，供前端时间线展示（落库后历史可回放）。"""
        if result is None:
            return None
        if getattr(result, "thumb", None):
            return result
        if screen is None or not getattr(screen, "has_image", lambda: False)():
            return result
        try:
            thumb = make_thumb(screen.image_base64)
            if thumb:
                return result.model_copy(update={"thumb": thumb})
        except Exception as e:  # pragma: no cover
            SLog.d(TAG, f"_with_thumb failed: {e}")
        return result

    # ---------- internal ----------

    def _build_menu_index(self) -> dict[str, list[str]]:
        idx: dict[str, list[str]] = {}
        for cap in plugin_registry.list_capabilities():
            execs = [impl.executor for impl in cap.implementations]
            idx[cap.id] = execs
        return idx

    def _build_impl_index(self) -> dict[tuple[str, str], dict[str, Any]]:
        """同 cap+executor 下若有多 impl（同 executor），取第一条作为代表。"""
        idx: dict[tuple[str, str], dict[str, Any]] = {}
        for cap in plugin_registry.list_capabilities():
            for impl in cap.implementations:
                key = (cap.id, impl.executor)
                if key in idx:
                    continue
                idx[key] = impl.model_dump(exclude_none=True)
        return idx

    def _available_executors(self) -> set[str]:
        flags = self.run_context.connectivity_flags
        connected: set[str] = {"internal"}
        for ex_id in self.executors:
            if ex_id == "internal":
                continue
            if ex_id == "ai_persona":
                # ai_persona 借 remote 通道执行
                if flags.get("remote", False):
                    connected.add(ex_id)
            elif flags.get(ex_id, False):
                connected.add(ex_id)
        return connected

    def _executor_order(self, event: PlanEvent) -> list[str]:
        """合成最终的 executor 尝试顺序。

        优先级：
          1. capture_prefer 首选（网页 playwright / iOS ios_wda），若该通道能跑这条能力
          2. event.expected_executor（AI 选的）
          3. event.fallback_executors
          4. 该 cap 在菜单里允许的其他 executor
        全部按 connectivity 过滤。
        """
        in_menu = self._menu_index.get(event.capability_id, [])
        ordered: list[str] = []
        if event.expected_executor:
            ordered.append(event.expected_executor)
        for ex in event.fallback_executors or []:
            if ex not in ordered:
                ordered.append(ex)
        for ex in in_menu:
            if ex not in ordered:
                ordered.append(ex)
        # 过滤：必须 connected + 必须在我们注册的 executors 字典里 + executor.supports() 同意
        out: list[str] = []
        for ex in ordered:
            if ex not in self.executors:
                continue
            if ex not in self._connected:
                continue
            if not self.executors[ex].supports(event.capability_id):
                continue
            out.append(ex)
        prefer0 = (self.capture_prefer or ("",))[0]
        if prefer0 in out:
            out = [prefer0] + [x for x in out if x != prefer0]
        return out

    def _impl_skips_vlm_locate(self, event: PlanEvent) -> bool:
        ordered = self._executor_order(event)
        return bool(ordered) and ordered[0] == "playwright"

    def _needs_locate(self, event: PlanEvent) -> bool:
        if event.capability_id not in _VLM_LOCATE_NEEDED:
            return False
        if self._impl_skips_vlm_locate(event):
            return False
        params = event.params or {}
        # 带语义锚点（target: resource_id/text/content_desc）时不走 VLM locate：
        # 由 executor 用 UI 层级解析成精确坐标，更稳且可复用（S0b）。
        try:
            from server.services.regression.hierarchy import has_target

            if has_target(params):
                return False
        except Exception:  # pragma: no cover - 解析模块不可用时退回原行为
            pass
        # 已经有坐标（如 baseline 填的）就不重抓
        if event.capability_id == "swipe_element_to_element":
            return any(params.get(k) is None for k in ("from_x", "from_y", "to_x", "to_y"))
        return params.get("x") is None or params.get("y") is None

    def _ensure_screen_for_locate(
        self,
        ctx: ExecutorContext,
        event: PlanEvent,
    ) -> tuple[Optional[CapturedScreen], Optional[EventResult]]:
        screen = capture_screen(
            self.run_context,
            prefer=self.capture_prefer,
            force_fresh=True,
        )
        ctx.screen = screen
        if not screen.has_image():
            return None, self._synthetic_fail(
                event,
                f"router: 抓图失败（{screen.source}: {screen.error}），VLM locate 无法继续",
                vlm_meta={"screenshot_capture": screenshot_failure_meta(screen)},
            )
        return screen, None

    def _inject_locate_coords(
        self,
        event: PlanEvent,
        screen: CapturedScreen,
        ctx: ExecutorContext,
    ) -> Optional[PlanEvent]:
        params = event.params or {}
        description = (
            params.get("description")
            or params.get("label")
            or event.label
            or event.ai_reasoning[:60]
        )
        # 单坐标事件
        if event.capability_id in {"tap_element", "long_press_element", "input_text"}:
            loc = locate_element(
                description=description,
                preview_width=screen.width,
                preview_height=screen.height,
                image_base64=screen.image_base64,
                image_mime=screen.image_mime,
                ai_hint=event.ai_reasoning,
            )
            if not loc.found:
                ctx.shared["last_locate"] = loc.model_dump(exclude_none=True)
                return None
            new_params = dict(params)
            new_params["x"] = loc.x
            new_params["y"] = loc.y
            new_params.setdefault("description", description)
            ctx.shared["last_locate"] = loc.model_dump(exclude_none=True)
            return event.model_copy(update={"params": new_params})

        # 两点 swipe
        if event.capability_id == "swipe_element_to_element":
            from_desc = params.get("from_description") or params.get("from") or "起点"
            to_desc = params.get("to_description") or params.get("to") or "终点"
            loc_from = locate_element(
                description=str(from_desc),
                preview_width=screen.width, preview_height=screen.height,
                image_base64=screen.image_base64, image_mime=screen.image_mime,
                ai_hint=event.ai_reasoning,
            )
            loc_to = locate_element(
                description=str(to_desc),
                preview_width=screen.width, preview_height=screen.height,
                image_base64=screen.image_base64, image_mime=screen.image_mime,
                ai_hint=event.ai_reasoning,
            )
            if not (loc_from.found and loc_to.found):
                ctx.shared["last_locate_from"] = loc_from.model_dump(exclude_none=True)
                ctx.shared["last_locate_to"] = loc_to.model_dump(exclude_none=True)
                return None
            new_params = dict(params)
            new_params["from_x"] = loc_from.x; new_params["from_y"] = loc_from.y
            new_params["to_x"] = loc_to.x; new_params["to_y"] = loc_to.y
            ctx.shared["last_locate_from"] = loc_from.model_dump(exclude_none=True)
            ctx.shared["last_locate_to"] = loc_to.model_dump(exclude_none=True)
            return event.model_copy(update={"params": new_params})
        return event

    def _synthetic_fail(
        self,
        event: PlanEvent,
        msg: str,
        *,
        screenshot_path: str = "",
        vlm_meta: Optional[dict[str, Any]] = None,
    ) -> EventResult:
        from server.services.regression.executors.base import _now_iso, make_event_result

        ts = _now_iso()
        return make_event_result(
            event,
            status=EventStatus.FAIL,
            executor_used="router",
            started_at=ts,
            elapsed_ms=0,
            summary=msg,
            error=msg,
            screenshot_path=screenshot_path,
            vlm_meta=vlm_meta or {},
        )
