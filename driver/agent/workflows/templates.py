# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""
工作流节点图模板 — 与前端 Workflow.nodes 结构一致。
设备由运行时的 target_sn（前端选机）统一注入，节点 data 不写 udid。
"""
from __future__ import annotations

import driver.tentacle.common.platform as platform_code

TRIGGER_ID = "public-trigger-ios"
OPEN_ID = "ios-window-open"
SLEEP_ID = "cfs-sleep-launch"
GESTURE_ID = "ios-gesture-center"
CLOSE_ID = "ios-window-close"


def ios_open_tap_close(
    bundle_id: str,
    *,
    wait_seconds: float = 2.0,
    tap_normalized: tuple[float, float] = (0.5, 0.5),
    platform: str = platform_code.IOS,
) -> dict:
    """打开 App → 等待 → public/gesture 点击 → 关闭 App。"""
    window_data = {
        "platform": platform,
        "target_mobile": bundle_id,
        "operation": "start",
        "restart": False,
    }
    close_data = {**window_data, "operation": "close"}

    return {
        TRIGGER_ID: {
            "id": TRIGGER_ID,
            "nodeType": "normal",
            "nodeCode": "public/trigger",
            "displayName": "开始",
            "platform": platform,
            "lastCodes": [],
            "nextCodes": [OPEN_ID],
            "data": {"label": "开始"},
        },
        OPEN_ID: {
            "id": OPEN_ID,
            "nodeType": "normal",
            "nodeCode": "public/window",
            "displayName": "打开应用",
            "platform": platform,
            "lastCodes": [TRIGGER_ID],
            "nextCodes": [SLEEP_ID],
            "data": dict(window_data),
        },
        SLEEP_ID: {
            "id": SLEEP_ID,
            "nodeType": "normal",
            "nodeCode": "cfs/sleep",
            "displayName": "等待启动",
            "platform": platform,
            "lastCodes": [OPEN_ID],
            "nextCodes": [GESTURE_ID],
            "data": {"seconds": wait_seconds},
        },
        GESTURE_ID: {
            "id": GESTURE_ID,
            "nodeType": "normal",
            "nodeCode": "public/gesture",
            "displayName": "点击屏幕",
            "platform": platform,
            "lastCodes": [SLEEP_ID],
            "nextCodes": [CLOSE_ID],
            "data": {
                "platform": platform,
                "sub_type": "click",
                "position": [tap_normalized[0], tap_normalized[1]],
                "normalized": True,
            },
        },
        CLOSE_ID: {
            "id": CLOSE_ID,
            "nodeType": "normal",
            "nodeCode": "public/window",
            "displayName": "关闭应用",
            "platform": platform,
            "lastCodes": [GESTURE_ID],
            "nextCodes": [],
            "data": close_data,
        },
    }


def ios_magicam_demo() -> dict:
    return ios_open_tap_close("com.mathmagic.magicam")
