# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""Plugin YAML 加载器。支持热更新（基于 mtime 探测）。"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import ValidationError

from script.log import SLog

from server.services.plugins.models import (
    AbstractCap,
    Capability,
    Executor,
    LoadError,
)

TAG = "PluginLoader"

# 加载器跳过 .disabled / .draft 后缀的文件
_SKIP_SUFFIXES = (".disabled", ".draft", ".bak")


def _is_active_yaml(path: Path) -> bool:
    name = path.name.lower()
    if not (name.endswith(".yaml") or name.endswith(".yml")):
        return False
    return not any(name.endswith(s) for s in _SKIP_SUFFIXES)


def find_plugin_root() -> Path:
    """查找 plugins/ 目录。

    顺序：
      1. 环境变量 MINIORANGE_PLUGINS_ROOT
      2. 从本文件向上找包含 plugins/abstract_caps.yaml 的目录
      3. 抛错
    """
    env = os.environ.get("MINIORANGE_PLUGINS_ROOT")
    if env:
        p = Path(env).resolve()
        if (p / "abstract_caps.yaml").is_file():
            return p
        SLog.w(TAG, f"env MINIORANGE_PLUGINS_ROOT={env} invalid; falling back to auto-discover")

    here = Path(__file__).resolve()
    # this file: <repo>/server/services/plugins/loader.py
    # plugins dir: <repo>/plugins
    for ancestor in here.parents:
        candidate = ancestor / "plugins"
        if (candidate / "abstract_caps.yaml").is_file():
            return candidate
    raise RuntimeError(
        f"plugins/abstract_caps.yaml not found in any ancestor of {here}; "
        f"set MINIORANGE_PLUGINS_ROOT to override"
    )


