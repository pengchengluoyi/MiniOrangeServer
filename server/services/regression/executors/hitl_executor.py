# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""HITL (Human-In-The-Loop) 执行通道（Step 5 真接）。

执行流：
  1. capability_id → kind 映射（human_input_text → input_text 等）。
  2. 调 HITL_PROMPT_COMPOSER（planner.compose_hitl_prompt）生成 title/body/options/constraints。
     LLM 不可用时 planner 自带 fallback，保证此处一定能拿到结果。
  3. 注册 HitlSessionManager → submit_request；得到 _Session 句柄。
  4. 通过 HitlTransport 把 HitlRequest 推送给桌面端（默认 WebSocket 广播）。
     推送失败也继续 wait，因为前端可能从 GET /hitl/pending 拉到。
  5. 阻塞 wait_for_reply()，最长 request.timeout_sec：
       - 收到 reply 且 skipped=False → PASS（answer 写入 ctx.shared['hitl_last_answer']）
       - 收到 reply 且 skipped=True  → SKIPPED
       - 超时（None）                → BLOCKED + 通知前端 revoke
  6. 任何情况下都尝试 transport.push_resolved 让其它客户端同步关闭弹框。

返回 PASS 时把 answer 同时塞进 EventResult.raw_response 与 ctx.shared，
便于下一条事件（input_text / confirm 后跟的 tap）使用人类输入。
"""
from __future__ import annotations

import time
from typing import Any

from script.log import SLog

from server.services.ai.regression.planner import compose_hitl_prompt
from server.services.ai.regression.schemas import EventResult, EventStatus, PlanEvent
from server.services.regression.executors.base import (
    ExecutorContext,
    _now_iso,
    make_event_result,
)
from server.services.regression.hitl import (
    HitlRequest,
    get_session_manager,
    get_transport,
)

TAG = "HitlExecutor"


# capability_id → HITL kind
_CAP_TO_KIND: dict[str, str] = {
    "human_confirm":          "confirm",
    "human_input_text":       "input_text",
    "human_choice_single":    "choice_single",
    "human_choice_multiple":  "choice_multiple",
    "human_upload_image":     "upload_image",
    "human_acknowledge":      "acknowledge",
}


class HitlExecutor:
    id = "hitl"

    def __init__(
        self,
        *,
        compose_timeout_sec: int = 30,
        provider_id: str | None = None,
    ):
        # 把 composer 调用参数收敛在这里，方便测试替换
        self._compose_timeout_sec = compose_timeout_sec
        self._provider_id = provider_id

    def supports(self, capability_id: str) -> bool:
        return capability_id in _CAP_TO_KIND

    def execute(self, event: PlanEvent, ctx: ExecutorContext) -> EventResult:
        started_at = _now_iso()
        t0 = time.time()

        kind = _CAP_TO_KIND.get(event.capability_id)
        if kind is None:
            return self._fail(
                event, started_at, t0,
                f"HitlExecutor 不支持 capability={event.capability_id}",
            )

        # ---- 1) composer ----
        device_brief = self._safe_device_brief(ctx)
        composer = compose_hitl_prompt(
            hitl_kind=kind,
            case_summary=ctx.case_brief or f"case_id={ctx.case_id}",
            event_dict=event.model_dump(exclude_none=True),
            device_brief=device_brief,
            provider_id=self._provider_id,
            timeout_sec=self._compose_timeout_sec,
        )

        # ---- 2) build HitlRequest ----
        manager = get_session_manager()
        request_id = manager.new_request_id()
        timeout_sec = int(composer.default_timeout_sec)
        created_at = time.time()
        request = HitlRequest(
            request_id=request_id,
            sn=ctx.run_context.sn if ctx.run_context else None,
            run_id=ctx.run_id or None,
            case_id=ctx.case_id or None,
            event_seq=event.seq,
            capability_id=event.capability_id,
            kind=kind,
            title=composer.title,
            body=composer.body,
            options=composer.options,
            constraints=composer.constraints,
            created_at=created_at,
            timeout_sec=timeout_sec,
            deadline_at=created_at + timeout_sec,
            ai_reasoning=composer.ai_reasoning or event.ai_reasoning or "",
            screenshot_path=(ctx.screen.image_path if ctx.screen and ctx.screen.image_path else None),
            composer_warnings=list(composer.parse_warnings or []),
        )

        # ---- 3) submit + broadcast ----
        try:
            session = manager.submit_request(request)
        except ValueError as exc:
            return self._fail(event, started_at, t0, f"hitl session 注册失败：{exc}")

        transport = get_transport()
        try:
            pushed = transport.push_request(request)
        except Exception as exc:
            SLog.e(TAG, f"transport.push_request raised: {exc}")
            pushed = False
        if not pushed:
            SLog.w(TAG, f"hitl {request_id} 推送失败，仍等待人工经 /hitl/pending 主动拉取")

        # ---- 4) wait ----
        SLog.i(
            TAG,
            f"[{ctx.run_id}] hitl waiting request={request_id} kind={kind} "
            f"cap={event.capability_id} timeout={timeout_sec}s",
        )
        reply = manager.wait_for_reply(session, timeout_sec=timeout_sec)

        elapsed_ms = int((time.time() - t0) * 1000)
        composer_meta: dict[str, Any] = {
            "kind": "hitl",
            "hitl_kind": kind,
            "request_id": request_id,
            "composer_title": composer.title,
            "composer_warnings": composer.parse_warnings,
            "composer_ai_reasoning": composer.ai_reasoning,
            "timeout_sec": timeout_sec,
            "broadcast_pushed": pushed,
        }

        # ---- 5) 处理结果 ----
        if reply is None:
            # 超时 → 主动 revoke + 通知前端关闭
            self._safe_revoke(request_id, reason="executor_timeout")
            self._safe_push_resolved(request_id, {
                "status": "timeout",
                "summary": f"{kind} 超时 {timeout_sec}s 未收到人工回复",
            })
            composer_meta["resolved"] = "timeout"
            return make_event_result(
                event,
                status=EventStatus.BLOCKED,
                executor_used=self.id,
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                summary=f"HITL 超时（{kind} / {timeout_sec}s 内无人答复）",
                error="hitl_timeout",
                vlm_meta=composer_meta,
                raw_response={"request": request.to_payload()},
            )

        # 走到这里说明拿到 reply（或被 revoke）
        if reply.skipped:
            self._safe_push_resolved(request_id, {
                "status": "skipped",
                "summary": "用户主动跳过",
            })
            composer_meta["resolved"] = "skipped"
            return make_event_result(
                event,
                status=EventStatus.SKIPPED,
                executor_used=self.id,
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                summary=f"HITL 被用户跳过（{kind}）",
                error="",
                vlm_meta=composer_meta,
                raw_response={"reply": reply.model_dump(), "request": request.to_payload()},
            )

        # 收到人工答复 → 写入 ctx.shared 供后续事件使用
        ctx.shared["hitl_last_answer"] = {
            "request_id": request_id,
            "kind": kind,
            "answer": reply.answer,
            "replied_at": reply.replied_at,
            "capability_id": event.capability_id,
            "event_seq": event.seq,
        }
        self._safe_push_resolved(request_id, {
            "status": "answered",
            "summary": f"用户已答复 {kind}",
        })
        composer_meta["resolved"] = "answered"
        composer_meta["answer_preview"] = self._preview_answer(reply.answer)

        return make_event_result(
            event,
            status=EventStatus.PASS,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            summary=f"HITL 收到人工答复（{kind}）：{composer_meta['answer_preview']}",
            error="",
            vlm_meta=composer_meta,
            raw_response={"reply": reply.model_dump(), "request": request.to_payload()},
        )

    # ---------- helpers ----------

    def _fail(self, event: PlanEvent, started_at: str, t0: float, msg: str) -> EventResult:
        return make_event_result(
            event,
            status=EventStatus.FAIL,
            executor_used=self.id,
            started_at=started_at,
            elapsed_ms=int((time.time() - t0) * 1000),
            summary=msg,
            error=msg,
            vlm_meta={"kind": "hitl", "fail": True},
        )

    @staticmethod
    def _safe_device_brief(ctx: ExecutorContext) -> dict[str, Any]:
        rc = ctx.run_context
        if rc is None:
            return {}
        try:
            brief = rc.to_prompt_brief()  # 含 model / channels / advice
            return brief if isinstance(brief, dict) else {"sn": getattr(rc, "sn", "")}
        except Exception:
            return {"sn": getattr(rc, "sn", "")}

    @staticmethod
    def _safe_revoke(request_id: str, reason: str) -> None:
        try:
            get_session_manager().revoke(request_id, reason=reason)
        except Exception as exc:
            SLog.w(TAG, f"revoke failed {request_id}: {exc}")

    @staticmethod
    def _safe_push_resolved(request_id: str, summary: dict[str, Any]) -> None:
        try:
            get_transport().push_resolved(request_id, summary)
        except Exception as exc:
            SLog.w(TAG, f"push_resolved failed {request_id}: {exc}")

    @staticmethod
    def _preview_answer(ans: Any, max_len: int = 80) -> str:
        if ans is None:
            return "(empty)"
        if isinstance(ans, (str, int, float, bool)):
            text = str(ans)
        elif isinstance(ans, (list, tuple)):
            text = ",".join(str(x) for x in ans)
        elif isinstance(ans, dict):
            text = ",".join(f"{k}={v}" for k, v in list(ans.items())[:3])
        else:
            text = str(ans)
        if len(text) > max_len:
            text = text[: max_len - 1] + "…"
        return text
