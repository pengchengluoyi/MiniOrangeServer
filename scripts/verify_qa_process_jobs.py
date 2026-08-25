#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验异步推进任务：进度、取消、同应用互斥、context 绑定。"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.services import qa_process_jobs as jobs  # noqa: E402


def main() -> int:
    failed = []

    def check(cond, msg):
        print(f"   {'PASS' if cond else 'FAIL'}  {msg}")
        if not cond:
            failed.append(msg)

    print("── 创建与进度")
    job = jobs.create(app_id="app-a", requirement_id="req-1", jobs=["draft_mindmap"])
    check(job.status == "running", "新建是 running")
    check(jobs.running_for("app-a") is job, "running_for 能找到")
    job.report(phase="mindmap_skeleton", label="骨架", done=0, total=3)
    job.report(inc=1)
    snap = job.public()
    check(snap["done"] == 1 and snap["total"] == 3, f"进度 {snap['done']}/{snap['total']}")
    job.add_total(2)
    check(job.public()["total"] == 5, "add_total 累加分母")

    print("\n── 同应用互斥")
    try:
        jobs.create(app_id="app-a", requirement_id="req-2")
        check(False, "同应用不应开第二个任务")
    except jobs.JobConflict as exc:
        check(exc.job.id == job.id, "冲突返回正在跑的那个")

    print("\n── 取消在下一次 check 生效")
    job.cancel()
    check(job.public()["status"] == "cancelled", "cancel 后状态是 cancelled")
    try:
        job.check()
        check(False, "check 应抛 Cancelled")
    except jobs.Cancelled:
        check(True, "check 抛 Cancelled")

    print("\n── 流式落库回调")
    saved = []
    job2 = jobs.create(app_id="app-b", requirement_id="req-1")
    job2.flush = lambda doc: saved.append(dict(doc))
    tok = jobs.bind(job2)
    try:
        jobs.report(phase="draft_cases", label="写用例", done=0, total=2)
        jobs.save({"requirements": [{"id": "req-1", "draft_cases": [{"case_id": "c1"}]}]})
        jobs.inc(1, label="一批")
        jobs.save({"requirements": [{"id": "req-1", "draft_cases": [{"case_id": "c1"}, {"case_id": "c2"}]}]})
    finally:
        jobs.reset(tok)
    check(len(saved) >= 2, f"flush 至少两次（实际 {len(saved)}）")
    check(saved[-1]["cover_job"]["done"] == 1, "落库快照带进度")
    check((saved[-1]["requirements"][0]["draft_cases"] or []).__len__() == 2, "落库带上已写用例")

    print("\n── finish 释放互斥")
    job2.finish({"qa_process": saved[-1], "actions": [], "usage": {}})
    jobs.release(job2)
    check(jobs.running_for("app-b") is None, "完成后 running_for 为空")
    check(job2.public()["status"] == "done", "finish → done")

    print("\n── contextvar 未绑定时 report 是空操作")
    jobs.report(phase="noop", label="不应崩")
    check(True, "未绑定不崩")

    # 清掉测试残留，避免污染同进程其它测试
    jobs.release(job)
    print("\n" + ("=== 全部通过 ===" if not failed else f"=== {len(failed)} 条失败 ==="))
    for m in failed:
        print(f"  · {m}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