class PluginLoader:
    """加载并缓存 plugins/ 下的全部 yaml。"""

    def __init__(self, root: Path):
        self.root = root
        self._lock = threading.RLock()
        self._loaded: bool = False
        self._mtimes: dict[Path, float] = {}
        self._abstract_caps: dict[str, AbstractCap] = {}
        self._executors: dict[str, Executor] = {}
        self._capabilities: dict[str, Capability] = {}
        self._errors: list[LoadError] = []

    # ---------- 公开访问 ----------

    @property
    def abstract_caps(self) -> dict[str, AbstractCap]:
        self._ensure_loaded()
        return self._abstract_caps

    @property
    def executors(self) -> dict[str, Executor]:
        self._ensure_loaded()
        return self._executors

    @property
    def capabilities(self) -> dict[str, Capability]:
        self._ensure_loaded()
        return self._capabilities

    @property
    def errors(self) -> list[LoadError]:
        self._ensure_loaded()
        return list(self._errors)

    # ---------- 加载流程 ----------

    def _ensure_loaded(self) -> None:
        with self._lock:
            if not self._loaded:
                self._load_all()
                self._loaded = True
                return
            if self._mtimes_changed():
                SLog.i(TAG, "plugin yaml mtime changed; reloading")
                self._load_all()

    def _mtimes_changed(self) -> bool:
        for path, prev in self._mtimes.items():
            try:
                cur = path.stat().st_mtime
            except OSError:
                return True
            if cur != prev:
                return True
        # 新增的文件也算变更
        for path in self._iter_all_yaml_paths():
            if path not in self._mtimes:
                return True
        return False

    def _iter_all_yaml_paths(self) -> list[Path]:
        paths: list[Path] = []
        ac = self.root / "abstract_caps.yaml"
        if ac.is_file():
            paths.append(ac)
        for sub in ("executors", "capabilities"):
            d = self.root / sub
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                if _is_active_yaml(p):
                    paths.append(p)
        return paths

    def _load_all(self) -> None:
        self._abstract_caps = {}
        self._executors = {}
        self._capabilities = {}
        self._errors = []
        self._mtimes = {}

        # abstract_caps
        ac_path = self.root / "abstract_caps.yaml"
        if ac_path.is_file():
            self._load_abstract_caps(ac_path)
        else:
            self._errors.append(
                LoadError(
                    path=str(ac_path),
                    kind="abstract_caps",
                    message="abstract_caps.yaml not found",
                )
            )

        # executors
        exec_dir = self.root / "executors"
        if exec_dir.is_dir():
            for path in sorted(exec_dir.iterdir()):
                if _is_active_yaml(path):
                    self._load_executor(path)

        # capabilities
        cap_dir = self.root / "capabilities"
        if cap_dir.is_dir():
            for path in sorted(cap_dir.iterdir()):
                if _is_active_yaml(path):
                    self._load_capability(path)

        SLog.i(
            TAG,
            f"loaded plugins: abstract_caps={len(self._abstract_caps)}, "
            f"executors={len(self._executors)}, capabilities={len(self._capabilities)}, "
            f"errors={len(self._errors)}",
        )

        # cross-check：每个 capability 的 requires_caps 是否都在 abstract_caps 中
        self._cross_check()

    def _read_yaml(self, path: Path) -> Optional[Any]:
        try:
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            self._mtimes[path] = path.stat().st_mtime
            return data
        except (yaml.YAMLError, OSError) as e:
            self._errors.append(
                LoadError(path=str(path), kind="yaml_parse", message=str(e))
            )
            SLog.w(TAG, f"yaml parse failed: {path} -> {e}")
            return None

    def _load_abstract_caps(self, path: Path) -> None:
        data = self._read_yaml(path)
        if not data or not isinstance(data, dict):
            return
        for entry in data.get("caps") or []:
            try:
                cap = AbstractCap(**entry)
                self._abstract_caps[cap.id] = cap
            except ValidationError as e:
                self._errors.append(
                    LoadError(path=str(path), kind="abstract_caps", message=str(e))
                )

    def _load_executor(self, path: Path) -> None:
        data = self._read_yaml(path)
        if not data or not isinstance(data, dict):
            return
        try:
            executor = Executor(**data)
            executor.source_path = str(path)
            if executor.id in self._executors:
                self._errors.append(
                    LoadError(
                        path=str(path),
                        kind="executor",
                        message=f"duplicate executor id: {executor.id}",
                    )
                )
                return
            self._executors[executor.id] = executor
        except ValidationError as e:
            self._errors.append(
                LoadError(path=str(path), kind="executor", message=str(e))
            )
            SLog.w(TAG, f"executor validation failed: {path} -> {e}")

    def _load_capability(self, path: Path) -> None:
        data = self._read_yaml(path)
        if not data or not isinstance(data, dict):
            return
        try:
            cap = Capability(**data)
            cap.source_path = str(path)
            if cap.id in self._capabilities:
                self._errors.append(
                    LoadError(
                        path=str(path),
                        kind="capability",
                        message=f"duplicate capability id: {cap.id}",
                    )
                )
                return
            self._capabilities[cap.id] = cap
        except ValidationError as e:
            self._errors.append(
                LoadError(path=str(path), kind="capability", message=str(e))
            )
            SLog.w(TAG, f"capability validation failed: {path} -> {e}")

    def _cross_check(self) -> None:
        """校验：
          - capability.implementations.*.executor 必须存在
          - capability.implementations.*.requires_caps 必须都在 abstract_caps
          - executor.provides 必须都在 abstract_caps
        """
        ac_ids = set(self._abstract_caps.keys())
        exec_ids = set(self._executors.keys())

        for executor in self._executors.values():
            missing = [c for c in executor.provides if c not in ac_ids]
            if missing:
                self._errors.append(
                    LoadError(
                        path=executor.source_path,
                        kind="executor",
                        message=f"executor {executor.id} provides unknown caps: {missing}",
                    )
                )

        for cap in self._capabilities.values():
            for impl in cap.implementations:
                # internal / hitl 占位 executor 可以不显式注册（兼容）
                if impl.executor not in exec_ids and impl.executor not in ("internal",):
                    self._errors.append(
                        LoadError(
                            path=cap.source_path,
                            kind="capability",
                            message=(
                                f"capability {cap.id} impl {impl.id} "
                                f"references unknown executor: {impl.executor}"
                            ),
                        )
                    )
                missing = [c for c in impl.requires_caps if c not in ac_ids]
                if missing:
                    self._errors.append(
                        LoadError(
                            path=cap.source_path,
                            kind="capability",
                            message=(
                                f"capability {cap.id} impl {impl.id} "
                                f"requires unknown caps: {missing}"
                            ),
                        )
                    )


# ---------- 单例 ----------

_LOADER: Optional[PluginLoader] = None
_LOADER_LOCK = threading.Lock()


def get_loader() -> PluginLoader:
    """获取全局加载器（懒加载 + 热更新）。"""
    global _LOADER
    with _LOADER_LOCK:
        if _LOADER is None:
            root = find_plugin_root()
            SLog.i(TAG, f"plugin root: {root}")
            _LOADER = PluginLoader(root)
    return _LOADER


def force_reload() -> PluginLoader:
    """强制重新加载（调试 / 运维用）。"""
    global _LOADER
    with _LOADER_LOCK:
        root = find_plugin_root()
        _LOADER = PluginLoader(root)
    return _LOADER
