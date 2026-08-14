# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""RunContext：每次回归用例 / Run 启动时构造的运行时上下文。

它做两件事：
  1. 跑 4 个 connectivity_probe，得到通道连通性 (adb / remote / vlm / hitl)
  2. 从 MDevice 读取设备元信息 (model / os_version / resolution / ...)

产出物：
  - ctx.connectivity_flags：喂给 plugins.registry.filter_capabilities_by_connectivity()
  - ctx.to_prompt_brief()：喂给 PLAN_OVERVIEW_TEXT prompt（Step 3 接入）
  - ctx.device_signature：用作 baseline trace 的设备指纹

不在这里做的事：
  - 不持久化（不写 DB）；channels 的持久化由 device_manager + Run 完成时回写
  - 不缓存（同 sn 多次 build_run_context 会重新探测，谨慎用在长 Run 内部）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from script.log import SLog

from server.services.runtime import connectivity_probe as cp

TAG = "RunContext"


@dataclass
class RunContext:
    sn: str
    platform: str = "android"
    run_id: str = ""

    # 任务归属（BE-P0-3）：落到 trace.run_context / trace 列，供按应用/任务查询用例历史
    app_id: str = ""
    batch_id: str = ""

    # 设备元信息（来自 MDevice）
    device_type: str = ""
    model: str = ""
    os_version: str = ""
    resolution: str = ""
    role: str = ""

    # 通道探测结果（state + meta）
    remote: dict[str, Any] = field(default_factory=dict)
    adb: dict[str, Any] = field(default_factory=dict)
    vlm: dict[str, Any] = field(default_factory=dict)
    hitl: dict[str, Any] = field(default_factory=dict)
    ios: dict[str, Any] = field(default_factory=dict)

    # AI provider hints
    provider_id: str = ""
    model_name: str = ""

    # 被测应用包名（来自应用配置 / case_runner）
    target_package: str = ""

    # ---------- properties ----------

    @property
    def device_signature(self) -> str:
        """用作 baseline trace key 的设备指纹（不含 sn，便于跨设备相同型号复用）。"""
        parts: list[str] = []
        if self.model:
            parts.append(self.model)
        if self.os_version:
            parts.append(self.os_version)
        if self.resolution:
            parts.append(self.resolution)
        return " / ".join(parts) if parts else (self.sn or "unknown")

    @property
    def connectivity_flags(self) -> dict[str, bool]:
        """喂给 plugins.registry.filter_capabilities_by_connectivity 的精简 dict。

        key 必须与 capabilities/*.yaml > implementations[].executor 用到的 id 对齐。
        """
        return {
            "adb": (self.adb.get("state") == "connected"),
            "remote": (self.remote.get("state") == "connected"),
            "vlm": (self.vlm.get("state") == "available"),
            "hitl": (self.hitl.get("state") == "available"),
            "ios_wda": (self.ios.get("state") == "connected"),
            "web_cdp": False,
            "pc_winapi": False,
            "mac_apple_script": False,
            "ai_persona": (self.vlm.get("state") == "available"),
        }

    # ---------- prompt ----------

    def to_prompt_brief(self, *, app_cache_cleared: bool = False) -> dict[str, Any]:
        """注入到 PLAN_OVERVIEW_TEXT prompt 的 connectivity_brief 区段。

        刻意输出"扁平 dict + 人类可读 advice"，让 prompt 模板可以直接 jinja /
        f-string 拼装，无需二次结构化。
        """
        flags = self.connectivity_flags
        adb_on = flags["adb"]
        remote_on = flags["remote"]
        ios_on = flags.get("ios_wda", False)
        vlm_on = flags["vlm"]
        hitl_on = flags["hitl"]

        if ios_on and not adb_on and not remote_on:
            advice = (
                "ios_wda=true：走 WebDriverAgent（USB/usbmuxd 或已配对 Wi‑Fi）；"
                "不要规划 adb / ClawNode remote 事件。"
            )
        elif adb_on and remote_on:
            advice = (
                "adb=true & remote=true：所有路径都可选；"
                "系统级（install_apk/system_pkg_clear/read_device_data）优先选 adb，"
                "UI 交互（tap_element/input_text）优先选 remote。"
            )
        elif adb_on and not remote_on:
            advice = (
                "adb=true & remote=false：仅 adb 可用；"
                "所有 UI 交互都用 adb input tap/text；不要规划 remote 才支持的事件。"
            )
        elif (not adb_on) and remote_on:
            advice = (
                "adb=false & remote=true：仅 remote 可用；"
                "清缓存 / 装包 / 强停因 Android 权限限制需通过 PERSONA 拟人化点击。"
            )
        elif ios_on:
            advice = "ios_wda=true：使用 WDA 点击/滑动/截图。"
        else:
            advice = (
                "adb=false & remote=false & ios_wda=false：无可用执行通道，"
                "PLAN 应直接 decline 并给出 reasoning，不要凭空规划事件。"
            )

        # 额外能力提示
        capability_notes: list[str] = []
        if not vlm_on:
            capability_notes.append("vlm=false：禁止规划任何 needs_vlm=true 的事件（tap_element / assert_visual 等）。")
        if not hitl_on:
            capability_notes.append("hitl=false：禁止规划 human_* 事件；遇到需要人工确认的步骤改为 decline。")
        if app_cache_cleared:
            capability_notes.append(
                "precondition_done: 应用清缓存前置已在规划前执行完成（含 EXEC_SCRIPT 打开应用详情页）；"
                "禁止再规划 clear_app_cache。"
            )

        return {
            "sn": self.sn,
            "platform": self.platform,
            "device_signature": self.device_signature,
            "device_model": self.model,
            "device_os": self.os_version,
            "device_resolution": self.resolution,
            "device_role": self.role,
            "channels": {
                "adb": self.adb.get("state"),
                "remote": self.remote.get("state"),
                "ios": self.ios.get("state"),
                "vlm": self.vlm.get("state"),
                "hitl": self.hitl.get("state"),
            },
            "flags": {
                "adb": adb_on,
                "remote": remote_on,
                "ios_wda": ios_on,
                "vlm": vlm_on,
                "hitl": hitl_on,
            },
            "router_advice": advice,
            "capability_notes": capability_notes,
        }

    def to_dict(self) -> dict[str, Any]:
        """完整序列化，用于落入 trace / 持久化。"""
        return {
            "sn": self.sn,
            "run_id": self.run_id,
            "app_id": self.app_id,
            "batch_id": self.batch_id or self.run_id,
            "platform": self.platform,
            "device": {
                "device_type": self.device_type,
                "model": self.model,
                "os_version": self.os_version,
                "resolution": self.resolution,
                "role": self.role,
                "signature": self.device_signature,
            },
            "channels": {
                "remote": self.remote,
                "adb": self.adb,
                "ios": self.ios,
                "vlm": self.vlm,
                "hitl": self.hitl,
            },
            "ai": {
                "provider_id": self.provider_id,
                "model_name": self.model_name,
            },
            "target_package": self.target_package,
        }


