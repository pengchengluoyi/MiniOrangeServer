# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""四层编排：驱动 → 技能 → 角色 → 触发。

从下往上绑。运行时从上往下走。
驱动没有 prompt。技能、角色各有一份 prompt（prompt 仍在角色/技能源里）。
"""
from __future__ import annotations

from typing import Any, Dict, List

DRIVERS: List[Dict[str, Any]] = [
    {"id": "adb", "label": "真机 ADB", "kind": "device", "summary": "本机点按、截图、装包"},
    {"id": "claw", "label": "Claw 远程", "kind": "device", "summary": "远程设备执行"},
    {"id": "im.send", "label": "IM 发消息", "kind": "channel", "summary": "飞书 / 微信 / 企微 / 钉钉 / Slack 回消息", "plugin_ids": ["feishu", "wechat", "wecom", "dingtalk", "slack"]},
    {"id": "zentao.submit", "label": "禅道建单", "kind": "plugin", "summary": "把缺陷写到禅道", "plugin_ids": ["zentao"]},
    {"id": "feishu.wiki", "label": "飞书 Wiki", "kind": "plugin", "summary": "落 Wiki 文件夹（尚未接线）", "plugin_ids": ["feishu"]},
    {"id": "figma.sync", "label": "Figma 拉稿", "kind": "plugin", "summary": "按应用拉设计稿", "plugin_ids": ["figma"]},
]

SKILL_CATEGORIES: List[Dict[str, str]] = [
    {"id": "flow", "label": "流程产出", "desc": "读需求、写脑图和用例、出验收或发版草稿"},
    {"id": "device", "label": "设备操作", "desc": "真机上规划、点按、定位和断言"},
    {"id": "channel", "label": "通道对话", "desc": "IM 里回答、下令、提缺陷，或问人"},
    {"id": "sync", "label": "外部同步", "desc": "写到 Wiki 等外部系统"},
]

SKILLS: List[Dict[str, Any]] = [
    {"id": "im.dialogue", "label": "IM 对话", "owner": "im-qa-assistant", "summary": "在通道里回答和下令", "intent": "talk", "category": "channel"},
    {"id": "im.defect", "label": "IM 提缺陷", "owner": "im-defect-assistant", "summary": "把说清的缺陷整理成单", "intent": "act", "category": "channel"},
    {"id": "analyze_req", "label": "拆验收标准", "owner": "req-analyst", "summary": "读原文，拆测试点", "intent": "persist", "category": "flow"},
    {"id": "propose_atlas", "label": "建议图谱", "owner": "req-analyst", "summary": "出品变更等人确认", "intent": "persist", "category": "flow"},
    {"id": "draft_mindmap", "label": "写测试脑图", "owner": "mindmap-writer", "summary": "按入口和端铺脑图", "intent": "persist", "category": "flow"},
    {"id": "draft_cases", "label": "写用例草稿", "owner": "case-writer", "summary": "按测试点出步骤", "intent": "persist", "category": "flow"},
    {"id": "map_cases", "label": "对照用例库", "owner": "req-qa-bm", "summary": "看覆盖够不够", "intent": "persist", "category": "flow"},
    {"id": "draft_sign", "label": "验收草稿", "owner": "req-qa-bm", "summary": "出建议，结论人点", "intent": "persist", "category": "flow"},
    {"id": "pick_regression", "label": "圈回归范围", "owner": "version-qa-bm", "summary": "圈本版回归用例", "intent": "persist", "category": "flow"},
    {"id": "draft_gate", "label": "发版草稿", "owner": "version-qa-bm", "summary": "出建议，结论人点", "intent": "persist", "category": "flow"},
    {"id": "pick_account", "label": "筛测试账号", "owner": "test-engineer", "summary": "按场景选号", "intent": "persist", "category": "flow"},
    {"id": "goal-extract", "label": "抽取目标", "owner": "test-engineer", "summary": "开跑前抽出检查点", "intent": "act", "category": "device"},
    {"id": "agent-decide", "label": "看图决策", "owner": "test-engineer", "summary": "每一步决定下一个动作", "intent": "act", "category": "device"},
    {"id": "assert-vision", "label": "视觉断言", "owner": "test-engineer", "summary": "检查点是否达成", "intent": "act", "category": "device"},
    {"id": "plan-overview", "label": "规划步骤", "owner": "test-engineer", "summary": "Plan 模式先排事件", "intent": "act", "category": "device"},
    {"id": "locate-vision", "label": "视觉定位", "owner": "test-engineer", "summary": "根据截图给出点击坐标", "intent": "act", "category": "device"},
    {"id": "single-step-replan", "label": "失败重规划", "owner": "test-engineer", "summary": "某一步失败后只重规划这一步", "intent": "act", "category": "device"},
    {"id": "hitl-composer", "label": "问人话术", "owner": "test-engineer", "summary": "把卡住的步骤改写成问人的话", "intent": "talk", "category": "channel"},
    {"id": "persona-task", "label": "拟人路径", "owner": "test-engineer", "summary": "清缓存等系统路径的拟人展开", "intent": "act", "category": "device"},
    {"id": "publish_wiki", "label": "写入 Wiki", "owner": "doc-keeper", "summary": "人确认后落文件夹", "intent": "act", "category": "sync"},
]

ROLES: List[Dict[str, str]] = [
    {"id": "conductor", "label": "分析师"},
    {"id": "req-analyst", "label": "需求分析师"},
    {"id": "mindmap-writer", "label": "脑图编写"},
    {"id": "case-writer", "label": "用例编写"},
    {"id": "req-qa-bm", "label": "需求测试 BM"},
    {"id": "version-qa-bm", "label": "版本测试 BM"},
    {"id": "test-engineer", "label": "测试工程师"},
    {"id": "report-writer", "label": "报告编写"},
    {"id": "doc-keeper", "label": "文档维护"},
    {"id": "im-qa-assistant", "label": "IM 总指挥"},
    {"id": "im-defect-assistant", "label": "IM 缺陷助手"},
    {"id": "knowledge-reviewer", "label": "知识审核员"},
    {"id": "product-expert", "label": "产品专家"},
]

TRIGGERS: List[Dict[str, Any]] = [
    {
        "id": "im_chat",
        "label": "IM 进线",
        "summary": "飞书或微信里有人私聊或 @机器人",
        "intents": ["dialogue", "defect"],
        "live": True,
        "effect": "改完立刻影响飞书 / 微信：谁来回、提缺陷去不去禅道。",
    },
    {
        "id": "qa_tick",
        "label": "继续分析",
        "summary": "需求 / 版本流程点「继续分析」",
        "intents": ["default"],
        "live": False,
        "effect": "现在仍按流程阶段里的 job 跑，改这里暂时没影响。",
    },
    {
        "id": "case_run",
        "label": "跑用例",
        "summary": "对应用和设备下发自动化",
        "intents": ["default"],
        "live": False,
        "effect": "现在仍走原来的执行器，改这里暂时没影响。",
    },
    {
        "id": "settings_chat",
        "label": "设置页对话",
        "summary": "角色页里的试对话",
        "intents": ["default"],
        "live": False,
        "effect": "沙盒只打当前选中角色，不读这张表。",
    },
    {
        "id": "atlas_confirm",
        "label": "确认图谱",
        "summary": "人点过的骨架变更",
        "intents": ["default"],
        "live": False,
        "effect": "确认图谱仍走原来的保存逻辑。",
    },
]

DEFAULT_SKILL_DRIVERS: Dict[str, List[str]] = {
    "im.dialogue": ["im.send"],
    "im.defect": ["zentao.submit", "im.send"],
    "goal-extract": ["adb", "claw"],
    "agent-decide": ["adb", "claw"],
    "assert-vision": ["adb", "claw"],
    "plan-overview": ["adb", "claw"],
    "locate-vision": ["adb", "claw"],
    "single-step-replan": ["adb", "claw"],
    "persona-task": ["adb", "claw"],
    "publish_wiki": ["feishu.wiki"],
}

DEFAULT_ROLE_SKILLS: Dict[str, List[str]] = {
    "im-qa-assistant": ["im.dialogue"],
    "im-defect-assistant": ["im.defect"],
    "req-analyst": ["analyze_req", "propose_atlas"],
    "mindmap-writer": ["draft_mindmap"],
    "case-writer": ["draft_cases"],
    "req-qa-bm": ["map_cases", "draft_sign"],
    "version-qa-bm": ["pick_regression", "draft_gate"],
    "test-engineer": [
        "pick_account",
        "goal-extract",
        "agent-decide",
        "assert-vision",
        "plan-overview",
        "locate-vision",
        "single-step-replan",
        "hitl-composer",
        "persona-task",
    ],
    "doc-keeper": ["publish_wiki"],
    "conductor": [],
    "report-writer": [],
    "knowledge-reviewer": [],
    "product-expert": [],
}

DEFAULT_TRIGGER_ROLES: Dict[str, Dict[str, str]] = {
    "im_chat": {"dialogue": "im-qa-assistant", "defect": "im-defect-assistant"},
    "qa_tick": {"default": "conductor"},
    "case_run": {"default": "test-engineer"},
    "settings_chat": {"default": ""},
    "atlas_confirm": {"default": "req-analyst"},
}

DEFAULT_TRIGGER_SKILLS: Dict[str, Dict[str, str]] = {
    "im_chat": {"dialogue": "im.dialogue", "defect": "im.defect"},
    "qa_tick": {"default": ""},
    "case_run": {"default": "agent-decide"},
    "settings_chat": {"default": ""},
    "atlas_confirm": {"default": "propose_atlas"},
}

_INTENT_LABEL = {"dialogue": "问答", "defect": "提缺陷", "default": "默认"}

_DRIVER_IDS = {row["id"] for row in DRIVERS}
_SKILL_IDS = {row["id"] for row in SKILLS}
_ROLE_IDS = {row["id"] for row in ROLES}
_TRIGGER_IDS = {row["id"] for row in TRIGGERS}


def _store() -> Dict[str, Any]:
    from server.services.system_settings_service import get_layer_stack_store

    raw = get_layer_stack_store()
    return raw if isinstance(raw, dict) else {}


def _clean_id_list(raw: Any, allowed: set[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    if not isinstance(raw, list):
        return out
    for item in raw:
        sid = str(item or "").strip()
        if not sid or sid in seen or sid not in allowed:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _merged_skill_drivers() -> Dict[str, List[str]]:
    store = _store().get("skill_drivers")
    extra = store if isinstance(store, dict) else {}
    out = {sid: list(ids) for sid, ids in DEFAULT_SKILL_DRIVERS.items()}
    for sid in _SKILL_IDS:
        if sid in extra:
            out[sid] = _clean_id_list(extra.get(sid), _DRIVER_IDS)
        else:
            out.setdefault(sid, [])
    return out


def _merged_role_skills() -> Dict[str, List[str]]:
    store = _store().get("role_skills")
    extra = store if isinstance(store, dict) else {}
    out = {rid: list(ids) for rid, ids in DEFAULT_ROLE_SKILLS.items()}
    for rid in _ROLE_IDS:
        if rid in extra:
            out[rid] = _clean_id_list(extra.get(rid), _SKILL_IDS)
        else:
            out.setdefault(rid, [])
    return out


def _merged_trigger_roles() -> Dict[str, Dict[str, str]]:
    store = _store().get("trigger_roles")
    extra = store if isinstance(store, dict) else {}
    out: Dict[str, Dict[str, str]] = {tid: dict(row) for tid, row in DEFAULT_TRIGGER_ROLES.items()}
    for tid, row in extra.items():
        if tid not in _TRIGGER_IDS or not isinstance(row, dict):
            continue
        cur = out.setdefault(tid, {})
        for intent, role_id in row.items():
            rid = str(role_id or "").strip()
            if rid and rid not in _ROLE_IDS:
                continue
            cur[str(intent)] = rid
    return out


def _merged_trigger_skills() -> Dict[str, Dict[str, str]]:
    store = _store().get("trigger_skills")
    extra = store if isinstance(store, dict) else {}
    out: Dict[str, Dict[str, str]] = {tid: dict(row) for tid, row in DEFAULT_TRIGGER_SKILLS.items()}
    for tid, row in extra.items():
        if tid not in _TRIGGER_IDS or not isinstance(row, dict):
            continue
        cur = out.setdefault(tid, {})
        for intent, skill_id in row.items():
            sid = str(skill_id or "").strip()
            if sid and sid not in _SKILL_IDS:
                continue
            cur[str(intent)] = sid
    return out


def _driver_ready(row: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(row.get("kind") or "")
    if kind == "device":
        return {"ready": True, "hint": "是否在线看运行状态"}
    plugin_ids = list(row.get("plugin_ids") or [])
    if not plugin_ids:
        return {"ready": True, "hint": ""}
    try:
        from server.services.system_settings_service import _merged_plugin_config, _plugin_configured

        ready = any(_plugin_configured(pid, _merged_plugin_config(pid)) for pid in plugin_ids)
    except Exception:
        ready = False
    return {"ready": ready, "hint": "已连接" if ready else "还没连上"}


def skills_for_role(role_id: str) -> List[str]:
    return list(_merged_role_skills().get(str(role_id or "").strip()) or [])


def drivers_for_skill(skill_id: str) -> List[str]:
    return list(_merged_skill_drivers().get(str(skill_id or "").strip()) or [])


def resolve_trigger(trigger_id: str, *, intent: str = "") -> Dict[str, str]:
    tid = str(trigger_id or "").strip()
    kind = str(intent or "").strip() or "default"
    row = _merged_trigger_roles().get(tid) or {}
    role_id = str(row.get(kind) or row.get("default") or "").strip()
    return {"trigger_id": tid, "intent": kind, "role_id": role_id}


def resolve_skill_for_role(role_id: str, *, intent: str = "") -> str:
    skills = skills_for_role(role_id)
    kind = str(intent or "").strip()
    if kind == "defect" and "im.defect" in skills:
        return "im.defect"
    if kind == "dialogue" and "im.dialogue" in skills:
        return "im.dialogue"
    return skills[0] if skills else ""


def resolve_im(*, plugin_id: str, intent: str) -> Dict[str, Any]:
    kind = str(intent or "dialogue").strip() or "dialogue"
    if kind not in ("dialogue", "defect"):
        kind = "dialogue"
    pid = str(plugin_id or "").strip()
    bound = resolve_trigger("im_chat", intent=kind)
    role_id = bound["role_id"] or ("im-defect-assistant" if kind == "defect" else "im-qa-assistant")
    skill_id = str((_merged_trigger_skills().get("im_chat") or {}).get(kind) or "").strip()
    if not skill_id:
        skill_id = resolve_skill_for_role(role_id, intent=kind) or ("im.defect" if kind == "defect" else "im.dialogue")
    drivers = drivers_for_skill(skill_id)
    submit = "zentao" if "zentao.submit" in drivers else ""
    return {
        "intent": kind,
        "role_id": role_id,
        "skill_id": skill_id,
        "plugin_id": pid,
        "capability": "chat",
        "use": "intake" if kind == "defect" else "channel",
        "job": "im_defect" if kind == "defect" else "im_dialogue",
        "submit_plugin_id": submit,
        "submit_capability": "flow" if submit else "",
        "driver_ids": drivers,
    }


def im_roles_for_plugin(plugin_id: str = "") -> Dict[str, str]:
    del plugin_id
    roles = _merged_trigger_roles().get("im_chat") or {}
    return {
        "dialogue": str(roles.get("dialogue") or "im-qa-assistant"),
        "defect": str(roles.get("defect") or "im-defect-assistant"),
    }


def get_stack() -> Dict[str, Any]:
    skill_drivers = _merged_skill_drivers()
    role_skills = _merged_role_skills()
    trigger_roles = _merged_trigger_roles()
    trigger_skills = _merged_trigger_skills()
    drivers = []
    for row in DRIVERS:
        status = _driver_ready(row)
        drivers.append(
            {
                **row,
                **status,
                "skill_ids": [sid for sid, ids in skill_drivers.items() if row["id"] in ids],
            }
        )
    cat_label = {row["id"]: row["label"] for row in SKILL_CATEGORIES}
    skills = []
    for row in SKILLS:
        category = str(row.get("category") or "flow")
        skills.append(
            {
                **row,
                "category": category,
                "category_label": cat_label.get(category, category),
                "driver_ids": list(skill_drivers.get(row["id"]) or []),
                "role_ids": [rid for rid, ids in role_skills.items() if row["id"] in ids],
            }
        )
    roles = []
    for row in ROLES:
        roles.append({**row, "skill_ids": list(role_skills.get(row["id"]) or [])})
    triggers = []
    for row in TRIGGERS:
        roles_map = dict(trigger_roles.get(row["id"]) or {})
        skills_map = dict(trigger_skills.get(row["id"]) or {})
        paths = []
        for intent in row.get("intents") or ["default"]:
            role_id = str(roles_map.get(intent) or roles_map.get("default") or "").strip()
            skill_id = str(skills_map.get(intent) or "").strip()
            if not skill_id:
                owned = list(role_skills.get(role_id) or [])
                if intent == "defect" and "im.defect" in owned:
                    skill_id = "im.defect"
                elif intent == "dialogue" and "im.dialogue" in owned:
                    skill_id = "im.dialogue"
                elif owned:
                    skill_id = owned[0]
            paths.append(
                {
                    "intent": intent,
                    "intent_label": _INTENT_LABEL.get(intent, intent),
                    "role_id": role_id,
                    "skill_id": skill_id,
                    "driver_ids": list(skill_drivers.get(skill_id) or []),
                }
            )
        triggers.append(
            {
                **row,
                "roles": roles_map,
                "skills": skills_map,
                "paths": paths,
                "intent_labels": {key: _INTENT_LABEL.get(key, key) for key in (row.get("intents") or ["default"])},
            }
        )
    return {
        "drivers": drivers,
        "skill_categories": list(SKILL_CATEGORIES),
        "skills": skills,
        "roles": roles,
        "triggers": triggers,
        "intent_labels": dict(_INTENT_LABEL),
        "custom": {
            "skill_drivers": "skill_drivers" in _store(),
            "role_skills": "role_skills" in _store(),
            "trigger_roles": "trigger_roles" in _store(),
            "trigger_skills": "trigger_skills" in _store(),
        },
    }


def save_bindings(body: Dict[str, Any] | None, *, reset: bool = False) -> Dict[str, Any]:
    from server.services.system_settings_service import write_layer_stack_store

    if reset:
        write_layer_stack_store({})
        return get_stack()
    incoming = body if isinstance(body, dict) else {}
    store = dict(_store())
    if "skill_drivers" in incoming and isinstance(incoming["skill_drivers"], dict):
        store["skill_drivers"] = {
            sid: _clean_id_list(ids, _DRIVER_IDS)
            for sid, ids in incoming["skill_drivers"].items()
            if str(sid) in _SKILL_IDS
        }
    if "role_skills" in incoming and isinstance(incoming["role_skills"], dict):
        store["role_skills"] = {
            rid: _clean_id_list(ids, _SKILL_IDS)
            for rid, ids in incoming["role_skills"].items()
            if str(rid) in _ROLE_IDS
        }
    if "trigger_roles" in incoming and isinstance(incoming["trigger_roles"], dict):
        cleaned: Dict[str, Dict[str, str]] = {}
        for tid, row in incoming["trigger_roles"].items():
            if tid not in _TRIGGER_IDS or not isinstance(row, dict):
                continue
            cleaned[tid] = {}
            for intent, role_id in row.items():
                rid = str(role_id or "").strip()
                if rid and rid not in _ROLE_IDS:
                    continue
                cleaned[tid][str(intent)] = rid
        store["trigger_roles"] = cleaned
    if "trigger_skills" in incoming and isinstance(incoming["trigger_skills"], dict):
        cleaned_skills: Dict[str, Dict[str, str]] = {}
        for tid, row in incoming["trigger_skills"].items():
            if tid not in _TRIGGER_IDS or not isinstance(row, dict):
                continue
            cleaned_skills[tid] = {}
            for intent, skill_id in row.items():
                sid = str(skill_id or "").strip()
                if sid and sid not in _SKILL_IDS:
                    continue
                cleaned_skills[tid][str(intent)] = sid
        store["trigger_skills"] = cleaned_skills
    write_layer_stack_store(store)
    return get_stack()
