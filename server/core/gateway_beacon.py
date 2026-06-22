"""
Gateway mDNS 广播 — 对齐 OpenClaw discovery 思路。

- 主 beacon：`_miniorange-gw._tcp`（专用 gateway transport type）
- 兼容 beacon：`_http._tcp` + 实例名 `miniorange-{hostname}`（旧客户端）

TXT 仅作 UX 提示，路由以 SRV/A/AAAA 解析为准。
"""
from __future__ import annotations

import platform
import socket
from dataclasses import dataclass

from zeroconf import IPVersion, ServiceInfo
from zeroconf.asyncio import AsyncZeroconf

from script.log import SLog

TAG = "GatewayBeacon"

GATEWAY_SERVICE_TYPE = "_miniorange-gw._tcp.local."
LEGACY_HTTP_TYPE = "_http._tcp.local."
DEFAULT_WS_PATH = "/ws"


@dataclass
class GatewayBeaconHandle:
    aiozc: AsyncZeroconf
    services: list[ServiceInfo]
    instance_id: str
    mdns_hostname: str
    local_ip: str
    port: int


def _safe_hostname() -> str:
    hostname = platform.node().split(".")[0]
    safe = "".join(c for c in hostname if c.isalnum() or c == "-") or "miniorange"
    return safe


def build_gateway_identity(local_ip: str | None = None) -> dict:
    ip = local_ip or _get_local_ip()
    safe = _safe_hostname()
    instance_id = f"miniorange-{safe}"
    mdns_hostname = f"{instance_id}.local."
    return {
        "instance_id": instance_id,
        "mdns_hostname": mdns_hostname,
        "display_name": instance_id,
        "local_ip": ip,
        "lan_host": mdns_hostname.rstrip("."),
    }


def _get_local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


async def register_gateway_beacons(
    port: int = 10104,
    ws_path: str = DEFAULT_WS_PATH,
    display_name: str | None = None,
) -> GatewayBeaconHandle:
    identity = build_gateway_identity()
    ip = identity["local_ip"]
    instance_id = identity["instance_id"]
    mdns_hostname = identity["mdns_hostname"]
    name = display_name or identity["display_name"]

    txt = {
        "role": "gateway",
        "transport": "gateway",
        "displayName": name,
        "lanHost": mdns_hostname.rstrip("."),
        "gatewayPort": str(port),
        "path": ws_path if ws_path.startswith("/") else f"/{ws_path}",
        "version": "1",
    }
    txt_bytes = {k: v.encode("utf-8") for k, v in txt.items()}

    gw_info = ServiceInfo(
        GATEWAY_SERVICE_TYPE,
        f"{instance_id}.{GATEWAY_SERVICE_TYPE}",
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties=txt_bytes,
        server=mdns_hostname,
    )
    legacy_info = ServiceInfo(
        LEGACY_HTTP_TYPE,
        f"{instance_id}.{LEGACY_HTTP_TYPE}",
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties=txt_bytes,
        server=mdns_hostname,
    )

    aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    services: list[ServiceInfo] = []
    for info in (gw_info, legacy_info):
        try:
            await aiozc.async_register_service(info, allow_name_change=True)
            services.append(info)
            SLog.i(
                TAG,
                f"mDNS registered {info.name} → ws://{ip}:{port}{txt['path']} "
                f"({mdns_hostname.rstrip('.')})",
            )
        except Exception as e:
            SLog.w(TAG, f"mDNS register failed {info.name}: {e}")

    return GatewayBeaconHandle(
        aiozc=aiozc,
        services=services,
        instance_id=instance_id,
        mdns_hostname=mdns_hostname,
        local_ip=ip,
        port=port,
    )


async def unregister_gateway_beacons(handle: GatewayBeaconHandle | None) -> None:
    if not handle:
        return
    for info in handle.services:
        try:
            await handle.aiozc.async_unregister_service(info)
        except Exception as e:
            SLog.w(TAG, f"mDNS unregister failed {info.name}: {e}")
    try:
        await handle.aiozc.async_close()
    except Exception as e:
        SLog.w(TAG, f"mDNS close failed: {e}")
