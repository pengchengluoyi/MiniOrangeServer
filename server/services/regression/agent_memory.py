# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Agent 成功轨迹记忆（P2 few-shot）。

agent 成功跑通一条 case 后，把「动作轨迹」存为 JSON；下次同 case（同设备指纹）执行时
作为提示喂给 decide_next_action，加速收敛、减少步数与 VLM 调用成本。

轻量文件存储（APP_DATA_DIR/data/agent_traj/），不依赖 DB schema。这是"经验"不是脚本，
决策模型可随时推翻——真实屏幕永远优先。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from script.log import SLog

TAG = "AgentMemory"


def _traj_dir() -> str:
    from server.core.database import APP_DATA_DIR

    d = os.path.join(APP_DATA_DIR, "data", "agent_traj")
    os.makedirs(d, exist_ok=True)
    return d


def _key(case_id: str, device_signature: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{case_id}__{device_signature or 'any'}")
    return safe[:120] + ".json"


def save_trajectory(case_id: str, device_signature: str, steps: list[dict[str, Any]]) -> None:
    """保存成功轨迹。steps: [{capability_id, params, thought}]。"""
    if not case_id or not steps:
        return
    try:
        path = os.path.join(_traj_dir(), _key(case_id, device_signature))
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"case_id": case_id, "device_signature": device_signature, "steps": steps},
                      f, ensure_ascii=False, indent=2)
        SLog.i(TAG, f"saved trajectory case={case_id} steps={len(steps)}")
    except Exception as e:  # pragma: no cover
        SLog.w(TAG, f"save_trajectory failed case={case_id}: {e}")


def load_trajectory(case_id: str, device_signature: str) -> Optional[list[dict[str, Any]]]:
    """读上次成功轨迹；无则回退忽略设备指纹再找一次。"""
    if not case_id:
        return None
    for sig in (device_signature, "any", ""):
        try:
            path = os.path.join(_traj_dir(), _key(case_id, sig))
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                steps = data.get("steps") if isinstance(data, dict) else None
                if steps:
                    return steps
        except Exception:
            continue
    # 精确 key 没命中时，扫一遍该 case 前缀的任意设备轨迹
    try:
        prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{case_id}__")[:120]
        for fn in os.listdir(_traj_dir()):
            if fn.startswith(prefix):
                with open(os.path.join(_traj_dir(), fn), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and data.get("steps"):
                    return data["steps"]
    except Exception:
        pass
    return None


def trajectory_to_hint(steps: Optional[list[dict[str, Any]]], *, max_steps: int = 12) -> str:
    """成功轨迹 → 供 prompt 的精简提示块。"""
    if not steps:
        return ""
    lines = []
    for i, s in enumerate(steps[:max_steps], start=1):
        cap = s.get("capability_id") or ""
        if not cap:
            continue
        params = s.get("params") or {}
        # 坐标可能因分辨率/布局变化而失效，只保留非坐标关键参数作参考
        kept = {k: v for k, v in params.items()
                if k not in ("x", "y", "from_x", "from_y", "to_x", "to_y")}
        thought = (s.get("thought") or "")[:50]
        lines.append(f"{i}. {cap} {kept if kept else ''} — {thought}")
    return "\n".join(lines)
