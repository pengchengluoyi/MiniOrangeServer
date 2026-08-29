# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""用例三列覆盖码：前置 / 步骤 / 预期。"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

PREP_OK = "PREP.OK"
PREP_UNMET = "PREP.UNMET"
PREP_FAIL = "PREP.FAIL"
PREP_UNSUPPORTED = "PREP.UNSUPPORTED"
PREP_UNKNOWN = "PREP.UNKNOWN"

COVERAGE_PASS = "pass"
COVERAGE_PRODUCT_FAIL = "product_fail"
COVERAGE_PREP = "prep_insufficient"
COVERAGE_STEP = "step_unexecutable"
COVERAGE_EXPECT = "expect_unverifiable"
COVERAGE_UNTESTABLE = "untestable"
COVERAGE_ENGINE = "engine_error"

COVERAGE_LABEL = {
    COVERAGE_PASS: "通过",
    COVERAGE_PRODUCT_FAIL: "校验不通过",
    COVERAGE_PREP: "执行期-前置准备不足",
    COVERAGE_STEP: "执行期-测试步骤无法执行",
    COVERAGE_EXPECT: "无法验证",
    COVERAGE_UNTESTABLE: "不可测",
    COVERAGE_ENGINE: "执行期-引擎故障",
}

GAP_CLASSES = frozenset(
    {COVERAGE_PREP, COVERAGE_STEP, COVERAGE_EXPECT, COVERAGE_UNTESTABLE, COVERAGE_ENGINE}
)
PRODUCT_RETRY_CLASSES = frozenset({COVERAGE_PRODUCT_FAIL, ""})

_UNSUPPORTED_PREP: List[Tuple[str, str]] = [
    ("web_config", r"web\s*端|web后台|运营后台|管理后台|后台配置|查看后台|后台.*开关|悬浮球开关"),
    ("remote_config", r"远程配置|远程开关|feature\s*flag|灰度开关"),
    ("backend_data", r"已购|指定订单|造数|服务端数据|号池标签"),
    ("sms_live", r"真短信|活短信|收到短信|短信验证码到达"),
    ("external_channel", r"接电话|来电|推送必达"),
    ("device_mock", r"地理围栏|模拟定位|时间旅行"),
]

_KIND_OK_CODE = {
    "clear_cache": "PREP.OK.clear_cache",
    "check_sim": "PREP.OK.sim",
    "check_wechat": "PREP.OK.wechat",
    "check_no_wechat": "PREP.OK.no_wechat",
    "check_ios_device": "PREP.OK.platform",
    "check_android_device": "PREP.OK.platform",
    "check_logged_in": "PREP.OK.session",
    "check_not_logged_in": "PREP.OK.session",
    "keep_permission_prompt": "PREP.OK.keep_permission",
}

_KIND_UNMET_CODE = {
    "clear_cache": "PREP.FAIL.clear_cache",
    "check_sim": "PREP.UNMET.sim",
    "check_wechat": "PREP.UNMET.wechat",
    "check_no_wechat": "PREP.UNMET.wechat",
    "check_ios_device": "PREP.UNMET.platform",
    "check_android_device": "PREP.UNMET.platform",
    "check_logged_in": "PREP.UNMET.session",
    "check_not_logged_in": "PREP.UNMET.session",
}

_UNSUPPORTED_KINDS = {k for k, _ in _UNSUPPORTED_PREP}
UNSUPPORTED_PREP_KINDS = _UNSUPPORTED_KINDS