# ============== builder ==============


def _is_ios_target(sn: str, platform: str = "", device_type: str = "") -> bool:
    plat = f"{platform} {device_type}".lower()
    if "ios" in plat or "iphone" in plat or "ipad" in plat:
        return True
    try:
        from server.services.runtime.ios_ids import is_physical_ios_udid
        return is_physical_ios_udid(sn)
    except Exception:
        return False


def _resolve_adb_serial(sn: str, platform: str, device_type: str = "") -> str:
    """把 sn 解析为可用的 adb serial。iOS UDID 不能当 adb serial。"""
    if not sn:
        return ""
    if _is_ios_target(sn, platform, device_type):
        return ""
    if not str(sn).startswith("claw-"):
        return str(sn)
    try:
        from driver.agent.Crawl.device_bootstrap import resolve_mobile_serial

        resolved = resolve_mobile_serial(sn, platform=platform or "android")
        if resolved and not str(resolved).startswith("claw-"):
            return str(resolved)
    except Exception as e:
        SLog.w(TAG, f"resolve_mobile_serial failed for {sn}: {e}")
    return ""


def _load_device_meta(sn: str) -> dict[str, Any]:
    """读 MDevice 拿设备元信息。读失败返回空 dict。"""
    if not sn:
        return {}
    try:
        from server.core.database import SessionLocal
        from server.models.mDevice import MDevice

        with SessionLocal() as db:
            dev = db.query(MDevice).filter(MDevice.sn == sn).first()
            if not dev:
                return {}
            return {
                "device_type": dev.device_type or "",
                "model": dev.model or "",
                "os_version": dev.os_version or "",
                "resolution": dev.resolution or "",
                "role": dev.role or "",
                "channels": dev.channels or {},
            }
    except Exception as e:
        SLog.w(TAG, f"load MDevice({sn}) failed: {e}")
        return {}


