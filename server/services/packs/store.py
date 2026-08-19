# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Pack 多根存储：四个根 + 优先级裁决（S1b）。

四个根（高 → 低，同 kind+id 高者胜，低者标 overridden_by）
--------------------------------------------------------
1. app     <APP_DATA>/packs/apps/<app_id>/<pack>/    业务测试同学，UI 可直接编辑
2. team    <APP_DATA>/packs/team/<pack>/             设备/环境组，可从 git 同步
3. builtin <repo>/plugins/                           平台团队，随版本发布
4. learned <APP_DATA>/packs/learned/<pack>/          系统写入，默认 draft 需人工确认

为什么 builtin 不是最高：现场遇到平台默认规则不适用时，团队/应用层要能**就地覆盖**，
不必等发版。为什么 learned 最低：人写的永远压过机器学的，不会被自动学习悄悄改掉行为。

与现有 PluginLoader 的关系
--------------------------
**不改** `server/services/plugins/loader.py`（它服务老的 /settings/skills，改动风险不对等）。
builtin 根的条目仍由它加载，本模块只负责：读非 builtin 根 + 合并 + 裁决 + 写入。

目录形态
--------
builtin 沿用既有布局：`plugins/recovery/*.yaml`（无 pack.yaml）。
其余根用包布局：`<pack>/pack.yaml` + `<pack>/entries/*.yaml`（+ 可选 `samples/`）。

uid 规则
--------
`<root>[/<app_id>]/<kind>/<id>` —— kind 与 id 恒为最后两段，所以解析方式对四个根一致，
也与 S2a 已经上线的 `builtin/recovery/xxx` 向后兼容。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import ValidationError

from script.log import SLog

from server.services.plugins.models import LoadError, PackManifest, RecoveryRule

TAG = "PackStore"

# 根名 → 优先级（数字小 = 优先级高）
ROOT_RANK = {"app": 0, "team": 1, "builtin": 2, "learned": 3}

# 目前支持从非 builtin 根加载的 kind（knowledge / oracle 落地后加进来即可）
SUPPORTED_KINDS = ("recovery",)

_MODEL_BY_KIND = {"recovery": RecoveryRule}

# 这些 provider 的产出必须人工确认后才允许生效
_REVIEW_REQUIRED_PROVIDERS = ("learned", "doc", "third_party")

_SKIP_SUFFIXES = (".disabled", ".draft", ".bak")


@dataclass
class PackEntry:
    """一条已解析的条目 + 它的归属信息。"""

    kind: str
    id: str
    root: str
    obj: Any                            # RecoveryRule / 未来的其它 kind
    manifest: Optional[PackManifest] = None
    app_id: str = ""
    source_path: str = ""
    overridden_by: str = ""

    @property
    def uid(self) -> str:
        if self.root == "app" and self.app_id:
            return f"app/{self.app_id}/{self.kind}/{self.id}"
        return f"{self.root}/{self.kind}/{self.id}"

    @property
    def pack_id(self) -> str:
        return self.manifest.id if self.manifest else ""

    @property
    def scope_app_ids(self) -> list[str]:
        if self.manifest and self.manifest.scope.app_ids:
            return list(self.manifest.scope.app_ids)
        return [self.app_id] if self.app_id else []

    def effective(self, attr: str, default: Any = "") -> Any:
        """条目字段优先，未写则回落到包清单（provider/owner/lifecycle 这类归属信息）。"""
        val = getattr(self.obj, attr, None)
        if val:
            return val
        if self.manifest is not None:
            mval = getattr(self.manifest, attr, None)
            if mval:
                return mval
        return default


def app_data_packs_root() -> Path:
    from server.core.database import APP_DATA_DIR

    return Path(APP_DATA_DIR) / "packs"


def root_dir(root: str, app_id: str = "") -> Path:
    """某个根的目录。builtin 指向仓库 plugins/，其余在 APP_DATA 下。"""
    if root == "builtin":
        from server.services.plugins.loader import find_plugin_root

        return find_plugin_root()
    base = app_data_packs_root()
    if root == "app":
        if not app_id:
            raise ValueError("app 根必须带 app_id")
        return base / "apps" / app_id
    if root in ("team", "learned"):
        return base / root
    raise ValueError(f"未知 root: {root}")