_GAP_KIND_LABEL = {
    "web_config": "查后台配置",
    "remote_config": "远程开关",
    "backend_data": "造服务端数据",
    "sms_live": "真短信",
    "external_channel": "外部通道",
    "device_mock": "设备模拟",
    "sim_ios": "iOS 无法读 SIM",
    "av_call": "语音/视频通话",
    "camera_scene": "摄像头场景",
    "gesture_complex": "复杂手势",
    "external_app_pay": "外部支付",
    "hardware_fx": "听声道/震动/闪光",
    "cross_surface": "跨端操作",
    "animation": "动画/转场",
    "av_haptic": "声画震动",
    "subjective": "主观观感",
    "temporal": "连续多帧",
    "no_baseline": "无基线对比",
    "pixel_perfect": "像素级对齐",
    "tab_selected": "选中态/切页",
    "session_frame": "首页登录态",
}

_ENGINE_FAIL_CATS = frozenset(
    {"execution_error", "budget_exhausted", "device_unhealthy"}
)


def is_capability_gap_code(code: str) -> bool:
    """引擎做不到 / 认不出 / 验不了：记缺口，不挡开跑。"""
    c = str(code or "")
    return (
        c == PREP_UNKNOWN
        or c.startswith(PREP_UNSUPPORTED)
        or c.startswith("STEP.UNKNOWN")
        or c.startswith("STEP.UNSUPPORTED")
        or c.startswith("EXPECT.UNKNOWN")
        or c.startswith("EXPECT.UNVERIFIABLE")
    )


def gap_tag(code: str, kind: str = "") -> str:
    c = str(code or "")
    if not is_capability_gap_code(c):
        return ""
    tail = (kind or "").strip() or (c.split(".")[-1] if "." in c else "")
    if tail in {"UNKNOWN", "UNSUPPORTED", "UNVERIFIABLE"}:
        tail = kind or ""
    detail = _GAP_KIND_LABEL.get(tail, "")
    if "UNKNOWN" in c:
        return "无法识别 · 未命中引擎库"
    if "UNVERIFIABLE" in c:
        return f"无法验证 · {detail}" if detail else "无法验证"
    return f"无法执行 · {detail}" if detail else "无法执行"


def refine_precondition_kind(kind: str, text: str) -> str:
    k = (kind or "unknown").strip() or "unknown"
    t = text or ""
    if k == "unknown":
        try:
            from server.services.shared.semantic.case_text_semantic_service import (
                _classify_precondition_rules,
            )

            rk, _ = _classify_precondition_rules(t)
            if rk != "unknown":
                return rk
        except Exception:
            pass
    if k != "unknown":
        return k
    for skind, pat in _UNSUPPORTED_PREP:
        if re.search(pat, t, re.I):
            return skind
    return "unknown"


def reason_code_for_prep_item(item: Dict[str, Any], *, platform: str = "") -> str:
    kind = str(item.get("kind") or "unknown")
    plat = (platform or "").lower()
    ios = plat in ("ios", "iphone", "ipad") or bool(item.get("ios"))
    if kind in _UNSUPPORTED_KINDS:
        return f"{PREP_UNSUPPORTED}.{kind}"
    if kind == "unknown":
        return PREP_UNKNOWN
    if kind == "check_sim" and (ios or item.get("skipped")):
        if ios:
            return f"{PREP_UNSUPPORTED}.sim_ios"
    if item.get("ok"):
        return _KIND_OK_CODE.get(kind, f"{PREP_OK}.{kind}")
    return _KIND_UNMET_CODE.get(kind, f"{PREP_FAIL}.{kind}")


