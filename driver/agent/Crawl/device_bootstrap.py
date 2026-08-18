# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""跑图专用：初始化移动端引擎（不依赖工作流 Checklist）。"""
from __future__ import annotations

import builtins
import time
from typing import Any, Dict, Optional, Tuple

from script.log import SLog
from driver.agent.Common.task_details import TaskDetails
from driver.agent.Memory import memory_manager
from driver.tentacle.manager import Manager

TAG = "CrawlDeviceBootstrap"

_ENGINE_CACHE: Dict[str, Dict[str, Any]] = {}
_ENGINE_CACHE_TTL_SEC = 180.0


class DeviceOfflineError(RuntimeError):
    """ADB 设备不可见（断连、未授权等）。"""


def _is_clawnode(sn: Optional[str]) -> bool:
    """ClawNode 直连设备：SN 以 claw- 开头，不经 adb。统一走 device_identity 权威判定。"""
    try:
        from server.services.runtime.device_identity import is_clawnode_sn

        return is_clawnode_sn(sn)
    except Exception:
        return bool(sn) and str(sn).startswith("claw-")


def list_connected_adb_serials() -> list:
    """本机 adb devices 中 state=device 的序列号。

    优先用 adbutils；未安装时回退 `adb devices` subprocess，保证无 adbutils 环境
    下 adb 直连设备仍可被发现/驱动。
    """
    try:
        import adbutils

        client = adbutils.AdbClient()
        serials = []
        for info in client.list():
            if getattr(info, "state", None) == "device" and info.serial:
                serials.append(str(info.serial))
        return serials
    except Exception as e:
        SLog.d(TAG, f"adbutils unavailable ({e}), fallback to `adb devices`")

    try:
        import subprocess

        proc = subprocess.run(
            ["adb", "devices"], capture_output=True, text=True, timeout=6.0
        )
        serials = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line or line.lower().startswith("list of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials
    except Exception as e:
        SLog.w(TAG, f"`adb devices` fallback failed: {e}")
        return []


def resolve_mobile_serial(node_sn: str, platform: str = "android") -> str:
    """
    解析用于 ADB/WDA 的手机序列号。
    - 优先 adb devices 里在线的真机；
    - PC 节点（Driver 客户端 SN）不能当作 adb -s 使用。
    """
    # [ClawNode] 直连设备不走 adb，原样返回
    if _is_clawnode(node_sn):
        return str(node_sn)
    adb_serials = list_connected_adb_serials()
    if node_sn and node_sn in adb_serials:
        return str(node_sn)

    try:
        from server.services.device_service import DeviceService

        dev = DeviceService.get_by_sn(node_sn) if node_sn else None
        dt = (dev.device_type or "").lower() if dev else ""

        if dt == "pc" or (node_sn and node_sn not in adb_serials):
            if adb_serials:
                chosen = adb_serials[0]
                SLog.i(TAG, f"Driver node {node_sn} -> adb device {chosen}")
                return chosen

        if dt in ("android", "ios") and node_sn in adb_serials:
            return str(node_sn)

        pick_type = platform if platform in ("ios", "android") else "android"
        picked = DeviceService.pick_sn(device_type=pick_type)
        if picked and picked in adb_serials:
            return str(picked)
    except Exception as e:
        SLog.w(TAG, f"resolve_mobile_serial db fallback: {e}")

    if adb_serials:
        return adb_serials[0]

    return str(node_sn or "")


def is_adb_device_online(node_sn: str, platform: str = "android") -> bool:
    """解析后的 adb 序列号是否仍在线。"""
    mobile_sn = resolve_mobile_serial(node_sn, platform)
    return bool(mobile_sn) and mobile_sn in list_connected_adb_serials()


def wait_for_adb_device(
    node_sn: str,
    platform: str = "android",
    *,
    timeout: float = 12.0,
    interval: float = 0.8,
) -> str:
    """等待 hub/adb 设备上线（WS 心跳先于 adb 出现时有用）。"""
    # [ClawNode] 直连设备无需等待 adb
    if _is_clawnode(node_sn):
        return str(node_sn)
    deadline = time.time() + max(0.5, timeout)
    last_sn = ""
    while time.time() < deadline:
        last_sn = resolve_mobile_serial(node_sn, platform)
        if last_sn in list_connected_adb_serials():
            return last_sn
        time.sleep(interval)
    raise DeviceOfflineError(
        f"设备未就绪（node={node_sn}, adb={last_sn}，等待 {timeout:.0f}s）。"
        "请确认 USB 连接、adb devices 与 Driver 心跳。"
    )


def ensure_adb_device_online(
    node_sn: str,
    platform: str = "android",
    *,
    wait: bool = False,
    wait_timeout: float = 12.0,
) -> str:
    """设备离线时抛错；wait=True 时先短暂等待 adb 出现。"""
    # [ClawNode] 直连设备始终视为在线（连接状态由 WS active_connections 维护）
    if _is_clawnode(node_sn):
        return str(node_sn)
    if wait:
        return wait_for_adb_device(node_sn, platform, timeout=wait_timeout)
    mobile_sn = resolve_mobile_serial(node_sn, platform)
    if mobile_sn not in list_connected_adb_serials():
        raise DeviceOfflineError(
            f"设备已离线（node={node_sn}, adb={mobile_sn}）。"
            "请确认 USB 连接与 adb devices 状态。"
        )
    return mobile_sn


def _cache_engine(mobile_sn: str, platform: str, engine: object, size: Tuple[int, int]) -> None:
    key = f"{mobile_sn}:{platform}"
    _ENGINE_CACHE[key] = {
        "engine": engine,
        "size": size,
        "ts": time.time(),
        "mobile_sn": mobile_sn,
    }
    try:
        from server.services.shared.run_context.regression_run_context import get_ctx

        ctx = get_ctx()
        if ctx is not None:
            ctx["engine"] = engine
            ctx["engine_sn"] = mobile_sn
            ctx["screen_size"] = list(size)
    except Exception:
        pass


def _engine_bound_sn(engine: object) -> str:
    return str(
        getattr(engine, "_serial", None)
        or getattr(engine, "_test_subject", None)
        or ""
    ).strip()


def _purge_stale_engine_cache(engine: object, bound_sn: str) -> None:
    """Singleton RemoteEngine 会被多个 SN 缓存条目引用；换绑后清掉其它 SN 的过期条目。"""
    bound_sn = str(bound_sn or "").strip()
    if not engine or not bound_sn:
        return
    stale = [
        key
        for key, entry in list(_ENGINE_CACHE.items())
        if entry.get("engine") is engine and not key.startswith(f"{bound_sn}:")
    ]
    for key in stale:
        _ENGINE_CACHE.pop(key, None)


def _ensure_engine_bound(engine: object, mobile_sn: str) -> bool:
    """复用缓存前确保 engine 仍绑定到目标 SN（RemoteEngine 为进程内单例）。"""
    mobile_sn = str(mobile_sn or "").strip()
    if not engine or not mobile_sn:
        return False
    if _engine_bound_sn(engine) == mobile_sn:
        builtins.TARGET_DEVICE_SN = mobile_sn
        return True
    if not hasattr(engine, "init_driver"):
        return False
    # claw- 设备只能由 RemoteEngine 承载；adb 引擎不能 init claw SN
    if _is_clawnode(mobile_sn) and type(engine).__name__ != "RemoteEngine":
        return False
    if not _is_clawnode(mobile_sn) and type(engine).__name__ == "RemoteEngine":
        return False
    engine.init_driver(mobile_sn)
    builtins.TARGET_DEVICE_SN = mobile_sn
    if _engine_bound_sn(engine) != mobile_sn:
        return False
    _purge_stale_engine_cache(engine, mobile_sn)
    return True


def _try_reuse_engine(mobile_sn: str, platform: str) -> Optional[Tuple[object, Tuple[int, int]]]:
    mobile_sn = str(mobile_sn or "").strip()
    if not mobile_sn:
        return None
    try:
        from server.services.shared.run_context.regression_run_context import get_ctx

        ctx = get_ctx()
        if ctx and ctx.get("engine_sn") == mobile_sn and ctx.get("engine"):
            eng = ctx["engine"]
            if _ensure_engine_bound(eng, mobile_sn):
                size = ctx.get("screen_size") or [1080, 1920]
                return eng, (int(size[0]), int(size[1]))
    except Exception:
        pass

    key = f"{mobile_sn}:{platform}"
    entry = _ENGINE_CACHE.get(key)
    if not entry:
        return None
    if time.time() - float(entry.get("ts") or 0) > _ENGINE_CACHE_TTL_SEC:
        _ENGINE_CACHE.pop(key, None)
        return None
    eng = entry.get("engine")
    size = entry.get("size")
    if not eng or not size:
        return None
    if not _ensure_engine_bound(eng, mobile_sn):
        _ENGINE_CACHE.pop(key, None)
        return None
    return eng, tuple(size)


def clear_engine_cache(node_sn: str = "", platform: str = "android") -> None:
    """断开设备或跑批结束时释放缓存。"""
    if node_sn:
        mobile_sn = resolve_mobile_serial(node_sn, platform)
        key = f"{mobile_sn}:{platform}"
        entry = _ENGINE_CACHE.pop(key, None)
        if entry and entry.get("engine") is not None:
            try:
                from server.services.shared.page_context.page_context_service import invalidate_engine_screen_cache

                invalidate_engine_screen_cache(entry["engine"])
            except Exception:
                pass
    else:
        for entry in _ENGINE_CACHE.values():
            eng = entry.get("engine")
            if eng is not None:
                try:
                    from server.services.shared.page_context.page_context_service import invalidate_engine_screen_cache

                    invalidate_engine_screen_cache(eng)
                except Exception:
                    pass
        _ENGINE_CACHE.clear()
    try:
        from server.services.shared.run_context.regression_run_context import get_ctx

        ctx = get_ctx()
        if ctx is not None:
            ctx.pop("engine", None)
            ctx.pop("engine_sn", None)
            ctx.pop("screen_size", None)
    except Exception:
        pass


def bootstrap_mobile_engine(
    node_sn: str,
    platform: str = "android",
    *,
    reuse: bool = True,
) -> Tuple[object, Tuple[int, int]]:
    """
    初始化 Manager + MobileEngine，返回 (engine, (width, height))。
    同一设备在回归批次内会复用引擎实例，避免重复 bootstrap。
    """
    plat = (platform or "android").lower()
    if plat in ("ios", "iphone", "ipad"):
        from server.services.runtime.ios_wda_session import get_ios_engine

        mobile_sn = str(node_sn or "")
        builtins.TARGET_DEVICE_SN = mobile_sn
        memory_manager.short_term.set_global("platform", "ios")
        memory_manager.short_term.set_global("run_device_sn", mobile_sn)
        if reuse:
            cached = _try_reuse_engine(mobile_sn, "ios")
            if cached:
                return cached
        engine = get_ios_engine(mobile_sn)
        try:
            # 走引擎层 screen_size()，wda / appium 后端通用
            w, h = engine.screen_size()
            w, h = int(w), int(h)
        except Exception:
            w, h = 390, 844
        size = (w, h)
        SLog.i(TAG, f"iOS engine ready sn={mobile_sn} screen={w}x{h}")
        if reuse:
            _cache_engine(mobile_sn, "ios", engine, size)
        return engine, size

    mobile_sn = ensure_adb_device_online(node_sn, platform, wait=True, wait_timeout=8.0)
    if reuse:
        cached = _try_reuse_engine(mobile_sn, platform)
        if cached:
            return cached

    builtins.TARGET_DEVICE_SN = mobile_sn
    memory_manager.short_term.set_global("platform", platform)
    memory_manager.short_term.set_global("run_device_sn", mobile_sn)

    info = TaskDetails({
        "id": "sys_crawl_bootstrap",
        "nodeCode": "tools/screenshot",
        "nodeType": 200,
        "displayName": "跑图设备初始化",
        "platform": platform,
        "data": {"platform": platform},
        "lastCodes": [],
        "nextCodes": [],
    })

    mgr = Manager()
    if mgr.MobileEngine:
        prev = getattr(mgr.MobileEngine, "_serial", None) or getattr(
            mgr.MobileEngine, "_test_subject", None
        )
        want_remote = _is_clawnode(mobile_sn)
        is_remote = type(mgr.MobileEngine).__name__ == "RemoteEngine"
        if prev != mobile_sn and want_remote == is_remote:
            mgr.MobileEngine._test_subject = mobile_sn
            if hasattr(mgr.MobileEngine, "init_driver"):
                mgr.MobileEngine.init_driver(mobile_sn)
                _purge_stale_engine_cache(mgr.MobileEngine, mobile_sn)
    mgr.online(info)
    engine = mgr.MobileEngine
    if engine is None:
        raise RuntimeError(
            f"无法初始化移动端引擎（node={node_sn}, mobile_sn={mobile_sn}）。"
            "请确认手机已 USB 连接且 adb devices 可见。"
        )

    if hasattr(engine, "ensure_screen_ready"):
        try:
            ok = engine.ensure_screen_ready(node_sn=node_sn)
            if not ok:
                time.sleep(1.0)
                ok = engine.ensure_screen_ready(node_sn=node_sn)
            if not ok:
                SLog.e(
                    TAG,
                    f"screen not ready sn={mobile_sn}；OCR/自动化可能失败。"
                    "请确认设备管理已配置锁屏密码，或手动解锁手机。",
                )
        except Exception as e:
            SLog.w(TAG, f"ensure_screen_ready failed: {e}")
            if hasattr(engine, "screen_on"):
                engine.screen_on()
    elif hasattr(engine, "screen_on"):
        engine.screen_on()

    if platform == "android" and hasattr(engine, "ensure_input_ready"):
        engine.ensure_input_ready()
        mode = getattr(engine, "_input_mode", "unknown")
        SLog.i(TAG, f"Android input mode={mode}")

    if hasattr(engine, "screen_size"):
        w, h = engine.screen_size()
    elif hasattr(engine, "_display_size"):
        w, h = engine._display_size()
    else:
        w, h = 1080, 1920

    size = (int(w), int(h))
    SLog.i(TAG, f"Mobile engine ready sn={mobile_sn} screen={w}x{h}")
    if reuse:
        _cache_engine(mobile_sn, platform, engine, size)
    return engine, size
