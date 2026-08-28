# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""登录态闸门：开业务循环前给出 SessionFact。

P0：前置要求已登录但设备停在登录页 → 直接 fail（或微信一键登录 → untestable），
不把登录混进业务目标。失败时由学习模块抓「如何登录」草稿。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from script.log import SLog

TAG = "SessionGate"

_WECHAT_ONLY_RE = re.compile(
    r"微信(一键)?登录|使用微信登录|微信授权|WeChat\s*Login|打开微信",
    re.I,
)
_PHONE_LOGIN_RE = re.compile(r"手机号|验证码|短信|一键登录|本机号码")


def required_session(precondition: str = "") -> str:
    """logged_in | guest | any。整段扫描，避免「已登录 + 清缓存」被清缓存独占。"""
    text = str(precondition or "")
    if re.search(r"未登录|游客|未登陆", text):
        return "guest"
    if re.search(r"已登录|登录状态|保持登录", text):
        return "logged_in"
    from server.services.case_precondition_service import _classify_line

    want = "any"
    chunks = [c.strip() for c in re.split(r"[\n;；]+", text) if c.strip()]
    if not chunks and text.strip():
        chunks = [text.strip()]
    for line in chunks:
        kind, _ = _classify_line(line)
        if kind == "check_logged_in":
            want = "logged_in"
        elif kind == "check_not_logged_in":
            want = "guest"
    return want


def is_wechat_untestable(screen_text: str) -> bool:
    blob = str(screen_text or "")
    if not _WECHAT_ONLY_RE.search(blob):
        return False
    if _PHONE_LOGIN_RE.search(blob) and not re.search(r"仅(能|可)微信|必须微信", blob):
        return False
    return True


def _observe_deterministic(*, sn: str, platform: str, package: str, expect_logged_in: bool) -> dict[str, Any]:
    from server.services.case_precondition_service import _check_logged_in, _mobile_engine, _screen_blob

    engine = _mobile_engine(sn, platform)
    blob = _screen_blob(engine)
    ok, msg = _check_logged_in(engine, expect_logged_in=expect_logged_in, package=package)
    return {"ok": ok, "msg": msg, "blob": blob}


def observe_session(
    *,
    sn: str,
    platform: str,
    package: str = "",
    required: str = "any",
    screen_text: str = "",
    inspect_row: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """综合规则检查 + 可选 VLM inspect_session。"""
    observed = "unknown"
    how = "unknown"
    identity = "unknown"
    blob = str(screen_text or "")
    reason = ""

    if required in ("logged_in", "guest"):
        try:
            det = _observe_deterministic(
                sn=sn,
                platform=platform,
                package=package,
                expect_logged_in=(required == "logged_in"),
            )
            blob = blob or str(det.get("blob") or "")
            reason = str(det.get("msg") or "")
            how = "node_tabs"
            if required == "logged_in":
                observed = "logged_in" if det.get("ok") else "guest"
            else:
                observed = "guest" if det.get("ok") else "logged_in"
        except Exception as exc:
            reason = f"deterministic observe failed: {exc}"
            SLog.w(TAG, reason)

    row = inspect_row if isinstance(inspect_row, dict) else {}
    vlm_session = str(row.get("session") or "").strip().lower()
    if vlm_session == "logged_in":
        if observed == "unknown":
            observed = "logged_in"
            how = "vlm"
        reason = reason or str(row.get("reason") or "")
        blob = blob or str(row.get("seen") or "")
    elif vlm_session == "logged_out":
        if observed == "unknown":
            observed = "guest"
            how = "vlm"
        reason = reason or str(row.get("reason") or "")
        blob = blob or str(row.get("seen") or "")
    elif observed == "unknown":
        reason = reason or str(row.get("reason") or "看不清登录态")

    identity = str(row.get("identity") or identity)
    return {
        "required": required,
        "observed": observed,
        "identity": identity,
        "how": how,
        "screen_text": blob[:800],
        "reason": reason[:240],
        "seen": str(row.get("seen") or "")[:240],
        "wechat_untestable": is_wechat_untestable(blob or str(row.get("seen") or "")),
    }


def evaluate_gate(fact: dict[str, Any]) -> dict[str, Any]:
    """返回 {ok, status, category, reason}。status 为 pass|fail|untestable。"""
    required = str(fact.get("required") or "any")
    observed = str(fact.get("observed") or "unknown")
    if required == "any":
        return {"ok": True, "status": "pass", "category": "", "reason": ""}
    if required == "logged_in":
        if observed == "logged_in":
            return {"ok": True, "status": "pass", "category": "", "reason": ""}
        if fact.get("wechat_untestable"):
            reason = "需要已登录，当前是微信登录页，无法自动化（untestable）"
            return {
                "ok": False,
                "status": "untestable",
                "category": "goal_unreachable",
                "reason": reason,
            }
        reason = fact.get("reason") or "需要已登录，当前不是已登录态"
        if observed == "guest" or "登录" in str(reason):
            reason = f"需要已登录，当前是登录页/游客。{reason}".strip()
        return {
            "ok": False,
            "status": "fail",
            "category": "goal_unreachable",
            "reason": reason[:240],
        }
    if required == "guest":
        if observed == "guest":
            return {"ok": True, "status": "pass", "category": "", "reason": ""}
        if observed == "logged_in":
            return {
                "ok": False,
                "status": "fail",
                "category": "goal_unreachable",
                "reason": "需要游客/未登录，当前已登录；禁止业务里点退出，请换号或清数据后再跑",
            }
        return {
            "ok": False,
            "status": "fail",
            "category": "goal_unreachable",
            "reason": fact.get("reason") or "无法确认未登录",
        }
    return {"ok": True, "status": "pass", "category": "", "reason": ""}
