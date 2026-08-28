#!/usr/bin/env python
# -*-coding:utf-8 -*-
"""校验脚本用的说明书夹具。生产运行期读库，不读这个文件。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "zaohaowu.yaml"
ZHW_PKG = "com.mathmagic.zaohaowu"
OTHER_PKG = "com.someone.else"


def zaohaowu_playbook() -> dict:
    import yaml

    from server.services.ai.playbook_service import yaml_to_playbook

    raw = yaml.safe_load(FIXTURE.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"夹具不是 mapping: {FIXTURE}")
    return yaml_to_playbook(raw)


def zaohaowu_profile():
    from server.services.ai import app_profile as ap
    from server.services.ai.playbook_service import to_ui_override

    return ap.merge_override(ap.DEFAULT, to_ui_override(zaohaowu_playbook()))


def bind_zaohaowu():
    from server.services.ai.playbook_service import bind_profile

    return bind_profile(package=ZHW_PKG, playbook=zaohaowu_playbook())