def stamp_precondition_items(
    items: List[Dict[str, Any]],
    *,
    platform: str = "",
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    plat = (platform or "").lower()
    ios = plat in ("ios", "iphone", "ipad")
    for raw in items or []:
        row = dict(raw)
        kind = refine_precondition_kind(str(row.get("kind") or "unknown"), str(row.get("text") or ""))
        row["kind"] = kind
        if ios:
            row["ios"] = True
        if kind in _UNSUPPORTED_KINDS or kind == "unknown":
            row["ok"] = True
            row["skipped"] = True
            row["gap"] = True
            if kind == "unknown":
                row["reason_code"] = PREP_UNKNOWN
                row["msg"] = row.get("msg") or f"前置未命中引擎库: {row.get('text') or ''}"
            else:
                row["reason_code"] = f"{PREP_UNSUPPORTED}.{kind}"
                row["msg"] = row.get("msg") or f"前置引擎无法执行（{kind}）"
            row["tag"] = gap_tag(row["reason_code"], kind)
        elif kind == "check_sim" and ios:
            row["ok"] = True
            row["skipped"] = True
            row["gap"] = True
            row["reason_code"] = f"{PREP_UNSUPPORTED}.sim_ios"
            row["msg"] = "iOS 无法读取 SIM，已跳过"
            row["tag"] = gap_tag(row["reason_code"], "sim_ios")
        else:
            row["reason_code"] = reason_code_for_prep_item(row, platform=platform)
            row["gap"] = False
            row["tag"] = ""
        out.append(row)
    return out


def prep_blocks_run(items: List[Dict[str, Any]]) -> bool:
    """只有真实检查没过才挡跑。库外 / 做不到记缺口并放过。"""
    for row in items or []:
        code = str(row.get("reason_code") or "")
        if is_capability_gap_code(code) or row.get("gap"):
            continue
        if not row.get("ok"):
            return True
        if code.startswith(PREP_UNMET) or code.startswith(PREP_FAIL):
            return True
    return False


def collect_gaps(
    prep: List[Dict[str, Any]],
    steps: Optional[List[Dict[str, Any]]] = None,
    expects: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for col, rows, text_key in (
        ("prep", prep or [], "text"),
        ("step", steps or [], "text"),
        ("expect", expects or [], "text"),
    ):
        for row in rows:
            code = str(row.get("reason_code") or row.get("code") or "")
            if not is_capability_gap_code(code):
                continue
            kind = str(row.get("kind") or "")
            out.append(
                {
                    "col": col,
                    "text": str(row.get(text_key) or ""),
                    "code": code,
                    "tag": str(row.get("tag") or "") or gap_tag(code, kind),
                }
            )
    return out


def _step_rows_from_spec(spec) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for step in getattr(spec, "steps", None) or []:
        n = int(getattr(step, "index", 0) or 0)
        if n <= 0:
            continue
        expected = str(getattr(step, "expected", "") or "").strip()
        rows.append(
            {
                "n": n,
                "text": str(getattr(step, "instruction", "") or "").strip(),
                "expected": expected,
            }
        )
    return rows


def outcome_code_list(value) -> List[str]:
    """一步可以有多条观察结论（进入首页过 + 选中态未观察）。"""
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x or "").strip()]
    s = str(value or "").strip()
    if not s:
        return []
    if "|" in s:
        return [p.strip() for p in s.split("|") if p.strip()]
    return [s]


def _expect_rows_for_step(n: int, text: str, codes: List[str]) -> List[Dict[str, Any]]:
    bits = [p.strip() for p in re.split(r"[。；;\n]+|，", str(text or "")) if p.strip()]
    if len(codes) > 1 and len(bits) == len(codes):
        return [{"n": n, "text": bit, "code": code} for bit, code in zip(bits, codes)]
    if not codes:
        return [{"n": n, "text": text, "code": "EXPECT.SKIPPED.step_not_done"}]
    if len(codes) == 1:
        return [{"n": n, "text": text, "code": codes[0]}]
    return [{"n": n, "text": text, "code": code} for code in codes]


