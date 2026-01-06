import json
import time
import asyncio
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from script.log import SLog
from server.websocket.wsMap import HANDLERS


router = APIRouter()

TAG = "rWebSocket"


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    SLog.i(TAG, "Client connected")
    
    # 发送锁，防止并发任务同时写入 WebSocket 导致协议错误
    send_lock = asyncio.Lock()

    async def process_message(payload: dict):
        """独立处理每条消息的任务函数"""
        action = payload.get("action")
        req_id = payload.get("req_id")
        data = payload.get("data", {})
        
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
            text_data = await websocket.receive_text()
            try:
                payload = json.loads(text_data)
            except json.JSONDecodeError:
                async with send_lock:
                    await websocket.send_json({"code": 400, "msg": "Invalid JSON format"})
                continue
            
            # 使用 asyncio.create_task 将消息处理放入后台
            # 这样即使 get_file 耗时，也不会阻塞后续的心跳或其他请求
            asyncio.create_task(process_message(payload))
            
    except WebSocketDisconnect:
        SLog.i(TAG, "Client disconnected")
        if "disconnect" in HANDLERS:
            await HANDLERS["disconnect"](websocket, {})
    except Exception as e:
        SLog.e(TAG, f"WebSocket error: {e}")