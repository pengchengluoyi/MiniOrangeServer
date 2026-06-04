# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""在设备节点子进程中执行跑图。"""
from __future__ import annotations

import builtins
import os
import time
import traceback
import uuid

from script.log import SLog


def crawl_runner_wrapper(params, msg_queue, server_http_url, shared_responses):
    import builtins as bi

    bi.REMOTE_API_URL = server_http_url
    req_id = params.get("req_id") or str(uuid.uuid4())
    target_sn = params.get("target_sn") or params.get("sn")
    platform = str(params.get("platform") or "android")
    if target_sn:
        from driver.agent.Crawl.device_bootstrap import resolve_mobile_serial
        bi.TARGET_DEVICE_SN = resolve_mobile_serial(str(target_sn), platform)

    def query_server(action, query_params, timeout=10):
        if shared_responses is None:
            return None
        qid = str(uuid.uuid4())
        msg_queue.put({
            "type": "query",
            "req_id": qid,
            "action": action,
            "params": query_params or {},
        })
        start = time.time()
        while time.time() - start < timeout:
            if qid in shared_responses:
                return shared_responses.pop(qid)
            time.sleep(0.05)
        return None

    bi.SERVER_QUERY = query_server

    def _queue_log(*args, **kwargs):
        pass

    from script.log import SLog as _SLog
    _SLog.set_log_callback(_queue_log)

    try:
        from driver.agent.Crawl.page_crawler import PageCrawler
        from driver.agent.Crawl.remote_persistence import RemoteCrawlPersistence

        graph_id = int(params["graph_id"])
        persist = RemoteCrawlPersistence(graph_id)
        crawler = PageCrawler(
            graph_id=graph_id,
            device_sn=str(target_sn or bi.TARGET_DEVICE_SN or ""),
            platform=platform,
            package_name=params.get("package") or params.get("package_name"),
            max_similarity=float(params.get("max_sim") or 0.85),
            min_similarity=float(params.get("min_sim") or 0.50),
            max_pages=int(params.get("max_pages") or 20),
        )
        report = crawler.crawl(
            persist=persist,
            seed_node_id=params.get("seed_node_id"),
            seed_label=str(params.get("seed_label") or "首页"),
        )
        payload = {
            "code": 200,
            "msg": "Crawl finished",
            "data": {
                "pages": [
                    {
                        "node_id": p.node_id,
                        "label": p.label,
                        "screenshots": p.screenshot_paths,
                    }
                    for p in report.pages
                ],
                "navigations": [
                    {"from": n.from_node_id, "to": n.to_node_id, "action": n.action}
                    for n in report.navigations
                ],
                "errors": report.errors,
            },
        }
    except Exception as e:
        SLog.e("CrawlRunner", traceback.format_exc())
        payload = {"code": 500, "msg": str(e), "data": {"errors": [str(e)]}}

    msg_queue.put({"type": "crawl_complete", "req_id": req_id, "data": payload})