def build_run_context(
    sn: str,
    *,
    platform: str = "android",
    run_id: str = "",
    app_id: str = "",
    batch_id: str = "",
    provider_id: str = "",
    model_name: str = "",
    target_package: str = "",
    probe_adb_channel: bool = True,
    probe_remote_channel: bool = True,
    probe_vlm_channel: bool = True,
    probe_hitl_channel: bool = True,
) -> RunContext:
    """构造一次 Run 的上下文：读设备 + 4 路探测。

    特意拆出 probe_* 开关，方便单元测试或在已知通道状态时跳过。
    """
    ctx = RunContext(
        sn=sn or "",
        platform=platform or "android",
        run_id=run_id or "",
        app_id=app_id or "",
        batch_id=batch_id or run_id or "",
        provider_id=provider_id or "",
        model_name=model_name or "",
        target_package=(target_package or "").strip(),
    )

    # 1. 设备元信息
    meta = _load_device_meta(ctx.sn)
    ctx.device_type = meta.get("device_type", "")
    ctx.model = meta.get("model", "")
    ctx.os_version = meta.get("os_version", "")
    ctx.resolution = meta.get("resolution", "")
    ctx.role = meta.get("role", "")

    # 2. Remote
    if probe_remote_channel:
        state, meta_r = cp.probe_remote(ctx.sn)
        ctx.remote = {"state": state, **meta_r}
    else:
        ctx.remote = {"state": "disconnected", "reason": "probe skipped"}

    # 3. ADB（先把 sn 解析成 adb serial；iOS 不走 adb）
    if probe_adb_channel and not _is_ios_target(ctx.sn, ctx.platform, ctx.device_type):
        adb_serial = _resolve_adb_serial(ctx.sn, ctx.platform, ctx.device_type)
        if not adb_serial:
            ctx.adb = {
                "state": "not_applicable",
                "serial": "",
                "reason": "cannot resolve adb serial from sn",
            }
        else:
            state, meta_a = cp.probe_adb(adb_serial)
            ctx.adb = {"state": state, "serial": adb_serial, **meta_a}
    else:
        ctx.adb = {"state": "not_applicable", "reason": "ios device or probe skipped"}

    # 3b. iOS usbmuxd / WDA
    if _is_ios_target(ctx.sn, ctx.platform, ctx.device_type):
        state, meta_i = cp.probe_ios(ctx.sn)
        ctx.ios = {"state": state, "udid": ctx.sn, **meta_i}
    else:
        ctx.ios = {"state": "not_applicable"}

    # 4. VLM
    if probe_vlm_channel:
        state, meta_v = cp.probe_vlm(ctx.provider_id, ctx.model_name)
        ctx.vlm = {"state": state, **meta_v}
    else:
        ctx.vlm = {"state": "available", "reason": "probe skipped"}

    # 5. HITL
    if probe_hitl_channel:
        state, meta_h = cp.probe_hitl()
        ctx.hitl = {"state": state, **meta_h}
    else:
        ctx.hitl = {"state": "available", "reason": "probe skipped"}

    SLog.i(
        TAG,
        f"RunContext built sn={ctx.sn} adb={ctx.adb.get('state')} remote={ctx.remote.get('state')} "
        f"ios={ctx.ios.get('state')} vlm={ctx.vlm.get('state')} hitl={ctx.hitl.get('state')}",
    )
    return ctx