def _is_active_yaml(path: Path) -> bool:
    name = path.name.lower()
    if not (name.endswith(".yaml") or name.endswith(".yml")):
        return False
    return not any(name.endswith(s) for s in _SKIP_SUFFIXES)


class PackStore:
    """扫描四个根、合并裁决、提供读写。mtime 变化即重扫。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: list[PackEntry] = []
        self._errors: list[LoadError] = []
        self._mtimes: dict[Path, float] = {}
        self._loaded_at: float = 0.0

    # ---------- 加载 ----------

    def _iter_pack_dirs(self) -> list[tuple[str, str, Path]]:
        """返回 [(root, app_id, pack_dir)]，只含包布局的根。"""
        out: list[tuple[str, str, Path]] = []
        base = app_data_packs_root()
        for root in ("team", "learned"):
            d = base / root
            if d.is_dir():
                out.extend((root, "", p) for p in sorted(d.iterdir()) if p.is_dir())
        apps_dir = base / "apps"
        if apps_dir.is_dir():
            for app_dir in sorted(apps_dir.iterdir()):
                if not app_dir.is_dir():
                    continue
                for p in sorted(app_dir.iterdir()):
                    if p.is_dir():
                        out.append(("app", app_dir.name, p))
        return out

    def _watch(self, path: Path) -> None:
        try:
            self._mtimes[path] = path.stat().st_mtime
        except OSError:
            pass

    def _changed(self) -> bool:
        for path, prev in list(self._mtimes.items()):
            try:
                if path.stat().st_mtime != prev:
                    return True
            except OSError:
                return True
        # 新增的包目录 / 条目文件也算变更
        seen = set(self._mtimes)
        for _root, _app, pack_dir in self._iter_pack_dirs():
            if pack_dir / "pack.yaml" not in seen:
                return True
            entries = pack_dir / "entries"
            if entries.is_dir():
                for f in entries.iterdir():
                    if _is_active_yaml(f) and f not in seen:
                        return True
        return False

    def ensure_loaded(self) -> None:
        with self._lock:
            if not self._loaded_at or self._changed():
                self._load()

    def reload(self) -> dict[str, Any]:
        with self._lock:
            self._load()
            return {
                "entries": len(self._entries),
                "by_root": self.counts_by_root(),
                "errors": [e.model_dump() for e in self._errors],
            }

    def _load(self) -> None:
        self._entries = []
        self._errors = []
        self._mtimes = {}

        # 1) builtin：复用现有 PluginLoader 的成果，不重复解析
        from server.services.plugins import registry as plugin_registry

        for rule in plugin_registry.list_recovery_rules(enabled_only=False):
            self._entries.append(PackEntry(
                kind="recovery", id=rule.id, root="builtin", obj=rule,
                source_path=rule.source_path,
            ))

        # 2) 其余三个根：包布局
        for root, app_id, pack_dir in self._iter_pack_dirs():
            manifest = self._load_manifest(root, app_id, pack_dir)
            if manifest is None:
                continue
            entries_dir = pack_dir / "entries"
            if not entries_dir.is_dir():
                self._errors.append(LoadError(
                    path=str(pack_dir), kind="pack",
                    message=f"包 {manifest.id} 缺少 entries/ 目录"))
                continue
            for f in sorted(entries_dir.iterdir()):
                if _is_active_yaml(f):
                    self._load_entry(root, app_id, manifest, f)

        self._resolve_precedence()
        self._loaded_at = time.time()
        # 注意：这里不能调用 self.counts_by_root()，因为它会 ensure_loaded()
        # 从而在 _load() 进行中触发递归，导致计数/加载逻辑爆栈。
        by_root: dict[str, int] = {}
        for e in self._entries:
            by_root[e.root] = by_root.get(e.root, 0) + 1
        SLog.i(TAG, f"pack store loaded: entries={len(self._entries)} "
                    f"by_root={by_root} errors={len(self._errors)}")

    def _load_manifest(self, root: str, app_id: str, pack_dir: Path) -> Optional[PackManifest]:
        path = pack_dir / "pack.yaml"
        self._watch(path)
        if not path.is_file():
            self._errors.append(LoadError(
                path=str(pack_dir), kind="pack", message="缺少 pack.yaml，整个包被跳过"))
            return None
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError) as exc:
            self._errors.append(LoadError(path=str(path), kind="pack", message=str(exc)))
            return None
        if not isinstance(data, dict):
            self._errors.append(LoadError(path=str(path), kind="pack",
                                         message="pack.yaml 顶层必须是对象"))
            return None
        data.setdefault("id", pack_dir.name)
        try:
            manifest = PackManifest(**data)
        except ValidationError as exc:
            self._errors.append(LoadError(path=str(path), kind="pack", message=str(exc)))
            return None
        manifest.root = root
        manifest.dir_path = str(pack_dir)
        manifest.app_id = app_id
        if not manifest.owner:
            self._errors.append(LoadError(
                path=str(path), kind="pack",
                message=f"包 {manifest.id} 缺 owner（谁负责），仍加载但请补上"))
        if manifest.provider in _REVIEW_REQUIRED_PROVIDERS and not manifest.review.required:
            # 学习/文档/第三方产出必须走人工确认，这条由代码强制，不靠人自觉
            manifest.review.required = True
            SLog.i(TAG, f"pack {manifest.id} provider={manifest.provider} → 强制 review.required")
        return manifest

    def _load_entry(self, root: str, app_id: str, manifest: PackManifest, path: Path) -> None:
        self._watch(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (yaml.YAMLError, OSError) as exc:
            self._errors.append(LoadError(path=str(path), kind="entry", message=str(exc)))
            return
        if not isinstance(data, dict):
            self._errors.append(LoadError(path=str(path), kind="entry",
                                         message="条目顶层必须是对象"))
            return
        kind = str(data.get("kind") or manifest.kind_hint or "").strip()
        if kind not in SUPPORTED_KINDS:
            self._errors.append(LoadError(
                path=str(path), kind="entry",
                message=f"kind={kind!r} 暂不支持从包加载（当前支持 {list(SUPPORTED_KINDS)}）"))
            return
        data.setdefault("id", path.stem)
        model = _MODEL_BY_KIND[kind]
        try:
            obj = model(**data)
        except ValidationError as exc:
            self._errors.append(LoadError(path=str(path), kind="entry", message=str(exc)))
            return
        obj.source_path = str(path)
        # 归属信息未写则继承包清单
        if not getattr(obj, "provider", "") or obj.provider == "platform":
            obj.provider = manifest.provider
        if not getattr(obj, "owner", ""):
            obj.owner = manifest.owner
        # 需要评审但未批准 → 强制降为 draft（不生效），而不是让它悄悄生效
        if manifest.review.required and not manifest.review.approved_at:
            obj.lifecycle = "draft"
            obj.enabled = False

        # 同一个根内 kind+id 撞车 = 明确错误（跨根才是覆盖）
        dup = next((e for e in self._entries
                    if e.kind == kind and e.id == obj.id and e.root == root
                    and e.app_id == app_id), None)
        if dup is not None:
            self._errors.append(LoadError(
                path=str(path), kind="entry",
                message=f"同一根内 {kind}/{obj.id} 重复（另一处：{dup.source_path}）"))
            return

        self._entries.append(PackEntry(
            kind=kind, id=obj.id, root=root, obj=obj, manifest=manifest,
            app_id=app_id, source_path=str(path),
        ))

    def _resolve_precedence(self) -> None:
        """同 kind+id 跨根冲突：高优先级根胜，低者标 overridden_by 且不参与执行。"""
        groups: dict[tuple[str, str], list[PackEntry]] = {}
        for e in self._entries:
            e.overridden_by = ""
            groups.setdefault((e.kind, e.id), []).append(e)
        for (kind, eid), group in groups.items():
            if len(group) < 2:
                continue
            group.sort(key=lambda e: ROOT_RANK.get(e.root, 99))
            winner = group[0]
            for loser in group[1:]:
                loser.overridden_by = winner.uid
            SLog.i(TAG, f"precedence {kind}/{eid}: {winner.root} 胜出，"
                        f"覆盖 {[l.root for l in group[1:]]}")

    # ---------- 查询 ----------

    def counts_by_root(self) -> dict[str, int]:
        self.ensure_loaded()
        out: dict[str, int] = {}
        for e in self._entries:
            out[e.root] = out.get(e.root, 0) + 1
        return out

    def list_entries(self, *, kind: str = "", app_id: str = "",
                     include_overridden: bool = True) -> list[PackEntry]:
        self.ensure_loaded()
        out = list(self._entries)
        if kind:
            out = [e for e in out if e.kind == kind]
        if app_id:
            out = [e for e in out if not e.scope_app_ids or app_id in e.scope_app_ids]
        if not include_overridden:
            out = [e for e in out if not e.overridden_by]
        return out

    def get_entry(self, uid: str) -> Optional[PackEntry]:
        self.ensure_loaded()
        return next((e for e in self._entries if e.uid == uid), None)

    def active_objects(self, kind: str, *, app_id: str = "") -> list[Any]:
        """执行期用：只返回生效中的条目对象（过滤 overridden / 非 active）。"""
        out = []
        for e in self.list_entries(kind=kind, app_id=app_id, include_overridden=False):
            obj = e.obj
            if not getattr(obj, "enabled", True):
                continue
            if getattr(obj, "lifecycle", "active") != "active":
                continue
            out.append(obj)
        out.sort(key=lambda o: -int(getattr(o, "priority", 0)))
        return out

    def errors(self) -> list[LoadError]:
        self.ensure_loaded()
        return list(self._errors)

    # ---------- 写入 ----------

    def ensure_pack(self, root: str, pack_id: str, *, app_id: str = "",
                    owner: str = "", provider: str = "", display_name: str = "") -> Path:
        """确保包目录与 pack.yaml 存在，返回包目录。"""
        if root == "builtin":
            raise ValueError("builtin 根随版本发布，不支持从 UI 写入；请选 app / team / learned")
        pack_dir = root_dir(root, app_id) / pack_id
        (pack_dir / "entries").mkdir(parents=True, exist_ok=True)
        manifest_path = pack_dir / "pack.yaml"
        if not manifest_path.is_file():
            default_provider = provider or ("learned" if root == "learned" else
                                            "device_team" if root == "team" else "app_qa")
            manifest = {
                "id": pack_id,
                "display_name": display_name or pack_id,
                "version": 1,
                "provider": default_provider,
                "owner": owner or "",
                "lifecycle": "active",
                "scope": {"app_ids": [app_id] if app_id else [], "platforms": ["android"]},
                "review": {"required": default_provider in _REVIEW_REQUIRED_PROVIDERS},
            }
            manifest_path.write_text(
                yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            SLog.i(TAG, f"created pack {root}/{pack_id} at {pack_dir}")
        return pack_dir

    def write_entry(self, root: str, pack_id: str, kind: str, entry_id: str,
                    raw_yaml: str, *, app_id: str = "", owner: str = "",
                    overwrite: bool = False) -> PackEntry:
        """写一条 entry。校验交调用方先做（rPacks 会先跑 schema 校验）。"""
        if kind not in SUPPORTED_KINDS:
            raise ValueError(f"kind={kind} 暂不支持写入（当前支持 {list(SUPPORTED_KINDS)}）")
        pack_dir = self.ensure_pack(root, pack_id, app_id=app_id, owner=owner)
        path = pack_dir / "entries" / f"{entry_id}.yaml"
        if path.exists() and not overwrite:
            raise FileExistsError(f"{root}/{pack_id}/{entry_id} 已存在")
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(raw_yaml, encoding="utf-8")
        os.replace(tmp, path)
        self.reload()
        entry = next((e for e in self._entries
                      if e.kind == kind and e.id == entry_id and e.root == root), None)
        if entry is None:
            raise RuntimeError(f"写入后未能加载 {root}/{kind}/{entry_id}，请看 errors")
        return entry


_STORE: Optional[PackStore] = None


def get_store() -> PackStore:
    global _STORE
    if _STORE is None:
        _STORE = PackStore()
    return _STORE