def coverage_from_spec(
    spec,
    *,
    prep_items: Optional[List[Dict[str, Any]]] = None,
    overall: str = "",
    failure_category: str = "",
    blocked_reason: str = "",
    expect_outcomes: Optional[Dict[Any, str]] = None,
) -> Dict[str, Any]:
    prep = list(prep_items or [])
    steps_src = _step_rows_from_spec(spec)
    step_rows: List[Dict[str, Any]] = []
    expect_rows: List[Dict[str, Any]] = []
    blocked = prep_blocks_run(prep) or overall == COVERAGE_PREP
    stamped: Dict[int, List[str]] = {}
    for k, v in (expect_outcomes or {}).items():
        try:
            nk = int(k)
        except (TypeError, ValueError):
            continue
        codes = outcome_code_list(v)
        if nk > 0 and codes:
            stamped[nk] = codes
    last_n = max(stamped) if stamped else 0

    for row in steps_src:
        n = row["n"]
        exp = row["expected"]
        if blocked:
            step_rows.append({"n": n, "text": row["text"], "code": "STEP.SKIPPED.blocked"})
            if exp:
                expect_rows.append({"n": n, "text": exp, "code": "EXPECT.SKIPPED.blocked"})
            else:
                expect_rows.append({"n": n, "text": "", "code": "EXPECT.SKIPPED.no_expect"})
            continue
        if overall in ("untestable", COVERAGE_UNTESTABLE):
            step_rows.append({"n": n, "text": row["text"], "code": "STEP.SKIPPED.blocked"})
            expect_rows.append(
                {"n": n, "text": exp, "code": "EXPECT.SKIPPED.blocked" if exp else "EXPECT.SKIPPED.no_expect"}
            )
            continue
        if n in stamped:
            step_rows.append({"n": n, "text": row["text"], "code": "STEP.OK"})
            if exp:
                expect_rows.extend(_expect_rows_for_step(n, exp, stamped[n]))
            else:
                expect_rows.append({"n": n, "text": "", "code": "EXPECT.SKIPPED.no_expect"})
            continue
        if last_n and n > last_n:
            step_rows.append({"n": n, "text": row["text"], "code": "STEP.SKIPPED.blocked"})
            expect_rows.append(
                {"n": n, "text": exp, "code": "EXPECT.SKIPPED.step_not_done" if exp else "EXPECT.SKIPPED.no_expect"}
            )
            continue
        # 没看过的预期不得写成过或产品失败。
        step_rows.append({"n": n, "text": row["text"], "code": "STEP.OK" if last_n and n <= last_n else "STEP.SKIPPED.blocked"})
        if not exp:
            expect_rows.append({"n": n, "text": "", "code": "EXPECT.SKIPPED.no_expect"})
        else:
            expect_rows.append({"n": n, "text": exp, "code": "EXPECT.SKIPPED.step_not_done"})

    cls = classify_coverage_class(
        prep=prep,
        steps=step_rows,
        expects=expect_rows,
        overall=overall,
        failure_category=failure_category,
    )
    written = [e for e in expect_rows if e.get("code") != "EXPECT.SKIPPED.no_expect"]
    verifiable = [
        e for e in written if str(e.get("code") or "").startswith("EXPECT.PASS") or str(e.get("code") or "").startswith("EXPECT.FAIL")
    ]
    gaps = collect_gaps(prep, step_rows, expect_rows)
    return {
        "coverage_class": cls,
        "coverage_label": COVERAGE_LABEL.get(cls, cls),
        "prep": [
            {
                "seq": i + 1,
                "text": r.get("text") or "",
                "kind": r.get("kind") or "",
                "code": r.get("reason_code") or "",
                "msg": r.get("msg") or "",
                "skipped": bool(r.get("skipped")),
                "gap": bool(r.get("gap")) or is_capability_gap_code(str(r.get("reason_code") or "")),
                "tag": str(r.get("tag") or "") or gap_tag(str(r.get("reason_code") or ""), str(r.get("kind") or "")),
            }
            for i, r in enumerate(prep)
        ],
        "steps": step_rows,
        "expects": expect_rows,
        "gaps": gaps,
        "metrics": {
            "steps_total": len(step_rows),
            "steps_executable": sum(1 for s in step_rows if str(s.get("code") or "").startswith(("STEP.OK", "STEP.HEALED"))),
            "expects_written": len([e for e in expect_rows if e.get("text")]),
            "expects_verifiable": len(verifiable),
            "expects_failed": sum(1 for e in expect_rows if str(e.get("code") or "").startswith("EXPECT.FAIL")),
            "prep_unknown": sum(1 for r in prep if str(r.get("reason_code") or "") == PREP_UNKNOWN),
            "gaps_skipped": len(gaps),
            "blocked_reason": blocked_reason or "",
        },
    }


