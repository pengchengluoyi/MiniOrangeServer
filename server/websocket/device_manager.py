# !/usr/bin/env python
# -*-coding:utf-8 -*-

import json
import re
import time
import uuid
from typing import Callable, Dict, Optional, TypeVar
from fastapi import WebSocket
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError
from datetime import datetime, timedelta
import asyncio

from server.core.database import engine
from server.models.mDevice import MDevice, MDeviceLog
from server.core.log_database import LogSessionLocal
from server.models.log import WorkflowLog
from script.log import SLog

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

DEFAULT_PAIR_PORT = 10105

# 创建会话工厂
SessionLocal = sessionmaker(bind=engine)

T = TypeVar("T")


def _with_db_retry(action: Callable[[], T], *, retries: int = 6, base_delay: float = 0.05) -> T:
    """SQLite 写冲突时短暂退避重试。"""
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return action()
        except OperationalError as e:
            last_err = e
            if "locked" not in str(e).lower() or attempt >= retries - 1:
                raise
            time.sleep(base_delay * (2 ** attempt))
    if last_err:
        raise last_err
    raise RuntimeError("db retry failed")


class DeviceManager:
    _instance = None
    
    # 内存中维护活跃连接: { "device_sn": WebSocket }
    active_connections: Dict[str, WebSocket] = {}
    # 维护观察者连接 (如前端页面)
    observers: set = set()

    # [新增] 维护流会话映射: { "sender_sn": "viewer_sn" }
    # 用于在一方断开时通知另一方
    stream_sessions: Dict[str, str] = {}
    _stop_command_sent: set = set()

    # [ClawNode] 直连节点的 SN 集合。标记哪些设备是 ClawNode 直连（说 ClawNode 方言），
    # 使 control/stream 指令走翻译分支而非 PC Node 分支。是“连接对象身份”的载体。
    direct_nodes: set = set()

    # [ClawNode] 主 event loop 引用，供 RemoteEngine 从 worker 线程 run_coroutine_threadsafe。
    # 在 main.py lifespan 启动时赋值。
    loop = None

    # [ClawNode] 待桌面端确认的配对配置 { sn: {ws_url, auth_token, gateway_id, expires} }
    pending_pairings: Dict[str, dict] = {}
    # [ClawNode] 注册时上报的扩展元数据 { sn: {app_version, ...} }
    device_meta: Dict[str, dict] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DeviceManager, cls).__new__(cls)
        return cls._instance

    def get_device_ws(self, sn: str) -> WebSocket:
        """获取指定设备的 WebSocket 连接对象 (供二进制流转发使用)"""
        return self.active_connections.get(sn)

    def _get_req_id(self, data: dict):
        """尝试从最外层或 data 层获取 req_id"""
        req_id = data.get("req_id")
        if not req_id:
            inner = data.get("data")
            if isinstance(inner, dict):
                req_id = inner.get("req_id")
        return req_id

    async def register(self, websocket: WebSocket, data: dict):
        """处理设备注册 (对应 wsMap 中的 register)"""
        sn = data.get("sn")
        if not sn:
            return {"code": 400, "msg": "Missing SN"}
        
        # 1. 建立内存映射
        self.active_connections[sn] = websocket
        
        # 2. 数据库注册/更新
        self._register_device(sn, data)
        
        # 3. 记录日志
        self._save_log(sn, "receive", "register", json.dumps(data))

        return {"code": 200, "msg": "Registered successfully"}

    def register_clawnode(self, websocket: WebSocket, data: dict):
        """
        [ClawNode] 注册直连节点。
        """
        sn = data.get("sn")
        if not sn:
            return
        self.observers.discard(websocket)
        self.active_connections[sn] = websocket
        self.direct_nodes.add(sn)
        if data.get("app_version"):
            self.device_meta[sn] = {
                **self.device_meta.get(sn, {}),
                "app_version": data.get("app_version"),
            }
        if not data.get("type"):
            data = {**data, "type": "android_direct"}
        self._register_device(sn, data)
        self._save_log(sn, "receive", "register_clawnode", json.dumps(data))
        SLog.i("DeviceManager", f"register_clawnode sn={sn} model={data.get('model')} app={data.get('app_version')}")
        asyncio.create_task(self._after_clawnode_register(websocket, sn))

    async def _after_clawnode_register(self, websocket: WebSocket, sn: str):
        pending = self.pending_pairings.get(sn)
        if pending and pending.get("expires", 0) > time.time():
            SLog.i("DeviceManager", f"register_clawnode pending PAIR_CONFIG sn={sn}")
            await self._send_pair_config(websocket, sn, pending)
            self.pending_pairings.pop(sn, None)
        await self.notify_device_list_changed("register", sn)

    async def _send_pair_config(self, websocket: WebSocket, sn: str, config: dict):
        payload = {
            "type": "PAIR_CONFIG",
            "data": {
                "ws_url": config.get("ws_url", ""),
                "auth_token": config.get("auth_token", ""),
                "gateway_id": config.get("gateway_id", ""),
            },
        }
        try:
            await websocket.send_text(json.dumps(payload))
            SLog.i(
                "DeviceManager",
                f"PAIR_CONFIG sent sn={sn} ws_url={config.get('ws_url')} gateway={config.get('gateway_id')} "
                f"token_len={len(config.get('auth_token') or '')}",
            )
        except Exception as e:
            SLog.w("DeviceManager", f"PAIR_CONFIG failed sn={sn}: {e}")

    async def _push_pair_config_http(self, ip: str, config: dict) -> bool:
        """向 ClawNode 局域网 HTTP 配对端口推送 PAIR_CONFIG（被动配对）。"""
        if not ip or not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            SLog.w("DeviceManager", f"HTTP pair push skip invalid ip={ip!r}")
            return False
        if httpx is None:
            SLog.w("DeviceManager", "HTTP pair push skip: httpx not installed")
            return False
        port = int(config.get("pair_port") or DEFAULT_PAIR_PORT)
        url = f"http://{ip}:{port}/pair"
        payload = {
            "ws_url": config.get("ws_url", ""),
            "auth_token": config.get("auth_token", ""),
            "gateway_id": config.get("gateway_id", ""),
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(url, json=payload)
                SLog.i(
                    "DeviceManager",
                    f"HTTP pair push ip={ip}:{port} status={resp.status_code} body={resp.text[:160]}",
                )
                return resp.status_code == 200
        except Exception as e:
            SLog.w("DeviceManager", f"HTTP pair push failed ip={ip}:{port}: {type(e).__name__}: {e!r}")
            return False

    async def _push_pair_config_http_with_retry(
        self, ip: str, config: dict, attempts: int = 6, interval: float = 2.0,
    ) -> bool:
        for attempt in range(attempts):
            if await self._push_pair_config_http(ip, config):
                return True
            if attempt < attempts - 1:
                await asyncio.sleep(interval)
        return False

    async def _pending_http_pair_loop(self, sn: str):
        """HTTP 首次失败后后台重试，直到配对成功或 pending 过期。"""
        while True:
            pending = self.pending_pairings.get(sn)
            if not pending or pending.get("expires", 0) <= time.time():
                return
            ip = (pending.get("ip") or "").strip()
            if ip and await self._push_pair_config_http(ip, pending):
                self.pending_pairings.pop(sn, None)
                SLog.i("DeviceManager", f"adopt_clawnode HTTP pair delivered (retry) sn={sn} ip={ip}")
                await self.notify_device_list_changed("pair_pushed", sn)
                return
            await asyncio.sleep(3)

    async def adopt_clawnode(self, sn: str, config: dict) -> dict:
        """桌面端确认添加设备：下发配对配置，或等待 pairing 模式连接。"""
        if not sn:
            return {"code": 400, "msg": "missing sn"}
        self._ensure_adopted_device(sn, config)
        config = {**config, "expires": time.time() + 300}
        self.pending_pairings[sn] = config
        ws = self.active_connections.get(sn)
        online = ws is not None
        SLog.i(
            "DeviceManager",
            f"adopt_clawnode sn={sn} online={online} ws_url={config.get('ws_url')} "
            f"gateway={config.get('gateway_id')} expires_in=300s pending_keys={list(self.pending_pairings.keys())}",
        )
        if ws:
            await self._send_pair_config(ws, sn, config)
            self.pending_pairings.pop(sn, None)
            SLog.i("DeviceManager", f"adopt_clawnode PAIR_CONFIG delivered immediately sn={sn}")
        else:
            device_ip = (config.get("ip") or "").strip()
            pushed = (
                await self._push_pair_config_http_with_retry(device_ip, config)
                if device_ip else False
            )
            if pushed:
                self.pending_pairings.pop(sn, None)
                SLog.i("DeviceManager", f"adopt_clawnode HTTP pair delivered sn={sn} ip={device_ip}")
            else:
                SLog.i(
                    "DeviceManager",
                    f"adopt_clawnode HTTP pair pending sn={sn} ip={device_ip or 'none'} "
                    f"(device listens on :{config.get('pair_port') or DEFAULT_PAIR_PORT})",
                )
                if device_ip:
                    asyncio.create_task(self._pending_http_pair_loop(sn))
        await self.notify_device_list_changed("adopt", sn)
        from server.services.device_service import DeviceService

        devices = []
        for d in DeviceService.list_all():
            meta = self.device_meta.get(d.sn, {})
            devices.append({
                "sn": d.sn,
                "type": d.device_type,
                "role": d.role,
                "model": d.model,
                "ip": d.ip_address,
                "status": d.status,
                "app_version": meta.get("app_version"),
                "last_online": str(d.last_online_time) if d.last_online_time else None,
            })
        return {
            "code": 200,
            "msg": "adopt ok",
            "data": {"sn": sn, "pending": sn in self.pending_pairings, "devices": devices},
        }

    def _ensure_adopted_device(self, sn: str, config: dict):
        """添加时预写入设备表（offline），桌面端列表立即可见。"""
        from server.services.device_service import is_valid_sn

        if not is_valid_sn(sn):
            SLog.d("DeviceManager", f"Skip invalid adopt sn={sn!r}")
            return
        try:
            with SessionLocal() as db:
                device = db.query(MDevice).filter(MDevice.sn == sn).first()
                if not device:
                    device = MDevice(sn=sn)
                    db.add(device)
                if device.status != "online":
                    device.status = "offline"
                device.device_type = config.get("type") or device.device_type or "android_direct"
                device.role = device.role or "node"
                model = (config.get("model") or "").strip()
                if model:
                    device.model = model
                ip = (config.get("ip") or "").strip()
                if ip:
                    device.ip_address = ip
                db.commit()
                SLog.i("DeviceManager", f"Adopted device pre-registered: {sn}")
        except Exception as e:
            SLog.e("DeviceManager", f"DB Error adopt register: {e}")

    async def notify_device_list_changed(self, event: str, sn: str = ""):
        from server.services.device_service import DeviceService

        devices = []
        for d in DeviceService.list_all():
            meta = self.device_meta.get(d.sn, {})
            devices.append({
                "sn": d.sn,
                "type": d.device_type,
                "role": d.role,
                "model": d.model,
                "ip": d.ip_address,
                "status": d.status,
                "app_version": meta.get("app_version"),
                "last_online": str(d.last_online_time) if d.last_online_time else None,
            })
        payload = {
            "type": "device_list_update",
            "data": {
                "event": event,
                "sn": sn,
                "devices": devices,
            },
        }
        await self.broadcast_to_observers(payload)

    async def request_clawnode_logs(self, sn: str, minutes: int = 5) -> dict:
        """向在线 ClawNode 下发 EXPORT_LOGS，触发设备上传日志。"""
        from server.websocket.routers.wClawNode import translate_control_to_clawnode

        ws = self.active_connections.get(sn)
        if not ws or sn not in self.direct_nodes:
            return {"code": 404, "msg": "device offline or not clawnode"}
        trace_id = f"log-{uuid.uuid4().hex[:12]}"
        frame = translate_control_to_clawnode({
            "action": "export_logs",
            "trace_id": trace_id,
            "minutes": max(1, min(int(minutes or 5), 24 * 60)),
        })
        ok = await self._safe_send(ws, frame)
        return {"code": 200 if ok else 500, "msg": "export requested" if ok else "send failed", "trace_id": trace_id}

    async def _send_unpair_config(self, websocket: WebSocket, sn: str):
        payload = {"type": "UNPAIR_CONFIG", "data": {"sn": sn}}
        try:
            await websocket.send_text(json.dumps(payload))
            SLog.i("DeviceManager", f"UNPAIR_CONFIG sent to {sn}")
        except Exception as e:
            SLog.w("DeviceManager", f"UNPAIR_CONFIG failed {sn}: {e}")

    async def unbind_device(self, sn: str) -> dict:
        """从 Server 解绑设备：通知客户端清除配对、关闭连接并删除库表记录。"""
        if not sn:
            return {"code": 400, "msg": "missing sn"}
        ws = self.active_connections.get(sn)
        if ws:
            await self._send_unpair_config(ws, sn)
            try:
                await ws.close(code=1000, reason="unbind")
            except Exception:
                pass
        self.active_connections.pop(sn, None)
        self.direct_nodes.discard(sn)
        self.device_meta.pop(sn, None)
        self.pending_pairings.pop(sn, None)
        if ws:
            self.observers.discard(ws)

        def _delete():
            with SessionLocal() as db:
                device = db.query(MDevice).filter(MDevice.sn == sn).first()
                if device:
                    db.delete(device)
                    db.commit()

        try:
            _with_db_retry(_delete)
        except Exception as e:
            SLog.e("DeviceManager", f"unbind db error {sn}: {e}")
            return {"code": 500, "msg": str(e)}

        await self.notify_device_list_changed("unbind", sn)
        SLog.i("DeviceManager", f"Device unbound: {sn}")
        return {"code": 200, "msg": "unbound", "data": {"sn": sn}}

    async def broadcast_to_observers(self, payload: dict, exclude: WebSocket = None):
        """广播给桌面端观察者，不包含设备直连 WebSocket。"""
        msg_str = json.dumps(payload)
        for ws in set(self.observers):
            if ws is exclude:
                continue
            try:
                await ws.send_text(msg_str)
            except Exception:
                pass

    async def heartbeat(self, websocket: WebSocket, data: dict):
        """处理心跳 (对应 wsMap 中的 heartbeat)"""
        sn = data.get("sn")
        if sn:
            # 修复：如果设备是热插拔接入（未经过 register），或者服务重启后内存丢失
            # 只要收到心跳，就认为该设备可通过当前 WebSocket 访问，重建映射
            # 同时也处理设备漫游的情况（从一个节点移动到另一个节点），更新 WebSocket 引用
            if sn not in self.active_connections or self.active_connections[sn] != websocket:
                self.active_connections[sn] = websocket
                SLog.i("DeviceManager", f"Active connection updated for {sn} via heartbeat")
            self._update_device_status(sn, "online")
        return {"code": 200}

    async def disconnect(self, websocket: WebSocket, data: dict):
        # 找出断开的连接对应的 SN
        target_sns = [sn for sn, ws in self.active_connections.items() if ws == websocket]

        for sn in target_sns:
            # 🚨 关键修复: iOS 离线时，清理流会话
            await self._cleanup_on_disconnect(sn)
            if sn in self._stop_command_sent:
                self._stop_command_sent.remove(sn)  # 清理标记
            self.direct_nodes.discard(sn)  # [ClawNode] 清理直连标记
            if sn in self.active_connections:
                del self.active_connections[sn]
            self._update_device_status(sn, "offline")
            SLog.i("DeviceManager", f"Device disconnected: {sn}")
            asyncio.create_task(self.notify_device_list_changed("disconnect", sn))

    async def _cleanup_on_disconnect(self, disconnected_sn: str):
        """
        处理设备断开时的流清理
        """
        # 情况 1: 断开的是 iOS (接收端) -> 通知 Android 停止推流
        # 查找所有正在给这个 iOS 推流的设备
        senders_to_stop = [s for s, v in self.stream_sessions.items() if v == disconnected_sn]
        for sender_sn in senders_to_stop:
            SLog.i("DeviceManager", f"Viewer {disconnected_sn} left. Stopping stream on {sender_sn}")
            del self.stream_sessions[sender_sn]  # 移除记录

            sender_ws = self.active_connections.get(sender_sn)
            if sender_ws:
                # 发送停止指令给 client.py
                await self._safe_send(sender_ws, {
                    "type": "command",
                    "command": "stop_stream",
                    "params": {"reason": "viewer_disconnected"}
                })

        # 情况 2: 断开的是 Android (发送端) -> 通知 iOS 画面断了
        if disconnected_sn in self.stream_sessions:
            viewer_sn = self.stream_sessions.pop(disconnected_sn)
            viewer_ws = self.active_connections.get(viewer_sn)
            if viewer_ws:
                await self._safe_send(viewer_ws, {
                    "type": "command",
                    "command": "stream_ended",
                    "params": {"reason": "device_lost"}
                })

    async def handle_control_event(self, websocket: WebSocket, data: dict):
        """
        [解决 Point 5] 转发控制指令 (iOS -> Android)
        data: { "target_sn": "Android_SN", "action": "click", "x": 100, "y": 200 }
        """
        target_sn = data.get("target_sn")
        target_ws = self.active_connections.get(target_sn)

        if target_ws:
            # 直接转发给 Android 所在的 client.py
            cmd = {
                "type": "command",
                "command": "control",
                "params": data
            }
            await target_ws.send_text(json.dumps(cmd))
            return {"code": 200, "msg": "forwarded"}
        return {"code": 404, "msg": "device offline"}

    # [新增] 安全发送辅助方法
    async def _safe_send(self, ws: WebSocket, msg: dict) -> bool:
        try:
            await ws.send_text(json.dumps(msg))
            return True
        except Exception:
            return False

    async def send_command(self, sn: str, command: str, params: dict = None):
        """给设备发送指令"""
        if sn not in self.active_connections:
            SLog.w("DeviceManager", f"Device {sn} is offline")
            return False
        
        msg = {
            "type": "command",
            "command": command,
            "params": params or {},
            "timestamp": datetime.now().isoformat()
        }

        msg_str = json.dumps(msg)

        SLog.i("DeviceManager", f"msg_str {msg_str}")
        try:
            await self.active_connections[sn].send_text(msg_str)
            self._save_log(sn, "send", "command", msg_str)
            return True
        except Exception as e:
            SLog.e("DeviceManager", f"Send command failed: {e}")
            return False

    async def handle_list_dir(self, websocket: WebSocket, data: dict):
        """
        [前端 -> 服务端 -> 设备]
        处理前端请求获取文件列表
        data: { "sn": "target_device_sn", "path": "/" }
        """
        target_sn = data.get("sn")
        path = data.get("path", "/")
        req_id = self._get_req_id(data)

        # 将请求者(前端)加入观察者列表，以便接收后续的 dir_list 广播
        self.observers.add(websocket)
        
        if not target_sn:
            return {"code": 400, "msg": "Missing SN"}
            
        target_ws = self.active_connections.get(target_sn)
        if not target_ws:
            return {"code": 404, "msg": "Device offline"}
            
        # 构造指令发送给设备 (复用现有的 command 结构)
        cmd = {
            "type": "command",
            "command": "list_dir",
            "params": {"path": path}
        }
        try:
            await target_ws.send_text(json.dumps(cmd))
            return {"code": 200, "msg": "Request forwarded", "req_id": req_id}
        except Exception as e:
            return {"code": 500, "msg": str(e), "req_id": req_id}

    async def handle_dir_list(self, websocket: WebSocket, data: dict):
        """
        [设备 -> 服务端 -> 前端]
        处理设备返回的文件列表，广播给前端
        data: { "path": "...", "files": [...] }
        """
        sn = self._get_sn_by_ws(websocket)
        
        # 包装消息
        resp = {
            "type": "dir_list",
            "data": {
                "sn": sn,
                "path": data.get("path"),
                "files": data.get("files")
            }
        }
        msg_str = json.dumps(resp)
        
        # 广播给所有连接 (设备 + 观察者)
        targets = set(self.active_connections.values()) | self.observers
        for ws in targets:
            if ws != websocket:
                try:
                    await ws.send_text(msg_str)
                except: pass
        
        return {"code": 200, "msg": "ack"}

    async def handle_p2p_signal(self, websocket: WebSocket, data: dict):
        """
        处理设备间 P2P 文件传输信令转发
        data: { "target_sn": "...", "content": { "type": "...", ... } }
        """
        target_sn = data.get("target_sn")
        content = data.get("content")
        req_id = self._get_req_id(data)

        if not target_sn or not content:
            return {"code": 400, "msg": "Invalid P2P parameters"}

        source_sn = self._get_sn_by_ws(websocket)
        target_ws = self.active_connections.get(target_sn)

        if not target_ws:
            return {"code": 404, "msg": f"Target device {target_sn} is offline"}

        # 包装信令，注明来源，转发给目标
        payload = {"type": "p2p_signal", "source_sn": source_sn, "data": content}
        try:
            await target_ws.send_text(json.dumps(payload))
            return {"code": 200, "msg": "Signal forwarded", "req_id": req_id}
        except Exception as e:
            SLog.e("DeviceManager", f"P2P forward error: {e}")
            return {"code": 500, "msg": f"Forward error: {str(e)}", "req_id": req_id}

    async def handle_transfer_progress(self, websocket: WebSocket, data: dict):
        """
        处理设备上报的文件传输进度
        data: { "transfer_id": "...", "progress": 50.0, "speed": ..., "status": "..." }
        """
        req_id = self._get_req_id(data)
        # 1. 构造广播消息
        # 前端监听 type="transfer_progress" 即可获取进度
        payload = {
            "type": "transfer_progress",
            "data": data
        }
        msg_str = json.dumps(payload)

        # 2. 广播给所有连接 (设备 + 观察者)
        # 这样前端页面 (作为 WebSocket 客户端连接) 就能收到进度更新
        targets = set(self.active_connections.values()) | self.observers
        for ws in targets:
            if ws != websocket:
                try:
                    await ws.send_text(msg_str)
                except Exception as e:
                    # 发送失败不应中断广播循环
                    SLog.w("DeviceManager", f"Broadcast progress failed: {e}")

        return {"code": 200, "msg": "ack", "req_id": req_id}

    async def handle_start_stream(self, websocket: WebSocket, data: dict):
        """
        处理开始投屏请求
        data: { "device_sn": "...", "viewer_sn": "..." }
        """
        # [修复] 兼容 iOS 端发送的 target 字段
        device_sn = data.get("device_sn") or data.get("target")
        
        # [修复] 如果未传 viewer_sn，默认为当前请求的 WebSocket (即 iOS 端自己)
        viewer_sn = data.get("viewer_sn")
        if not viewer_sn:
            viewer_sn = self._get_sn_by_ws(websocket)
            
        req_id = self._get_req_id(data)

        if not device_sn or not viewer_sn:
            return {"code": 400, "msg": "Missing device_sn or viewer_sn"}

        # 找到目标设备（Android）所属的连接（PC Node）
        # 注意：如果是 USB 连接的手机，device_sn 对应的 WebSocket 其实是宿主 PC 的连接
        target_ws = self.active_connections.get(device_sn)
        if not target_ws:
            return {"code": 404, "msg": "Target device offline"}

        # [ClawNode] 直连节点：直接让设备自身开启推流（无需 PC + scrcpy）
        if device_sn in self.direct_nodes:
            from server.websocket.routers.wClawNode import translate_stream_to_clawnode
            await self._safe_send(target_ws, translate_stream_to_clawnode(True, data))
            return {"code": 200, "msg": "Stream command sent (clawnode)", "req_id": req_id}

        # 发送指令给 PC Node，让它开始推流
        cmd = {
            "type": "command",
            "command": "start_stream",
            "params": {
                "device_sn": device_sn,
                "viewer_sn": viewer_sn
            }
        }
        
        try:
            await target_ws.send_text(json.dumps(cmd))
            return {"code": 200, "msg": "Stream command sent", "req_id": req_id}
        except Exception as e:
            return {"code": 500, "msg": str(e), "req_id": req_id}

    async def handle_stop_stream(self, websocket: WebSocket, data: dict):
        """
        处理停止投屏请求
        """
        # [修复] 兼容 target 和自动获取 viewer_sn
        device_sn = data.get("device_sn") or data.get("target")
        viewer_sn = data.get("viewer_sn")
        if not viewer_sn:
            viewer_sn = self._get_sn_by_ws(websocket)
            
        req_id = self._get_req_id(data)

        target_ws = self.active_connections.get(device_sn)
        if target_ws:
            # [ClawNode] 直连节点：发 STOP_STREAM 方言
            if device_sn in self.direct_nodes:
                from server.websocket.routers.wClawNode import translate_stream_to_clawnode
                await self._safe_send(target_ws, translate_stream_to_clawnode(False, data))
                return {"code": 200, "msg": "Stop command sent (clawnode)", "req_id": req_id}
            cmd = {"type": "command", "command": "stop_stream", "params": {"viewer_sn": viewer_sn}}
            try:
                await target_ws.send_text(json.dumps(cmd))
            except: pass

        return {"code": 200, "msg": "Stop command sent", "req_id": req_id}

    async def handle_binary_stream(self, websocket: WebSocket, data: bytes):
        """
        处理二进制流转发
        协议: Magic(1)|Type(1)|SN_Len(1)|Target_SN(N)|Payload(...)
        """
        if len(data) < 4:
            return
        
        # 1. 解析协议头
        magic = data[0]
        if magic != 0xAA:
            return
            
        msg_type = data[1]
        sn_len = data[2]
        
        if len(data) < 3 + sn_len:
            return
            
        # 2. 获取目标 SN
        try:
            target_sn_bytes = data[3 : 3 + sn_len]
            target_sn = target_sn_bytes.decode('utf-8')
        except:
            return

        # 3. 转发给目标
        # 注意：这里我们直接转发原始二进制数据，保留协议头，
        # 这样接收端(iOS/Web)可以校验 Magic 并解析 Payload
        target_ws = self.active_connections.get(target_sn)
        if target_ws:
            try:
                await target_ws.send_bytes(data)
            except Exception as e:
                # 网络波动时可能会发送失败，忽略即可，避免阻塞
                SLog.w("DeviceManager", f"Stream forward error to {target_sn}: {e}")
        else:
            # [关键修复] 目标不存在时，通知发送端停止，防止无限 Log 刷屏
            # SLog.w("DeviceManager", f"Target {target_sn} offline. Stopping stream.") # 可降低日志级别
            # [修复刷屏] 只有没发过指令才发
            sender_sn = self._get_sn_by_ws(websocket)
            if sender_sn and sender_sn not in self._stop_command_sent:
                SLog.w("DeviceManager", f"Target {target_sn} offline. Stopping stream on {sender_sn}.")

                self._stop_command_sent.add(sender_sn)  # 标记已发送

                await self._safe_send(websocket, {
                    "type": "command",
                    "command": "stop_stream",
                    "params": {"reason": "target_not_found"}
                })
            # # 既然目标都没了，告诉发送者 (iOS) 别发了
            # sender_ws = websocket
            # await self._safe_send(sender_ws, {
            #     "type": "command",
            #     "command": "stop_stream",
            #     "params": {"reason": "target_not_found"}
            # })

            # 同时清理可能的残留会话记录
            sender_sn = self._get_sn_by_ws(websocket)
            if sender_sn in self.stream_sessions:
                del self.stream_sessions[sender_sn]

    # 修改点 4: 在 handle_message 分发逻辑中增加 control 类型支持
    # 你需要在 listen_loop 或 handle_message 的 if/else 里增加对 type="control" 的处理
    # 或者直接复用 command 通道，但我建议分开
    async def handle_control(self, websocket: WebSocket, data: dict):
        """
        处理控制信号 (Touch, Swipe, Home)
        前端发来: { "type": "control", "target_sn": "...", "data": { "action": "touch", "x": 100, "y": 200 } }
        """
        target_sn = data.get("device_sn") or data.get("target_sn")
        payload = data.get("data")

        target_ws = self.active_connections.get(target_sn)
        if not target_ws:
            return {"code": 404, "msg": "Device offline"}

        # [ClawNode] 直连节点：翻译成 ClawNode 方言后裸发，即发即回
        if target_sn in self.direct_nodes:
            from server.websocket.routers.wClawNode import translate_control_to_clawnode
            translated = translate_control_to_clawnode(payload or {})
            if translated is None:
                return {"code": 400, "msg": "action not supported by clawnode"}
            await self._safe_send(target_ws, translated)
            return {"code": 200, "msg": "forwarded (clawnode)"}

        # ↓↓↓ 原有 PC Node 逻辑，原封不动
        # 转发给设备 (iOS/Android)
        msg = {
            "type": "command",
            "command": "control",
            "params": payload
        }
        await self._safe_send(target_ws, msg)
        return {"code": 200, "msg": "ack"}

    # --- 数据库操作 ---
    def _update_device_status(self, sn: str, status: str):
        def _write():
            with SessionLocal() as db:
                device = db.query(MDevice).filter(MDevice.sn == sn).first()
                if device:
                    device.status = status
                    if status == "online":
                        now = datetime.now()
                        device.last_online_time = now.replace(microsecond=0)
                    db.commit()

        try:
            _with_db_retry(_write)
        except Exception as e:
            SLog.e("DeviceManager", f"DB Error update status: {e}")

    async def monitor_heartbeats(self):
        """后台任务：监控设备心跳，超时自动下线"""
        # 启动时先执行一次清理重复设备
        self._cleanup_duplicate_devices()
        SLog.i("DeviceManager", "Starting heartbeat monitor...")
        while True:
            await asyncio.sleep(30) # 每30秒检查一次
            def _sweep():
                with SessionLocal() as db:
                    timeout_threshold = datetime.now() - timedelta(seconds=60)
                    devices = db.query(MDevice).filter(
                        MDevice.status == "online",
                        MDevice.last_online_time < timeout_threshold,
                    ).all()

                    for dev in devices:
                        SLog.w("DeviceManager", f"Device {dev.sn} heartbeat timeout. Marking offline.")
                        dev.status = "offline"
                        if dev.sn in self.active_connections:
                            del self.active_connections[dev.sn]

                    if devices:
                        db.commit()

            try:
                _with_db_retry(_sweep)
            except Exception as e:
                SLog.e("DeviceManager", f"Heartbeat monitor error: {e}")

    def _cleanup_duplicate_devices(self):
        """清理同名(model)的重复设备，保留最近上线的一个"""
        try:
            with SessionLocal() as db:
                # 获取所有 PC 类型的设备
                devices = db.query(MDevice).filter(MDevice.device_type == "pc").all()
                
                # 按 model 分组
                grouped = {}
                for dev in devices:
                    if not dev.model: continue
                    if dev.model not in grouped:
                        grouped[dev.model] = []
                    grouped[dev.model].append(dev)
                
                for model, devs in grouped.items():
                    if len(devs) > 1:
                        # 按最后上线时间降序排序 (None 视为最旧)
                        devs.sort(key=lambda x: x.last_online_time or datetime.min, reverse=True)
                        
                        # 保留第一个 (最新的)，删除其余的
                        keep = devs[0]
                        to_delete = devs[1:]
                        
                        # 修复：如果保留的最新记录没有密码，尝试从即将删除的旧记录中继承密码
                        # 这样可以防止设备 SN 变化后，用户之前设置的锁屏密码丢失
                        if not keep.password:
                            for old_dev in to_delete:
                                if old_dev.password:
                                    keep.password = old_dev.password
                                    SLog.i("DeviceManager", f"Inherited password from duplicate {old_dev.sn} to {keep.sn}")
                                    break

                        SLog.i("DeviceManager", f"Cleaning up duplicates for model '{model}'. Keeping {keep.sn}, deleting {len(to_delete)} others.")
                        
                        for d in to_delete:
                            if d.sn in self.active_connections:
                                del self.active_connections[d.sn]
                            db.delete(d)
                
                db.commit()
        except Exception as e:
            SLog.e("DeviceManager", f"Error cleaning duplicates: {e}")

    def _get_sn_by_ws(self, websocket: WebSocket):
        """通过 WebSocket 连接反查设备 SN"""
        for sn, ws in self.active_connections.items():
            if ws == websocket:
                return sn
        return "unknown"

    async def handle_client_log(self, websocket: WebSocket, data: dict):
        """处理客户端回传的日志"""
        # data: {run_id, flow_id, node_id, level, tag, message}
        # 修复: 获取正确的设备 SN，而不是使用 node_id
        device_sn = self._get_sn_by_ws(websocket)
        self._save_log(device_sn, "client", "log", json.dumps(data))
        
        # 写入 WorkflowLog 表 (替代客户端直接写库)
        try:
            with LogSessionLocal() as db:
                log = WorkflowLog(
                    run_id=data.get("run_id"),
                    flow_id=data.get("flow_id"),
                    node_id=data.get("node_id"),
                    level=data.get("level"),
                    tag=data.get("tag"),
                    message=data.get("message"),
                    created_at=datetime.now()
                )
                db.add(log)
                db.commit()
        except Exception as e:
            SLog.e("DeviceManager", f"DB Error save workflow log: {e}")

    async def handle_crawl_complete(self, websocket: WebSocket, data: dict):
        from server.services.crawl_job_manager import complete

        req_id = data.get("req_id")
        payload = data.get("payload") or data
        if req_id:
            complete(req_id, payload if isinstance(payload, dict) and "code" in payload else {"code": 200, "data": payload})
        SLog.i("DeviceManager", f"Crawl complete req_id={req_id}")

    async def handle_task_report(self, websocket: WebSocket, data: dict):
        """处理客户端回传的任务报告"""
        # 更新服务端的全局 report 变量，以便前端轮询时能获取到结果
        from script.mTask import report
        if isinstance(data, dict):
            report.update(data)
            SLog.i("DeviceManager", f"Received task report with {len(data)} items")

    def _register_device(self, sn: str, info: dict):
        from server.services.device_service import is_valid_sn

        if not is_valid_sn(sn):
            SLog.d("DeviceManager", f"Skip invalid device registration sn={sn!r}")
            return
        try:
            with SessionLocal() as db:
                device = db.query(MDevice).filter(MDevice.sn == sn).first()
                if not device:
                    device = MDevice(sn=sn)
                    db.add(device)
                
                # 更新字段
                if info.get("type"):
                    device.device_type = info.get("type")
                if info.get("model"):
                    device.model = info.get("model")
                if info.get("ip"):
                    device.ip_address = info.get("ip")
                if info.get("mac"):
                    device.mac_address = info.get("mac")
                if info.get("os_version"):
                    device.os_version = info.get("os_version")
                if info.get("resolution"):
                    device.resolution = info.get("resolution")
                
                # 🚀 新增字段: 角色与密码
                if info.get("role"):
                    device.role = info.get("role")
                elif not device.role:
                    device.role = "node"

                # 修复: 仅当 info 中包含有效密码时才更新，防止设备重连时覆盖数据库中已保存的密码
                if info.get("password"):
                    device.password = info.get("password")
                device.status = "online"
                # 优化：直接去除微秒
                now = datetime.now()
                device.last_online_time = now.replace(microsecond=0)
                db.commit()
                SLog.i("DeviceManager", f"Device registered/updated: {sn}")
        except Exception as e:
            SLog.e("DeviceManager", f"DB Error register: {e}")

    def _save_log(self, sn: str, direction: str, msg_type: str, content: str):
        try:
            with SessionLocal() as db:
                log = MDeviceLog(sn=sn, direction=direction, type=msg_type, content=content)
                db.add(log)
                db.commit()
        except Exception as e:
            SLog.e("DeviceManager", f"DB Error save log: {e}")
