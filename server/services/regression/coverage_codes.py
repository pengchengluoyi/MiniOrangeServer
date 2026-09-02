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

_PREP_DEFER: List[Tuple[str, str]] = [
    ("web_config", r"web\s*端|web后台|运营后台|管理后台|后台配置|查看后台|后台.*开关|悬浮球开关"),
]

_UNSUPPORTED_PREP: List[Tuple[str, str]] = [
    ("remote_config", r"远程配置|远程开关|feature\s*flag|灰度开关"),
    ("backend_data", r"已购|指定订单|造数|服务端数据|号池标签|账号标签"),
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
    "web_config": "PREP.OK.deferred",
    "check_app_version": "PREP.OK.app_version",
    "check_app_foreground": "PREP.OK.foreground",
    "check_env": "PREP.OK.env",
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
    "check_app_version": "PREP.UNMET.app_version",
    "check_app_foreground": "PREP.UNMET.foreground",
    "check_env": "PREP.UNMET.env",
}

_UNSUPPORTED_KINDS = {k for k, _ in _UNSUPPORTED_PREP}
UNSUPPORTED_PREP_KINDS = _UNSUPPORTED_KINDS
_PREP_DEFER_KINDS = {k for k, _ in _PREP_DEFER}

_GAP_KIND_LABEL = {
    "remote_config": "远程开关",
    "backend_data": "造服务端数据",
    "sms_live": "真短信",
    "external_channel": "外部通道",
    "device_mock": "设备模拟",
    "sim_ios": "iOS 无法读 SIM",
    "check_app_version": "读版本",
    "check_app_foreground": "读前台",
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
    """只认白名单 kind。不再用中文关键字从原文猜。"""
    del text
    from server.services.runtime.session_gate import PREP_KINDS

    k = (kind or "unknown").strip() or "unknown"
    return k if k in PREP_KINDS else "unknown"


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
        if kind in _PREP_DEFER_KINDS:
            row["ok"] = True
            row["skipped"] = True
            row["gap"] = False
            row["reason_code"] = f"{PREP_OK}.deferred"
            row["msg"] = row.get("msg") or "后台开关由用例步骤/预期验证，不单独查后台"
            row["tag"] = ""
        elif kind in _UNSUPPORTED_KINDS or kind == "unknown":
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
        elif kind in ("check_app_version", "check_app_foreground") and (
            ios or plat in ("web", "browser", "playwright")
        ):
            row["ok"] = True
            row["skipped"] = True
            row["gap"] = True
            row["reason_code"] = f"{PREP_UNSUPPORTED}.{kind}"
            row["msg"] = row.get("msg") or (
                "网页通道不读安装包版本/前台" if plat in ("web", "browser", "playwright")
                else "iOS 尚未接 dumpsys 读版本/前台"
            )
            row["tag"] = gap_tag(row["reason_code"], kind)
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
    if not codes:
        return [{"n": n, "text": text, "code": "EXPECT.SKIPPED.step_not_done"}]
    from server.services.regression.expect_catalog import split_expect_clauses

    clauses = split_expect_clauses(text)
    if len(codes) == 1:
        return [{"n": n, "text": text, "code": codes[0]}]
    rows: List[Dict[str, Any]] = []
    for i, code in enumerate(codes):
        bit = clauses[i] if i < len(clauses) else (clauses[-1] if clauses else text)
        rows.append({"n": n, "text": bit, "code": code})
    return rows


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
    if failure_category in ("prep_insufficient", COVERAGE_PREP) or overall == COVERAGE_PREP:
        return COVERAGE_PREP
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
    if failure_category == "step_unexecutable" or overall in ("unexecutable", COVERAGE_STEP):
        return COVERAGE_STEP
    if expects and all(
        str(e.get("code") or "") == "EXPECT.SKIPPED.no_expect" or not str(e.get("text") or "").strip()
        for e in expects
    ):
        return COVERAGE_STEP
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


# ---------- 签收三态：成立 / 不成立 / 未观察 ----------

STATE_HELD = "held"
STATE_FAILED = "failed"
STATE_UNOBSERVED = "unobserved"

STATE_LABEL = {
    STATE_HELD: "成立",
    STATE_FAILED: "不成立",
    STATE_UNOBSERVED: "未观察",
}

REASON_NOT_REACHED = "not_reached"
REASON_NO_SCENE = "no_scene"
REASON_CANT_SEE = "cant_see"
REASON_HUMAN = "human_unsigned"

REASON_LABEL = {
    REASON_NOT_REACHED: "没跑到",
    REASON_NO_SCENE: "场景没有",
    REASON_CANT_SEE: "这句看不了",
    REASON_HUMAN: "人手未签",
}

_PROXY_SELECTED = re.compile(r"选中|高亮")
_PROXY_BALL = re.compile(r"悬浮球|领取球|投放球")
_NAV_TEXT = re.compile(r"进入|到了|打开了")
_PENDING_STATUS = frozenset({"pending", "queued", "running"})


def observation_state(code: str) -> str:
    c = str(code or "")
    if c.startswith("EXPECT.PASS"):
        return STATE_HELD
    if c.startswith("EXPECT.FAIL"):
        return STATE_FAILED
    if c == "EXPECT.SKIPPED.no_expect":
        return ""
    return STATE_UNOBSERVED


def unobserved_reason(
    code: str,
    *,
    coverage_class: str = "",
    case_status: str = "",
) -> str:
    c = str(code or "")
    st = str(case_status or "")
    cls = str(coverage_class or "")
    if st == "blocked":
        return REASON_HUMAN
    if st in {"pending", "queued", "cancelled", "skipped"}:
        return REASON_NOT_REACHED
    if "step_not_done" in c:
        return REASON_NOT_REACHED
    if cls == COVERAGE_PREP or "SKIPPED.blocked" in c:
        return REASON_NO_SCENE
    if cls in {COVERAGE_ENGINE, COVERAGE_STEP}:
        return REASON_NOT_REACHED
    if "UNVERIFIABLE" in c or c == "EXPECT.UNKNOWN" or cls in {COVERAGE_EXPECT, COVERAGE_UNTESTABLE}:
        return REASON_CANT_SEE
    return REASON_CANT_SEE


def _is_proxy_held(point_text: str, rows: List[Dict[str, Any]]) -> bool:
    """进了首页不能代替「球在不在 / 选中态」。"""
    pt = str(point_text or "")
    held = [r for r in rows if r.get("state") == STATE_HELD]
    if not held:
        return False
    only_nav = all(
        "page_nav" in str(r.get("code") or "") or _NAV_TEXT.search(str(r.get("text") or ""))
        for r in held
    )
    if not only_nav:
        return False
    if _PROXY_SELECTED.search(pt):
        return True
    if _PROXY_BALL.search(pt) and not any(re.search(r"球|悬浮", str(r.get("text") or "")) for r in held):
        return True
    return False


def _count_unknown_by_col(cov: Dict[str, Any]) -> Dict[str, int]:
    counts = {"prep": 0, "step": 0, "expect": 0}
    gaps = cov.get("gaps") if isinstance(cov, dict) else None
    if gaps:
        for gap in gaps:
            col = str(gap.get("col") or "")
            if col in counts:
                counts[col] += 1
        return counts
    if not isinstance(cov, dict):
        return counts
    for col, key in (("prep", "prep"), ("step", "steps"), ("expect", "expects")):
        for row in cov.get(key) or []:
            code = str(row.get("code") or row.get("reason_code") or "")
            if is_capability_gap_code(code):
                counts[col] += 1
    return counts


def _synth_expects(case: Dict[str, Any]) -> List[Dict[str, Any]]:
    texts: List[Tuple[int, str]] = []
    ebs = case.get("expected_by_step") or {}
    if isinstance(ebs, dict) and ebs:
        for k in sorted(ebs, key=lambda x: int(x) if str(x).isdigit() else 0):
            t = str(ebs.get(k) or "").strip()
            if t:
                n = int(k) if str(k).isdigit() else len(texts) + 1
                texts.append((n, t))
    elif isinstance(case.get("expected"), list):
        for i, t in enumerate(case.get("expected") or [], 1):
            tt = str(t or "").strip()
            if tt:
                texts.append((i, tt))
    st = str(case.get("status") or "")
    if not texts and st in _PENDING_STATUS | {"cancelled", "skipped", "blocked"}:
        texts = [(0, str(case.get("summary") or case.get("name") or case.get("case_id") or ""))]
    cls = str(case.get("coverage_class") or "")
    code = "EXPECT.SKIPPED.blocked" if cls == COVERAGE_PREP else "EXPECT.SKIPPED.step_not_done"
    return [{"n": n, "text": t, "code": code} for n, t in texts if t]


def signoff_from_cases(
    cases: List[Dict[str, Any]],
    *,
    points: Optional[List[Dict[str, Any]]] = None,
    include_rows: bool = True,
) -> Dict[str, Any]:
    """任务签收：行优先测试点，否则每条观察句。未观察必须带原因。"""
    observations: List[Dict[str, Any]] = []
    unknown_by_col = {"prep": 0, "step": 0, "expect": 0}

    for case in cases or []:
        if not isinstance(case, dict):
            continue
        cid = str(case.get("case_id") or "")
        name = str(case.get("name") or cid)
        st = str(case.get("status") or "")
        cls = str(case.get("coverage_class") or "")
        cov = case.get("coverage") if isinstance(case.get("coverage"), dict) else {}
        pids = [str(x).strip() for x in (case.get("point_ids") or []) if str(x).strip()]
        col_counts = _count_unknown_by_col(cov)
        for col, n in col_counts.items():
            unknown_by_col[col] += n

        expects = [e for e in (cov.get("expects") or []) if isinstance(e, dict)]
        if not expects:
            expects = _synth_expects(case)

        for exp in expects:
            code = str(exp.get("code") or "")
            text = str(exp.get("text") or "").strip()
            if code == "EXPECT.SKIPPED.no_expect" and not text:
                continue
            state = observation_state(code)
            if not state:
                continue
            if st in _PENDING_STATUS:
                state = STATE_UNOBSERVED
                if "SKIPPED" not in code and "UNVERIFIABLE" not in code and "UNKNOWN" not in code:
                    code = "EXPECT.SKIPPED.step_not_done"
            reason = unobserved_reason(code, coverage_class=cls, case_status=st) if state == STATE_UNOBSERVED else ""
            try:
                n = int(exp.get("n") or 0)
            except (TypeError, ValueError):
                n = 0
            observations.append({
                "case_id": cid,
                "case_name": name,
                "sn": str(case.get("sn") or ""),
                "n": n,
                "text": text,
                "code": code,
                "state": state,
                "reason": reason,
                "reason_label": REASON_LABEL.get(reason, "") if reason else "",
                "point_ids": pids,
            })

    rows: List[Dict[str, Any]] = []
    point_list = [p for p in (points or []) if isinstance(p, dict)]
    case_ids = {str(c.get("case_id") or "") for c in (cases or []) if isinstance(c, dict)}

    def point_obs(p: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
        pid = str(p.get("id") or p.get("point_id") or "")
        cids = {str(x) for x in (p.get("case_ids") or []) if str(x)}
        matched = [
            o for o in observations
            if (pid and pid in (o.get("point_ids") or [])) or (cids and o.get("case_id") in cids)
        ]
        return pid, matched

    in_task: List[Tuple[Dict[str, Any], str, List[Dict[str, Any]]]] = []
    for p in point_list:
        pid, matched = point_obs(p)
        linked = any(str(x) in case_ids for x in (p.get("case_ids") or []))
        if matched or linked:
            in_task.append((p, pid, matched))

    if in_task:
        for p, pid, matched in in_task:
            title = str(p.get("text") or p.get("title") or pid)
            if p.get("waived"):
                state, reason = STATE_UNOBSERVED, REASON_HUMAN
            elif any(o["state"] == STATE_FAILED for o in matched):
                state, reason = STATE_FAILED, ""
            elif _is_proxy_held(title, matched):
                state, reason = STATE_UNOBSERVED, REASON_CANT_SEE
            elif matched and all(o["state"] == STATE_HELD for o in matched):
                state, reason = STATE_HELD, ""
            elif any(o["state"] == STATE_HELD for o in matched) and any(o["state"] == STATE_UNOBSERVED for o in matched):
                state = STATE_UNOBSERVED
                reason = next(
                    (o["reason"] for o in matched if o["state"] == STATE_UNOBSERVED and o.get("reason")),
                    REASON_CANT_SEE,
                )
            elif matched:
                state = STATE_UNOBSERVED
                reason = next((o["reason"] for o in matched if o.get("reason")), REASON_NOT_REACHED)
            else:
                state, reason = STATE_UNOBSERVED, REASON_NOT_REACHED
            case_id = matched[0]["case_id"] if matched else str((p.get("case_ids") or [""])[0] if p.get("case_ids") else "")
            rows.append({
                "kind": "point",
                "id": pid,
                "title": title,
                "state": state,
                "reason": reason,
                "reason_label": REASON_LABEL.get(reason, "") if reason else "",
                "case_id": case_id,
                "evidence": matched[:8] if include_rows else [],
            })
    else:
        for obs in observations:
            rows.append({
                "kind": "expect",
                "id": f"{obs['case_id']}:{obs['n']}:{obs['code']}",
                "title": obs["text"] or obs["case_name"],
                "state": obs["state"],
                "reason": obs["reason"],
                "reason_label": obs["reason_label"],
                "case_id": obs["case_id"],
                "case_name": obs["case_name"],
                "n": obs["n"],
                "code": obs["code"],
            })

    held = sum(1 for r in rows if r["state"] == STATE_HELD)
    failed = sum(1 for r in rows if r["state"] == STATE_FAILED)
    unobserved = sum(1 for r in rows if r["state"] == STATE_UNOBSERVED)
    denom = held + failed
    if failed:
        verdict = "product_fail"
    elif unobserved:
        verdict = "unobserved"
    elif held:
        verdict = "pass"
    else:
        verdict = "empty"

    out: Dict[str, Any] = {
        "held": held,
        "failed": failed,
        "unobserved": unobserved,
        "product_pass_rate": int(round(held / denom * 100)) if denom else None,
        "unknown_by_col": unknown_by_col,
        "task_verdict": verdict,
    }
    if include_rows:
        out["rows"] = rows
        out["observations"] = observations
    return out
