import json
import time
import asyncio
import copy
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from script.log import SLog
from server.websocket.wsMap import HANDLERS
from server.websocket.device_manager import DeviceManager
from server.core.security import SecurityManager


router = APIRouter()

TAG = "rWebSocket"

_B64_KEYS = frozenset({"base64_image", "base64", "image_base64", "screenshot"})


def _truncate_b64(value) -> str:
    if value is None:
        return "<empty>"
    text = str(value)
    if len(text) <= 64:
        return text
    return f"<base64 {len(text)} chars>"


def _sanitize_ws_log_payload(payload: dict) -> dict:
    """日志中省略截图等大字段，避免终端被 base64 淹没。"""
    if not isinstance(payload, dict):
        return payload
    out = copy.deepcopy(payload)
    for key in _B64_KEYS:
        if key in out:
            out[key] = _truncate_b64(payload.get(key))
    data = out.get("data")
    if isinstance(data, dict):
        for key in _B64_KEYS:
            if key in data:
                data[key] = _truncate_b64(payload["data"].get(key))
    return out

@router.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(None),
    pairing: int = Query(0),
    node_sn: str = Query(None),
):
    # 注意：monitor_heartbeats 建议在 main.py 的 lifespan 中启动，避免每个连接都启动

    # [安全校验] 检查 Access Token
    server_token = SecurityManager.get_token()
    dm = DeviceManager()
    SLog.i(TAG, f"⚡ [WS] New connection attempt pairing={pairing} node_sn={node_sn} token={'set' if token else 'none'}")

    if pairing and node_sn:
        pending = dm.pending_pairings.get(node_sn)
        if pending and pending.get("expires", 0) > time.time():
            SLog.i(TAG, f"⚡ [Pairing Mode] Accept sn={node_sn} ws_url={pending.get('ws_url')} gateway={pending.get('gateway_id')}")
            await websocket.accept()
            dm.active_connections[node_sn] = websocket
            await dm._send_pair_config(websocket, node_sn, pending)
            dm.pending_pairings.pop(node_sn, None)
            SLog.i(TAG, f"⚡ [Pairing Mode] PAIR_CONFIG delivered sn={node_sn}")
        else:
            SLog.w(TAG, f"⛔ Pairing rejected sn={node_sn} pending={bool(pending)} expired={pending.get('expires', 0) <= time.time() if pending else 'n/a'}")
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
    elif server_token is None:
        # Case 1: 初始化模式 (Setup Mode)
        SLog.i(TAG, "⚠️ [Setup Mode] No server token configured. Accepting connection.")
        await websocket.accept()
        dm.observers.add(websocket)
    elif token == server_token:
        # Case 2: 正常鉴权通过
        await websocket.accept()
        dm.observers.add(websocket)
    else:
        user_ok = False
        if token:
            try:
                from server.services.auth_service import status as auth_status
                user_ok = bool(auth_status(token).get("logged_in"))
            except Exception:
                user_ok = False
        if user_ok:
            SLog.i(TAG, "⚡ [WS] Accepted logged-in web session")
            await websocket.accept()
            dm.observers.add(websocket)
        else:
            SLog.w(TAG, f"⛔ Connection rejected. Server Token: {str(server_token)[:6]}... Client Sent: {token}")
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
    
    # 发送锁，防止并发任务同时写入 WebSocket 导致协议错误
    send_lock = asyncio.Lock()

    async def process_message(payload: dict):
        """独立处理每条消息的任务函数"""
        # [ClawNode] 兼容：ClawNode 的结果帧用 type 字段（SCREENSHOT_RESULT 等），
        # 而非 action。这里做非破坏性回退——已有消息均带 action，or 分支不触发。
        action = payload.get("action") or payload.get("type")
        req_id = payload.get("req_id") or payload.get("trace_id")
        if action != "heartbeat" and action != "upload":
            SLog.i(TAG, _sanitize_ws_log_payload(payload))

        data = payload.get("data", {}).copy()

        # 2. 找出“其他”参数
        # 排除 action, req_id, 以及原本的 "data" key 本身
        extra_params = {
            k: v for k, v in payload.items()
            if k not in ["action", "req_id", "data"]
        }

        # 3. 将这些额外参数合并进 data
        data.update(extra_params)
        
        response = {
            "action": action,
            "req_id": req_id,
            "timestamp": time.time()
        }
        
        try:
            # 🔥 [Fix] 动态获取当前 Token，而不是使用连接时的 server_token 快照
            # 因为 join_cluster 后 Token 会变化，但 WebSocket 连接保持不变
            current_token = SecurityManager.get_token()
            
            if current_token is None:
                client = getattr(websocket.app.state, "device_client", None)
                standalone = not client or getattr(client, "role", "") in ("gateway", "server", "")
                if not standalone:
                    allowed_actions = ["get_server_info", "join_cluster", "get_node_status", "register", "heartbeat", "register_clawnode"]
                    if action not in allowed_actions:
                        response.update({"code": 403, "msg": "节点未绑定集群"})
                        async with send_lock:
                            await websocket.send_text(json.dumps(response))
                        return
            
            # 🔥 [新增] 节点离线检测 (Node Mode Offline Check)
            # 如果本机是 Node 模式且与集群断开，拒绝大部分业务请求，并通知移动端
            client = getattr(websocket.app.state, "device_client", None)
            is_node_offline = False
            if client and client.role == 'node':
                # 检查 DeviceClient 内部的 WebSocket 连接状态
                if not client.websocket or not getattr(client.websocket, "open", False):
                    is_node_offline = True
            
            # 1. 所有接口都返回当前节点状态，通知移动端
            response["node_status"] = "offline" if is_node_offline else "online"

            # 2. [新增接口] 专门用于查询离线状态
            if action == "get_offline_status":
                response.update({"code": 200, "data": {"is_offline": is_node_offline}})
                async with send_lock:
                    await websocket.send_text(json.dumps(response))
                return

            if action in HANDLERS:
                handler = HANDLERS[action]
                result = None

                # [ClawNode] 同步 handler（如 copilot/chat）可能内部同步阻塞等待
                # RemoteEngine 经 WS 往返；必须 offload 到线程池，否则阻塞 event loop
                # 导致收不到设备回传 → 死锁。用 asyncio.to_thread（带 copy_context，
                # 传播 regression_run_ctx 等 ContextVar）。async handler 仍走 await。
                is_async = asyncio.iscoroutinefunction(handler)

                # 兼容性调用：尝试传入 websocket，如果失败则回退到仅传入 data
                try:
                    if is_async:
                        result = await handler(websocket, data)
                    else:
                        result = await asyncio.to_thread(handler, websocket, data)
                except TypeError as e:
                    # 捕获参数数量不匹配的错误 (例如 handle_get_file 只接受 data)
                    if "positional argument" in str(e):
                        if is_async:
                            result = await handler(data)
                        else:
                            result = await asyncio.to_thread(handler, data)
                    else:
                        raise e

                if result:
                    response.update(result)
            else:
                response.update({"code": 404, "msg": f"Action '{action}' not supported"})
            
            # 并发环境下，发送数据需要加锁
            async with send_lock:
                await websocket.send_text(json.dumps(response))
                
        except WebSocketDisconnect:
            SLog.d(TAG, f"client disconnected while processing {action}")
        except Exception as e:
            # 连接已关闭时的发送失败是良性竞态（如设备收到 PAIR_CONFIG 后立即断开 WS，
            # 服务端此时才回 ack）→ 降级为 debug，避免当作真错误刷屏。
            from starlette.websockets import WebSocketState

            disconnected = (
                getattr(websocket, "application_state", None) == WebSocketState.DISCONNECTED
                or getattr(websocket, "client_state", None) == WebSocketState.DISCONNECTED
            )
            if disconnected:
                SLog.d(TAG, f"skip response for {action}: connection closed ({type(e).__name__})")
                return
            SLog.e(TAG, f"Error processing {action}: {e}")
            # 发生异常时尝试返回错误信息，防止前端超时
            try:
                response.update({"code": 500, "msg": str(e)})
                async with send_lock:
                    await websocket.send_text(json.dumps(response))
            except Exception:
                pass

    try:
        while True:
            # [修改] 使用 receive() 接收原始消息，兼容 Text 和 Bytes
            message = await websocket.receive()
            
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect
            
            # 1. 处理文本消息 (JSON 信令)
            if "text" in message and message["text"]:
                try:
                    payload = json.loads(message["text"])
                    asyncio.create_task(process_message(payload))
                except json.JSONDecodeError:
                    pass
            
            # 2. 处理二进制消息 (文件流转发)
            elif "bytes" in message and message["bytes"]:
                # 🔥 转发给 DeviceManager 统一处理 (支持投屏和文件传输)
                await DeviceManager().handle_binary_stream(websocket, message["bytes"])
            
    except WebSocketDisconnect:
        SLog.i(TAG, "Client disconnected")
        dm.observers.discard(websocket)
        if "disconnect" in HANDLERS:
            await HANDLERS["disconnect"](websocket, {})
    except Exception as e:
        SLog.e(TAG, f"WebSocket error: {e}")