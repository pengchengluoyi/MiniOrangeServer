# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Packs 控制台 API：把 YAML 声明的扩展（capability / recovery / …）暴露给前端管理。

设计要点（对应 docs/plan-skill-packs-and-console.md §5/§7）：
  - 列表每行都带 **provider / owner / 作用域 / 命中统计 / 状态**，回答"谁提供、谁维护"；
  - `?fixture=1` 返回样例数据，前端不连设备也能开工；
  - 只读接口不碰设备、不调 LLM，可随时刷新。

写接口（启停 / 存 YAML）与 dry-run（单条试跑）都在本文件；
写入前一律先做 schema 校验，校验不过直接 400，绝不落盘半成品。
"""
from __future__ import annotations

import os
import tempfile
from typing import Any, Optional

import yaml
from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel, ValidationError

from script.log import SLog
from server.services.packs.exec_classes import (
    EXEC_KINDS,
    KIND_META as EXEC_KIND_META,
    class_of_cap,
    runtime_rows,
)

TAG = "rPacks"

router = APIRouter(prefix="/packs", tags=["Packs"])

# 列表/详情可查询的 kind。Tab 以 /packs/kinds 为准（四类执行能力 + 恢复/知识/判定）。
# capability 仍可 ?kind=capability 拉全部 YAML 能力（旧客户端 / 验收脚本）。
PACK_KINDS = ("recovery", "knowledge", "oracle")
KINDS = EXEC_KINDS + PACK_KINDS + ("capability",)

# 尚未落地的 kind：接口先返回空列表 + 明确原因，前端可照常渲染空态
_NOT_READY = {
    "oracle": "判定类目 kind 还未落地（见方案 §2.4）",
}

_TAB_LABELS = {
    **EXEC_KIND_META,
    "recovery": {"label": "恢复", "desc": "系统/设备异常怎么处置"},
    "knowledge": {"label": "知识", "desc": "这个应用的业务判据"},
    "oracle": {"label": "判定", "desc": "能不能测、怎么判、多严"},
}
_TAB_ORDER = EXEC_KINDS + PACK_KINDS


# ---------- 序列化 ----------


def _cap_row(cap) -> dict[str, Any]:
    """capability → 统一的 entry 行结构。

    capability 目前没有 provider/owner 字段（老 yaml 不带），统一按平台内置处理，
    这样前端一套列表能渲染四类，不必分支。
    """
    impls = [
        {
            "id": i.id,
            "executor": i.executor,
            "requires_caps": list(i.requires_caps or []),
            "cost": getattr(i, "cost", 5),
            "has_low_level": bool(getattr(i, "low_level", None)),
        }
        for i in (cap.implementations or [])
    ]
    # 「纯声明式」= 有 low_level 声明 **且** executor 里没有对应 Python 分支。
    # 老 yaml 大多也写了 low_level，但同时有 Python 实现，那不算纯声明式。
    try:
        from server.services.regression.executors.adb_executor import _SUPPORTED_CAPS

        has_python = cap.id in _SUPPORTED_CAPS
    except Exception:  # pragma: no cover
        has_python = False
    pure = any(i["has_low_level"] for i in impls) and not has_python
    return {
        "uid": f"builtin/capability/{cap.id}",
        "kind": "capability",
        "id": cap.id,
        "title": cap.display_name or cap.id,
        "enabled": True,
        "lifecycle": "active",
        "provider": getattr(cap, "provider", "") or "platform",
        "owner": getattr(cap, "owner", "") or "@platform",
        "root": "builtin",
        "scope": {
            "platforms": list(cap.platforms or []),
            "app_ids": [],
            "visible_to": list(getattr(cap, "visible_to", []) or ["case", "system"]),
        },
        "when": "",
        "summary": (cap.description or "").strip().splitlines()[0][:120] if cap.description else "",
        "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
        "source_path": cap.source_path,
        "detail": {
            "category": cap.category,
            "event_kind": cap.event_kind,
            "needs_vlm": cap.needs_vlm,
            "trigger_phrases": list(cap.trigger_phrases or []),
            "implementations": impls,
            # 纯声明式 = 无需 Python 分支即可执行（§3.1 的通用 low_level 契约）
            "pure_declarative": pure,
            "has_python_branch": has_python,
            "origin": "yaml",
            "exec_class": class_of_cap(cap.id),
        },
    }


def _recovery_row(rule, entry=None) -> dict[str, Any]:
    """recovery → entry 行。entry 来自多根 store（带根/包/覆盖关系）。"""
    manifest = getattr(entry, "manifest", None) if entry else None
    return {
        "uid": entry.uid if entry else f"builtin/recovery/{rule.id}",
        "kind": "recovery",
        "id": rule.id,
        "title": rule.title or rule.id,
        "enabled": bool(rule.enabled),
        "lifecycle": rule.lifecycle,
        "provider": rule.provider,
        "owner": rule.owner,
        "root": entry.root if entry else "builtin",
        "pack_id": entry.pack_id if entry else "",
        "overridden_by": entry.overridden_by if entry else "",
        "scope": {
            "platforms": list(getattr(manifest.scope, "platforms", []) if manifest else (rule.platforms or [])),
            "app_ids": list(entry.scope_app_ids) if entry else [],
            "app_versions": (manifest.scope.app_versions if manifest else ""),
            "visible_to": ["system"],
        },
        "when": rule.when,
        "summary": rule.when or (rule.prompt_snippet or "").strip().splitlines()[0][:120],
        "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
        "source_path": rule.source_path,
        "detail": {
            "mode": rule.mode,
            "priority": rule.priority,
            "max_attempts": rule.max_attempts,
            "match": {
                "evidence": dict(rule.match.evidence or {}),
                "screen_text_any": list(rule.match.screen_text_any or []),
                "top_window_pkg_prefix": list(rule.match.top_window_pkg_prefix or []),
            },
            "actions": [
                {"capability": a.capability, "params": dict(a.params or {}),
                 "target": dict(a.target or {}), "fallback_xy": list(a.fallback_xy or [])}
                for a in (rule.actions or [])
            ],
            "verify": {
                "evidence": dict(rule.verify.evidence or {}),
                "screen_text_any": list(rule.verify.screen_text_any or []),
                "top_window_pkg_prefix": list(rule.verify.top_window_pkg_prefix or []),
            },
            "forbid": {"text_any": list(rule.forbid.text_any or [])},
            "prompt_snippet": rule.prompt_snippet,
            "evidence_notes": list(rule.evidence_notes or []),
        },
    }


def _collect(kind: str) -> list[dict[str, Any]]:
    from server.services.plugins import registry

    if kind in EXEC_KINDS:
        yaml_rows = []
        for c in registry.list_capabilities():
            row = _cap_row(c)
            if class_of_cap(c.id) != kind:
                continue
            row = dict(row)
            row["kind"] = kind
            yaml_rows.append(row)
        return runtime_rows(kind) + yaml_rows
    if kind == "capability":
        return [_cap_row(c) for c in registry.list_capabilities()]
    if kind == "recovery":
        # 控制台要能看到 draft / deprecated / 被覆盖的条目，所以全都返回
        from server.services.packs import get_store

        return [_recovery_row(e.obj, e) for e in get_store().list_entries(kind="recovery")]
    if kind == "knowledge":
        from server.services.system_settings_service import list_testing_knowledge

        out: list[dict[str, Any]] = []
        for x in list_testing_knowledge():
            # 前端 PacksConsole 只关心：title/id/enabled/lifecycle/provider/owner/root/scope/when/summary/detail/source_path
            # 其中 source_path 为空时，PackEntryDrawer 会自动把知识条目当作“只读”（不显示编辑/YAML/启停）。
            title = str(x.get("title") or x.get("id") or "")
            content = str(x.get("content") or "")
            category = str(x.get("category") or "其他")
            tags = list(x.get("tags") or [])
            app_ids = list(x.get("app_ids") or [])

            out.append(
                {
                    "uid": f"learned/knowledge/{x.get('id')}",
                    "kind": "knowledge",
                    "id": str(x.get("id") or ""),
                    "title": title,
                    "enabled": bool(x.get("enabled", True)),
                    "lifecycle": "active",
                    "provider": "learned",
                    "owner": "@system",
                    "root": "learned",
                    "overridden_by": "",
                    "scope": {
                        "platforms": ["android"],
                        "app_ids": app_ids,
                        "app_versions": "",
                        "visible_to": ["case"],
                    },
                    "when": "",
                    "summary": content.strip().splitlines()[0][:120] if content else "",
                    "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
                    # knowledge 现阶段按“兼容只读镜像”约定：先不迁移，source_path 置空
                    "source_path": "",
                    "detail": {
                        "category": category,
                        "tags": tags,
                        "content": content,
                    },
                }
            )
        return out
    return []


def _health() -> dict[str, Any]:
    """加载健康度：坏条目按 kind 归并，前端顶部红条直接用。"""
    from server.services.plugins import registry

    from server.services.packs import get_store

    errors = list(registry.list_load_errors()) + list(get_store().errors())
    by_kind: dict[str, int] = {}
    for e in errors:
        by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
    return {
        "error_count": len(errors),
        "by_kind": by_kind,
        "errors": [
            {"path": e.path, "kind": e.kind, "message": e.message[:400],
             "file": e.path.rsplit("/", 1)[-1]}
            for e in errors
        ],
    }


# ---------- fixture：前端不连设备也能开工 ----------


def _fixture() -> dict[str, Any]:
    rows = [
        {
            "uid": "runtime/prep/pick_device", "kind": "prep",
            "id": "pick_device", "title": "申请执行设备", "enabled": True,
            "lifecycle": "active", "provider": "platform", "owner": "@platform",
            "root": "builtin",
            "scope": {"platforms": ["android", "ios", "web"], "app_ids": [], "visible_to": ["case"]},
            "when": "", "summary": "按用例占用当前环境设备",
            "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
            "source_path": "",
            "detail": {"origin": "runtime", "exec_class": "prep"},
        },
        {
            "uid": "runtime/prep/pick_account", "kind": "prep",
            "id": "pick_account", "title": "租账号", "enabled": True,
            "lifecycle": "active", "provider": "platform", "owner": "@platform",
            "root": "builtin",
            "scope": {"platforms": ["android", "ios"], "app_ids": [], "visible_to": ["case"]},
            "when": "", "summary": "按场景从账号管理租号",
            "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
            "source_path": "",
            "detail": {"origin": "runtime", "exec_class": "prep"},
        },
        {
            "uid": "runtime/step/agent-decide", "kind": "step",
            "id": "agent-decide", "title": "看图决策", "enabled": True,
            "lifecycle": "active", "provider": "platform", "owner": "@platform",
            "root": "builtin",
            "scope": {"platforms": ["android", "ios"], "app_ids": [], "visible_to": ["case"]},
            "when": "", "summary": "看截图决定下一个动作",
            "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
            "source_path": "",
            "detail": {"origin": "runtime", "exec_class": "step"},
        },
        {
            "uid": "runtime/expect/assert-vision", "kind": "expect",
            "id": "assert-vision", "title": "视觉校验", "enabled": True,
            "lifecycle": "active", "provider": "platform", "owner": "@platform",
            "root": "builtin",
            "scope": {"platforms": ["android", "ios", "web"], "app_ids": [], "visible_to": ["case"]},
            "when": "", "summary": "看操作后的新图判断预期是否成立",
            "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
            "source_path": "",
            "detail": {"origin": "runtime", "exec_class": "expect"},
        },
        {
            "uid": "builtin/generic/tap_element", "kind": "generic",
            "id": "tap_element", "title": "点击元素", "enabled": True,
            "lifecycle": "active", "provider": "platform", "owner": "@platform",
            "root": "builtin",
            "scope": {"platforms": ["android", "ios", "web"], "app_ids": [], "visible_to": ["case"]},
            "when": "", "summary": "点击屏幕上的目标",
            "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
            "source_path": "plugins/capabilities/tap_element.yaml",
            "detail": {"origin": "yaml", "exec_class": "generic"},
        },
        {
            "uid": "apps/b5431352/zaowu-camera/gen-timing", "kind": "knowledge",
            "id": "gen-timing", "title": "生成链路耗时基线", "enabled": True,
            "lifecycle": "active", "provider": "app_qa", "owner": "@changpengcheng",
            "root": "app",
            "scope": {"platforms": ["android"], "app_ids": ["b5431352-e34a-4d53-9e5b-33d5b130f0ff"],
                      "app_versions": ">=2.0.0 <3.0.0", "visible_to": ["case"]},
            "when": "生成加载页出现「脑洞正在加载中 N%」",
            "summary": "正常 60~180s；进度 60s 无变化即判链路异常",
            "stats": {"hit_count": 17, "refuted_count": 0, "last_hit_at": "2026-08-18T14:26:15"},
            "source_path": "packs/apps/b5431352/zaowu-camera/entries/gen-timing.yaml",
            "detail": {"category": "timing", "assert_kinds": ["process_state"],
                       "then": "停止等待，写 env_fact generation_pipeline=down，结束本条用例",
                       "evidence": ["cr-898b203890ac / CAM-GEN-013：进度 0% 停滞 50s 后放弃"]},
        },
        {
            "uid": "learned/style-thumb-defect", "kind": "knowledge",
            "id": "style-thumb-defect", "title": "未完成缩略图点击无响应", "enabled": False,
            "lifecycle": "draft", "provider": "learned", "owner": "",
            "root": "learned",
            "scope": {"platforms": ["android"], "app_ids": ["b5431352-e34a-4d53-9e5b-33d5b130f0ff"],
                      "visible_to": ["case"]},
            "when": "点击仍在加载中的风格缩略图",
            "summary": "主图与选中态均不变，命中即结束，不要重复点击",
            "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
            "source_path": "packs/learned/entries/style-thumb-defect.yaml",
            "detail": {"category": "known_defect", "defect_ticket": "BUG-XXXX", "reverify": True,
                       "confidence": 0.55,
                       "evidence": ["cr-898b203890ac / CAM-VIEW-007：连续 3 次断言失败"]},
        },
        {
            "uid": "builtin/oracle/ui_layout", "kind": "oracle", "id": "ui_layout",
            "title": "布局 / 位置 / 列数 / 顺序", "enabled": True, "lifecycle": "active",
            "provider": "platform", "owner": "@platform", "root": "builtin",
            "scope": {"platforms": ["android", "ios"], "app_ids": [], "visible_to": []},
            "when": "", "summary": "可测：VLM 单图 / baseline layout 比对",
            "stats": {"hit_count": 41, "refuted_count": 0, "last_hit_at": ""},
            "source_path": "plugins/oracle/ui_layout.yaml",
            "detail": {"status": "supported", "method": ["vlm_single", "baseline_layout"],
                       "strength_default": "layout", "gap": "", "unlock": ""},
        },
        {
            "uid": "builtin/oracle/hardware_input", "kind": "oracle", "id": "hardware_input",
            "title": "硬件输入内容可控", "enabled": True, "lifecycle": "active",
            "provider": "platform", "owner": "@platform", "root": "builtin",
            "scope": {"platforms": ["android"], "app_ids": [], "visible_to": []},
            "when": "", "summary": "不可测：只能点快门，拍到什么不可控",
            "stats": {"hit_count": 0, "refuted_count": 0, "last_hit_at": ""},
            "source_path": "plugins/oracle/hardware_input.yaml",
            "detail": {"status": "unsupported", "method": [],
                       "gap": "只能点快门，拍到什么不可控",
                       "unlock": "注入受控图源（相册预置图 / 虚拟相机）",
                       "unlock_ref": "ENG-controlled-camera"},
        },
    ]
    return {"items": rows, "total": len(rows), "health": {"error_count": 0, "by_kind": {}, "errors": []},
            "counts": {
                "prep": 1, "step": 1, "expect": 1, "generic": 1,
                "capability": 0, "recovery": 0, "knowledge": 2, "oracle": 2,
            },
            "fixture": True}


# ---------- 接口 ----------


@router.get("")
def list_packs(
    kind: str = Query("", description="prep|step|expect|generic|recovery|knowledge|oracle|capability，留空=Tab 上的全部"),
    q: str = Query("", description="关键词，匹配 id/标题/触发条件/owner"),
    provider: str = Query("", description="platform|device_team|app_qa|learned|doc|third_party"),
    lifecycle: str = Query("", description="draft|review|active|deprecated"),
    root: str = Query("", description="app|team|builtin|learned"),
    app_id: str = Query("", description="只看某应用相关（含全局条目）"),
    fixture: int = Query(0, description="1=返回样例数据，前端可离线开发"),
) -> dict[str, Any]:
    if fixture:
        return {"code": 200, "data": _fixture()}

    kinds = [kind] if kind else list(_TAB_ORDER)
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        raise HTTPException(status_code=400, detail=f"未知 kind: {bad}，可用 {list(KINDS)}")

    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    not_ready: dict[str, str] = {}
    for k in kinds:
        rows = _collect(k)
        counts[k] = len(rows)
        if not rows and k in _NOT_READY:
            not_ready[k] = _NOT_READY[k]
        items.extend(rows)

    kw = q.strip().lower()
    if kw:
        def hit(r: dict) -> bool:
            blob = " ".join([
                r["id"], r.get("title", ""), r.get("when", ""), r.get("summary", ""),
                r.get("owner", ""), r.get("provider", ""),
            ]).lower()
            return kw in blob
        items = [r for r in items if hit(r)]
    if provider:
        items = [r for r in items if r.get("provider") == provider]
    if lifecycle:
        items = [r for r in items if r.get("lifecycle") == lifecycle]
    if root:
        items = [r for r in items if r.get("root") == root]
    if app_id:
        items = [r for r in items
                 if not r["scope"].get("app_ids") or app_id in r["scope"]["app_ids"]]

    return {"code": 200, "data": {
        "items": items,
        "total": len(items),
        "counts": counts,
        "not_ready": not_ready,
        "health": _health(),
        "fixture": False,
    }}


@router.get("/kinds")
def list_kinds() -> dict[str, Any]:
    """前端 Tab：由服务端下发，不要在页面写死分类。"""
    out = []
    for k in _TAB_ORDER:
        meta = _TAB_LABELS[k]
        rows = _collect(k)
        out.append({
            "kind": k, **meta,
            "count": len(rows),
            "ready": k not in _NOT_READY,
            "not_ready_reason": _NOT_READY.get(k, ""),
        })
    return {"code": 200, "data": {"kinds": out, "health": _health()}}


@router.get("/health")
def packs_health() -> dict[str, Any]:
    return {"code": 200, "data": _health()}


@router.post("/reload")
def reload_packs() -> dict[str, Any]:
    """改完 YAML 立即重载（不必重启服务）。"""
    from server.services.plugins import registry

    from server.services.packs import get_store

    info = registry.reload()          # builtin 根（plugins/）
    store_info = get_store().reload()  # app / team / learned 根
    SLog.i(TAG, f"packs reloaded: builtin={info.get('capabilities')} store={store_info.get('by_root')}")
    return {"code": 200, "msg": "已重载",
            "data": {"reload": info, "store": store_info, "health": _health()}}


# ---------- 写入 ----------


class LifecycleBody(BaseModel):
    lifecycle: str = ""          # draft | review | active | deprecated
    enabled: Optional[bool] = None


class SaveYamlBody(BaseModel):
    raw_yaml: str


_LIFECYCLES = ("draft", "review", "active", "deprecated")


def _locate(uid: str) -> tuple[str, str, dict[str, Any]]:
    """uid → (kind, id, row)。找不到抛 404。"""
    parts = [p for p in uid.split("/") if p]
    if len(parts) < 3:
        raise HTTPException(status_code=400, detail="uid 形如 <root>/<kind>/<id>")
    kind, entry_id = parts[-2], parts[-1]
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"未知 kind: {kind}")
    row = next((r for r in _collect(kind) if r["id"] == entry_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到 {kind}/{entry_id}")
    return kind, entry_id, row


def _validate_yaml(kind: str, data: dict) -> None:
    """按 kind 用对应 pydantic 模型校验。校验不过抛 400（带原因）。"""
    from server.services.plugins.models import Capability, RecoveryRule

    model = {"recovery": RecoveryRule, "capability": Capability}.get(kind)
    if model is None:
        raise HTTPException(status_code=400, detail=f"kind={kind} 暂不支持写入")
    try:
        obj = model(**data)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"schema 校验失败：{exc}") from exc
    if kind == "recovery":
        if obj.mode not in ("deterministic", "advise"):
            raise HTTPException(status_code=400,
                                detail=f"mode 只支持 deterministic / advise，收到 {obj.mode!r}")
        if obj.mode == "deterministic" and not obj.actions:
            raise HTTPException(status_code=400, detail="mode=deterministic 必须声明 actions")
        unknown = [a.capability for a in obj.actions
                   if _capability_missing(a.capability)]
        if unknown:
            raise HTTPException(status_code=400, detail=f"动作引用了不存在的能力：{unknown}")


def _capability_missing(cap_id: str) -> bool:
    from server.services.plugins import registry

    return registry.get_capability(cap_id) is None


def _atomic_write(path: str, text: str) -> None:
    """同目录临时文件 + rename，避免写坏原文件。"""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".pack-", suffix=".yaml", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


@router.post("/{uid:path}/lifecycle")
def set_lifecycle(uid: str, body: LifecycleBody) -> dict[str, Any]:
    """启停 / 改生命周期。直接改源 YAML 的对应字段，其余内容与注释保持不动。"""
    kind, entry_id, row = _locate(uid.removesuffix("/lifecycle"))
    path = row.get("source_path") or ""
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=400, detail="该条目没有可写的源文件")
    if body.lifecycle and body.lifecycle not in _LIFECYCLES:
        raise HTTPException(status_code=400, detail=f"lifecycle 只支持 {list(_LIFECYCLES)}")

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    data = yaml.safe_load(text) or {}
    if body.lifecycle:
        data["lifecycle"] = body.lifecycle
    if body.enabled is not None:
        data["enabled"] = bool(body.enabled)
    _validate_yaml(kind, data)
    # 用 yaml.dump 会丢注释，所以只做逐行替换；缺字段时追加
    lines = text.splitlines()
    for field in ("lifecycle", "enabled"):
        if field == "lifecycle" and not body.lifecycle:
            continue
        if field == "enabled" and body.enabled is None:
            continue
        value = data[field]
        rendered = f"{field}: {str(value).lower() if isinstance(value, bool) else value}"
        for i, ln in enumerate(lines):
            if ln.startswith(f"{field}:"):
                lines[i] = rendered
                break
        else:
            lines.insert(1, rendered)
    _atomic_write(path, "\n".join(lines) + "\n")

    from server.services.plugins import registry
    registry.reload()
    SLog.i(TAG, f"lifecycle updated {kind}/{entry_id} → {data.get('lifecycle')} enabled={data.get('enabled')}")
    _, _, fresh = _locate(uid.removesuffix("/lifecycle"))
    return {"code": 200, "msg": "已保存", "data": {"item": fresh}}


@router.put("/{uid:path}")
def save_pack(uid: str, body: SaveYamlBody) -> dict[str, Any]:
    """保存整份 YAML（编辑器直接改原文）。校验不过不落盘。"""
    kind, entry_id, row = _locate(uid)
    path = row.get("source_path") or ""
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=400, detail="该条目没有可写的源文件")
    try:
        data = yaml.safe_load(body.raw_yaml)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"YAML 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="YAML 顶层必须是对象")
    if str(data.get("id") or "") != entry_id:
        raise HTTPException(status_code=400, detail="不允许在保存时改 id（请新建条目）")
    _validate_yaml(kind, data)
    _atomic_write(path, body.raw_yaml)

    from server.services.plugins import registry
    registry.reload()
    SLog.i(TAG, f"pack saved {kind}/{entry_id} ({len(body.raw_yaml)} chars)")
    _, _, fresh = _locate(uid)
    return {"code": 200, "msg": "已保存并重载", "data": {"item": fresh, "health": _health()}}


# ---------- dry-run：单条试跑 ----------


@router.post("/{uid:path}/dry-run")
def dry_run(
    uid: str,
    sn: str = Query("", description="设备序列号；source=device 时必填"),
    source: str = Query("device", description="device（当前设备当前屏）"),
    execute: int = Query(0, description="1=真的在设备上执行动作，默认只预演"),
    app_id: str = Query(""),
    package: str = Query("", description="被测应用包名，用于取证 target_alive/前台判断"),
) -> dict[str, Any]:
    """在真实屏幕上试一条规则：是否命中 → 会做什么 → （可选）真的做。

    默认 execute=0 只预演不动设备，这样改完规则一分钟内就能验证，
    不必跑一整条用例（见 docs/plan-skill-packs-and-console.md §6.2）。
    """
    kind, entry_id, row = _locate(uid.removesuffix("/dry-run"))
    if kind != "recovery":
        raise HTTPException(status_code=400, detail=f"kind={kind} 暂不支持 dry-run（当前只支持 recovery）")
    if source != "device":
        raise HTTPException(status_code=400,
                            detail="当前只支持 source=device；样本图 / 历史 trace 需等 trace 存取证快照后再开")
    if not sn:
        raise HTTPException(status_code=400, detail="source=device 需要 sn")

    from server.services.plugins import registry
    from server.services.regression import hierarchy as H
    from server.services.regression import recovery as R
    from server.services.regression.router import CapabilityRouter
    from server.services.runtime.run_context import build_run_context

    rule = registry.get_recovery_rule(entry_id)
    if rule is None:
        raise HTTPException(status_code=404, detail=f"规则 {entry_id} 未加载")

    ctx = build_run_context(sn, platform="android", run_id=f"dryrun-{entry_id}",
                            app_id=app_id, target_package=package,
                            probe_remote_channel=False, probe_vlm_channel=False,
                            probe_hitl_channel=False)
    if ctx.adb.get("state") != "connected":
        raise HTTPException(status_code=409,
                            detail=f"设备 {sn} adb 未连通（{ctx.adb.get('state')}）")
    router_ = CapabilityRouter(ctx, capture_prefer=("adb",))

    evidence = R.collect_evidence(ctx, router_, target_package=package)
    dump = H.dump_ui_nodes(sn)
    texts = R.screen_texts_from_hierarchy(dump)
    hits = R.match_rules(evidence, texts, rules=[rule])
    matched = bool(hits)

    plan: list[dict[str, Any]] = []
    for a in (rule.actions or []):
        entry = {"capability": a.capability, "params": dict(a.params or {}),
                 "target": dict(a.target or {})}
        blocked = R._forbidden(rule, a)
        if blocked:
            entry["blocked_by_forbid"] = blocked
        plan.append(entry)

    result: dict[str, Any] = {
        "uid": row["uid"],
        "matched": matched,
        "match_reasons": hits[0].reasons if hits else [],
        "evidence": evidence.as_match_dict(),
        "evidence_brief": evidence.brief(),
        "screen_text_sample": texts[:20],
        "hierarchy": {"ok": dump.ok, "nodes": len(dump), "source": dump.source,
                      "elapsed_ms": dump.elapsed_ms},
        "mode": rule.mode,
        "planned_actions": plan,
        "advice": rule.prompt_snippet if rule.mode == "advise" else "",
        "executed": False,
    }

    if execute and matched and rule.mode == "deterministic":
        outcome = R.apply_rule(hits[0], ctx, router_, target_package=package)
        result["executed"] = True
        result["execution"] = {
            "recovered": outcome.recovered,
            "attempts": outcome.attempts,
            "actions": outcome.actions,
            "error": outcome.error,
            "summary": outcome.summary(),
        }
    return {"code": 200, "data": result}


# ---------- 根与新建 ----------


class CreateEntryBody(BaseModel):
    kind: str = "recovery"
    root: str = "team"               # app | team | learned（builtin 随版本发布，不允许写）
    app_id: str = ""                 # root=app 时必填
    pack_id: str = "adhoc"           # 包名；不存在会自动建 pack.yaml
    id: str                          # 条目 id（同 kind 内唯一）
    owner: str = ""
    raw_yaml: str = ""               # 留空则按 kind 生成一份最小骨架
    overwrite: bool = False


_SKELETON = {
    "recovery": """id: {id}
