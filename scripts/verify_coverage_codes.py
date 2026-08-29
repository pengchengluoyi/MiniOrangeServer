#!/usr/bin/env python3
"""覆盖码：缺口跳过不挡跑、缺号不验、iOS SIM。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server.services.regression.coverage_codes import (  # noqa: E402
    PREP_UNKNOWN,
    coverage_from_spec,
    refine_precondition_kind,
    stamp_precondition_items,
    prep_blocks_run,
    gap_tag,
)
from server.services.ai.regression.schemas import CaseSpec, CaseStep  # noqa: E402
from server.services.regression.case_runner import to_case_spec  # noqa: E402
from server.services.ai.regression.planner import _checkpoints_from_expected  # noqa: E402


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def test_refine():
    check(refine_precondition_kind("unknown", "已登录") == "check_logged_in", "已登录应回落到规则库")
    check(
        refine_precondition_kind("unknown", "已登录账号：需求上线前注册、未购买新人礼")
        == "check_logged_in",
        "已登录+账号标签仍是登录检查",
    )
    check(refine_precondition_kind("unknown", "查看运营后台配置") == "web_config", "web")
    check(refine_precondition_kind("unknown", "后台领取悬浮球开关为开") == "web_config", "后台开关")
    check(refine_precondition_kind("unknown", "环境准备完毕") == "unknown", "unknown")


def test_stamp_unknown_skips():
    items = stamp_precondition_items(
        [{"text": "环境准备完毕", "kind": "unknown", "ok": True, "skipped": True, "msg": "跳过"}],
        platform="android",
    )
    check(items[0]["ok"] is True, "unknown 应跳过而不是挡跑")
    check(items[0]["gap"] is True, items[0])
    check(items[0]["reason_code"] == PREP_UNKNOWN, items[0])
    check(not prep_blocks_run(items), "unknown 不得挡跑")
    check("无法识别" in (items[0].get("tag") or ""), items[0].get("tag"))


def test_unsupported_skips_unmet_blocks():
    gaps = stamp_precondition_items(
        [{"text": "后台领取悬浮球开关为开", "kind": "unknown"}],
        platform="android",
    )
    check(gaps[0]["kind"] == "web_config", gaps[0])
    check(gaps[0]["ok"] is True, "做不到应放过")
    check(not prep_blocks_run(gaps), "做不到不得挡跑")
    check("无法执行" in gap_tag(gaps[0]["reason_code"], "web_config"), gaps[0])

    unmet = stamp_precondition_items(
        [{"text": "已登录", "kind": "check_logged_in", "ok": False}],
        platform="android",
    )
    check(unmet[0]["ok"] is False, unmet[0])
    check(prep_blocks_run(unmet), "真实未登录应挡跑")


def test_ios_sim():
    items = stamp_precondition_items(
        [{"text": "已安装 SIM 卡", "kind": "check_sim", "ok": True, "skipped": True}],
        platform="ios",
    )
    check(items[0]["ok"] is True, "iOS SIM 做不到应跳过")
    check(items[0]["gap"] is True, items[0])
    check("sim_ios" in items[0]["reason_code"], items[0])
    check(not prep_blocks_run(items), "iOS SIM 不得挡跑")


def test_sparse_expected():
    spec = to_case_spec({
        "case_id": "c1",
        "name": "t",
        "precondition": "已登录",
        "steps": ["打开应用", "上滑浏览", "点击领取"],
        "step_nums": [1, 2, 3],
        "steps_raw": "1. 打开应用\n2. 上滑浏览\n3. 点击领取",
        "expected_raw": "1. 首页可见球\n3. 出现弹窗",
        "expected_by_step": {},
    })
    by = {s.index: s.expected for s in spec.steps}
    check(by[1] == "首页可见球", by)
    check(by[2] == "", by)
    check(by[3] == "出现弹窗", by)
    cps = _checkpoints_from_expected(spec)
    ids = [c.id for c in cps]
    check(ids == ["cp1", "cp3"], ids)


def test_coverage_prep():
    spec = CaseSpec(
        case_id="c1",
        name="t",
        steps=[
            CaseStep(index=1, instruction="打开", expected="到首页"),
            CaseStep(index=2, instruction="上滑", expected=""),
        ],
    )
    items = stamp_precondition_items(
        [{"text": "查看后台", "kind": "unknown", "ok": False}],
        platform="android",
    )
    cov = coverage_from_spec(spec, prep_items=items, overall="prep_insufficient")
    check(cov["coverage_class"] == "prep_insufficient", cov)
    check(cov["expects"][1]["code"] == "EXPECT.SKIPPED.no_expect", cov["expects"])
    check(cov["steps"][0]["code"] == "STEP.SKIPPED.blocked", cov["steps"])


def test_coverage_expect_unverifiable():
    spec = CaseSpec(
        case_id="c1",
        name="t",
        steps=[
            CaseStep(index=1, instruction="点击首页", expected="进入首页，底部为选中态"),
            CaseStep(index=2, instruction="查看悬浮球", expected="不出现领取悬浮球"),
        ],
    )
    cov = coverage_from_spec(
        spec,
        overall="pass",
        expect_outcomes={
            1: "EXPECT.PASS.page_nav|EXPECT.UNVERIFIABLE.tab_selected",
            2: "EXPECT.PASS.text_absent",
        },
    )
    check(cov["coverage_class"] == "expect_unverifiable", cov)
    codes = [e["code"] for e in cov["expects"] if e["n"] == 1]
    check("EXPECT.PASS.page_nav" in codes, codes)
    check("EXPECT.UNVERIFIABLE.tab_selected" in codes, codes)


def test_coverage_no_fake_pass_or_fail():
    spec = CaseSpec(
        case_id="c1",
        name="t",
        steps=[
            CaseStep(index=1, instruction="点击", expected="进入首页"),
            CaseStep(index=2, instruction="再点", expected="出现弹窗"),
        ],
    )
    green = coverage_from_spec(spec, overall="pass")
    check(green["coverage_class"] == "expect_unverifiable", green)
    check(all("PASS" not in e["code"] or "SKIPPED" in e["code"] for e in green["expects"]), green["expects"])
    red = coverage_from_spec(
        spec,
        overall="fail",
        failure_category="expect_fail",
        expect_outcomes={1: "EXPECT.FAIL.page_nav"},
    )
    check(red["coverage_class"] == "product_fail", red)
    later = [e for e in red["expects"] if e["n"] == 2]
    check(later and "FAIL" not in later[0]["code"], later)


def test_coverage_fail_not_prep():
    spec = CaseSpec(
        case_id="c1",
        name="t",
        steps=[CaseStep(index=1, instruction="点击", expected="出现弹窗")],
    )
    cov = coverage_from_spec(
        spec,
        overall="fail",
        failure_category="expect_fail",
        expect_outcomes={1: "EXPECT.FAIL.text_present"},
    )
    check(cov["coverage_class"] == "product_fail", cov)


def test_fail_wins_over_unverifiable():
    spec = CaseSpec(
        case_id="c1",
        name="t",
        steps=[
            CaseStep(index=1, instruction="点击首页", expected="进入首页，底部为选中态"),
            CaseStep(index=2, instruction="查看悬浮球", expected="不出现领取悬浮球"),
        ],
    )
    cov = coverage_from_spec(
        spec,
        overall="fail",
        failure_category="expect_fail",
        expect_outcomes={1: "EXPECT.FAIL.page_nav|EXPECT.UNVERIFIABLE.tab_selected"},
    )
    check(cov["coverage_class"] == "product_fail", cov)
    later = [e for e in cov["expects"] if e["n"] == 2]
    check(later and "FAIL" not in later[0]["code"], later)
    check(cov["coverage_label"] == "校验不通过", cov["coverage_label"])


def test_overall_unverifiable_not_pass():
    spec = CaseSpec(
        case_id="c1",
        name="t",
        steps=[CaseStep(index=1, instruction="点击首页", expected="底部为选中态")],
    )
    cov = coverage_from_spec(
        spec,
        overall="unverifiable",
        failure_category="expect_unverifiable",
        expect_outcomes={1: "EXPECT.UNVERIFIABLE.tab_selected"},
    )
    check(cov["coverage_class"] == "expect_unverifiable", cov)
    check(cov["coverage_label"] == "无法验证", cov["coverage_label"])


if __name__ == "__main__":
    test_refine()
    test_stamp_unknown_skips()
    test_unsupported_skips_unmet_blocks()
    test_ios_sim()
    test_sparse_expected()
    test_coverage_prep()
    test_coverage_expect_unverifiable()
    test_coverage_no_fake_pass_or_fail()
    test_coverage_fail_not_prep()
    test_fail_wins_over_unverifiable()
    test_overall_unverifiable_not_pass()
    print("ok")
