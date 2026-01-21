import json
import time
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from script.log import SLog
from server.websocket.wsMap import HANDLERS
from server.websocket.device_manager import DeviceManager
from server.core.security import SecurityManager


router = APIRouter()

TAG = "rWebSocket"

@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(None)):
    # 注意：monitor_heartbeats 建议在 main.py 的 lifespan 中启动，避免每个连接都启动

    # [安全校验] 检查 Access Token
    server_token = SecurityManager.get_token()
    if server_token and token != server_token:
        SLog.w(TAG, f"Connection rejected: Invalid token '{token}'")
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    SLog.i(TAG, "Client connected")
    
    # 发送锁，防止并发任务同时写入 WebSocket 导致协议错误
    send_lock = asyncio.Lock()

    async def process_message(payload: dict):
        """独立处理每条消息的任务函数"""
        action = payload.get("action")
        req_id = payload.get("req_id")
        data = payload.get("data", {})
        if action != "heartbeat" and action != "upload":
            SLog.i(TAG, payload)
        
        response = {
            "action": action,
            "req_id": req_id,
            "timestamp": time.time()
        }
        
        try:
            if action in HANDLERS:
                handler = HANDLERS[action]
                result = None
                
                # 兼容性调用：尝试传入 websocket，如果失败则回退到仅传入 data
                try:
                    result = await handler(websocket, data)
                except TypeError as e:
                    # 捕获参数数量不匹配的错误 (例如 handle_get_file 只接受 data)
                    if "positional argument" in str(e):
                        result = await handler(data)
                    else:
                        raise e
                
                if result:
                    response.update(result)
            else:
                response.update({"code": 404, "msg": f"Action '{action}' not supported"})
            
            # 并发环境下，发送数据需要加锁
            async with send_lock:
                await websocket.send_text(json.dumps(response))
                
        except Exception as e:
            SLog.e(TAG, f"Error processing {action}: {e}")
            # 发生异常时尝试返回错误信息，防止前端超时
            try:
                response.update({"code": 500, "msg": str(e)})
                async with send_lock:
                    await websocket.send_text(json.dumps(response))
            except:
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
        if "disconnect" in HANDLERS:
            await HANDLERS["disconnect"](websocket, {})
    except Exception as e:
        SLog.e(TAG, f"WebSocket error: {e}")