kind: recovery
title: {id}
enabled: true
owner: "{owner}"
lifecycle: draft            # 先 draft，验证过再改 active

when: 描述什么情况下该处置

match:                      # 条件之间是「与」，每类内部是「或」
  evidence:                 # 可用字段：awake/locked/screen_blocked/app_foreground/target_alive/anr/ime_shown
    screen_blocked: "yes"
  # screen_text_any: ["屏上文案"]
  # top_window_pkg_prefix: ["com.android."]

mode: advise                # deterministic=命中即执行动作；advise=只给模型提示
prompt_snippet: |
  这里写给模型的提示。

# mode=deterministic 时改成下面这样：
# actions:
#   - capability: wake_screen
#   - capability: wait_ms
#     params: {{ms: 800}}
# verify:
#   evidence:
#     screen_blocked: "no"

forbid:
  text_any: ["拒绝", "不允许", "清除数据"]

max_attempts: 1

evidence_notes:
  - "凭什么这么判：写清依据（哪次任务/哪条用例观察到）"
""",
}


@router.get("/roots")
def list_roots(app_id: str = Query("")) -> dict[str, Any]:
    """四个根的说明与当前条目数，供前端「新建」时选落哪个根。"""
    from server.services.packs import ROOT_RANK, get_store
    from server.services.packs.store import root_dir

    store = get_store()
    counts = store.counts_by_root()
    meta = {
        "app": ("应用私有", "业务测试同学写，只对该应用生效；优先级最高", True),
        "team": ("团队共享", "设备/环境组维护，跨应用生效，可从 git 同步", True),
        "builtin": ("仓库内置", "平台团队随版本发布，UI 只读", False),
        "learned": ("自动学习", "系统写入，默认 draft 需人工确认；优先级最低", True),
    }
    out = []
    for root, rank in sorted(ROOT_RANK.items(), key=lambda kv: kv[1]):
        label, desc, writable = meta[root]
        try:
            path = str(root_dir(root, app_id)) if root != "app" or app_id else ""
        except ValueError:
            path = ""
        out.append({"root": root, "rank": rank, "label": label, "desc": desc,
                    "writable": writable, "count": counts.get(root, 0), "path": path})
    return {"code": 200, "data": {"roots": out, "precedence": "app > team > builtin > learned"}}


@router.post("/create")
def create_entry(body: CreateEntryBody) -> dict[str, Any]:
    """在指定根新建一条条目。校验不过不落盘；builtin 根禁止写。"""
    from server.services.packs import get_store
    from server.services.packs.store import SUPPORTED_KINDS

    if body.kind not in SUPPORTED_KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind={body.kind} 暂不支持写入（当前支持 {list(SUPPORTED_KINDS)}）")
    if body.root == "builtin":
        raise HTTPException(status_code=400,
                            detail="仓库内置根随版本发布，请选 应用私有 / 团队共享 / 自动学习")
    if body.root not in ("app", "team", "learned"):
        raise HTTPException(status_code=400, detail=f"未知 root: {body.root}")
    if body.root == "app" and not body.app_id:
        raise HTTPException(status_code=400, detail="root=app 必须带 app_id")
    if not body.id.strip():
        raise HTTPException(status_code=400, detail="条目 id 不能为空")

    raw = body.raw_yaml.strip()
    if not raw:
        raw = _SKELETON[body.kind].format(id=body.id, owner=body.owner or "")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"YAML 解析失败：{exc}") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="YAML 顶层必须是对象")
    data.setdefault("id", body.id)
    if str(data.get("id")) != body.id:
        raise HTTPException(status_code=400, detail="raw_yaml 里的 id 与请求的 id 不一致")
    _validate_yaml(body.kind, data)

    store = get_store()
    try:
        entry = store.write_entry(
            body.root, body.pack_id, body.kind, body.id, raw,
            app_id=body.app_id, owner=body.owner, overwrite=body.overwrite,
        )
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    SLog.i(TAG, f"entry created {entry.uid} at {entry.source_path}")
    row = _recovery_row(entry.obj, entry)
    return {"code": 200, "msg": "已创建", "data": {"item": row, "health": _health()}}


# ---------- 通配详情路由必须放在最后 ----------
# FastAPI 按注册顺序匹配：/{uid:path} 会吞掉 /packs/roots、/packs/kinds 这类具体路径，
# 所以它必须排在所有具体路由之后（实测踩过：/packs/roots 曾返回「uid 形如…」的 400）。

@router.get("/{uid:path}")
def get_pack(uid: str, with_yaml: int = Query(1, description="1=附原始 YAML 文本")) -> dict[str, Any]:
    """按 uid 取详情。uid 形如 builtin/recovery/screen_asleep_or_locked。"""
    parts = [p for p in uid.split("/") if p]
    if len(parts) < 3:
        raise HTTPException(status_code=400, detail="uid 形如 <root>/<kind>/<id>")
    kind, entry_id = parts[-2], parts[-1]
    if kind not in KINDS:
        raise HTTPException(status_code=400, detail=f"未知 kind: {kind}")

    row: Optional[dict[str, Any]] = next((r for r in _collect(kind) if r["id"] == entry_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail=f"未找到 {kind}/{entry_id}")

    if with_yaml and row.get("source_path"):
        try:
            with open(row["source_path"], "r", encoding="utf-8") as f:
                row["raw_yaml"] = f.read()[:20000]
        except OSError as exc:
            row["raw_yaml"] = ""
            row["raw_yaml_error"] = str(exc)
    return {"code": 200, "data": {"item": row}}