def classify_coverage_class(
    *,
    prep: List[Dict[str, Any]],
    steps: List[Dict[str, Any]],
    expects: List[Dict[str, Any]],
    overall: str = "",
    failure_category: str = "",
) -> str:
    if overall == "untestable" or failure_category == "untestable":
        return COVERAGE_UNTESTABLE
    if prep_blocks_run(prep) or overall == COVERAGE_PREP:
        return COVERAGE_PREP
    if failure_category in _ENGINE_FAIL_CATS:
        return COVERAGE_ENGINE
    if any(str(s.get("code") or "").startswith("STEP.FAIL") for s in steps):
        return COVERAGE_STEP
    if any(str(e.get("code") or "").startswith("EXPECT.FAIL") for e in expects):
        return COVERAGE_PRODUCT_FAIL
    if overall == "unverifiable" or failure_category in ("expect_unverifiable", COVERAGE_EXPECT):
        return COVERAGE_EXPECT
    if any(
        str(e.get("code") or "").startswith("EXPECT.UNVERIFIABLE")
        or str(e.get("code") or "") == "EXPECT.UNKNOWN"
        or str(e.get("code") or "").endswith("step_not_done")
        for e in expects
        if e.get("text")
    ):
        return COVERAGE_EXPECT
    if overall == "pass":
        return COVERAGE_PASS
    if overall == "fail":
        return COVERAGE_PRODUCT_FAIL
    if overall in ("blocked", "declined"):
        return COVERAGE_ENGINE
    return COVERAGE_PASS if overall in ("", "pass") else COVERAGE_PRODUCT_FAIL


def bump_run_counters(run_doc: Dict[str, Any], coverage_class: str) -> None:
    """按覆盖类记账：产品红进 failed，缺口进独立计数。"""
    cls = coverage_class or ""
    run_doc["completed"] = int(run_doc.get("completed") or 0) + 1
    if cls == COVERAGE_PASS:
        run_doc["passed"] = int(run_doc.get("passed") or 0) + 1
    elif cls == COVERAGE_PRODUCT_FAIL:
        run_doc["failed"] = int(run_doc.get("failed") or 0) + 1
    elif cls == COVERAGE_PREP:
        run_doc["prep_insufficient"] = int(run_doc.get("prep_insufficient") or 0) + 1
    elif cls == COVERAGE_STEP:
        run_doc["step_unexecutable"] = int(run_doc.get("step_unexecutable") or 0) + 1
    elif cls == COVERAGE_EXPECT:
        run_doc["expect_unverifiable"] = int(run_doc.get("expect_unverifiable") or 0) + 1
    elif cls == COVERAGE_UNTESTABLE:
        run_doc["untestable"] = int(run_doc.get("untestable") or 0) + 1
    elif cls == COVERAGE_ENGINE:
        run_doc["engine_error"] = int(run_doc.get("engine_error") or 0) + 1
    else:
        run_doc["failed"] = int(run_doc.get("failed") or 0) + 1


def is_product_retry(row: Dict[str, Any]) -> bool:
    cls = str(row.get("coverage_class") or "")
    if cls:
        return cls == COVERAGE_PRODUCT_FAIL
    status = str(row.get("status") or "")
    if status in {"blocked", "declined"}:
        return True
    if status in {"unverifiable", "untestable", "pass"}:
        return False
    if status in {"fail", "failed"}:
        if str(row.get("failure_category") or "") in {
            "prep_insufficient",
            "untestable",
            "execution_error",
            "budget_exhausted",
            "expect_unverifiable",
        }:
            return False
        return True
    return False
