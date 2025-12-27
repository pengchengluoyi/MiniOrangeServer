import json
import time
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from script.log import SLog
from server.websocket.wsMap import HANDLERS

router = APIRouter()

TAG = "rWebSocket"


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    SLog.i(TAG, "Client connected")
    try:
        while True:
            text_data = await websocket.receive_text()
            try:
                payload = json.loads(text_data)
            except json.JSONDecodeError:
                await websocket.send_json({"code": 400, "msg": "Invalid JSON format"})
                continue
                
            action = payload.get("action")
            req_id = payload.get("req_id")
            data = payload.get("data", {})
            
            # 构造基础响应
            response = {
                "action": action,
                "req_id": req_id,
                "timestamp": time.time()
            }
            
            if action in HANDLERS:
                # 执行对应的处理函数
                result = await HANDLERS[action](data)
                response.update(result)
            else:
                response.update({"code": 404, "msg": f"Action '{action}' not supported"})
            
            # 发送响应
            await websocket.send_text(json.dumps(response))
            
    except WebSocketDisconnect:
        SLog.i(TAG, "Client disconnected")
    except Exception as e:
        SLog.e(TAG, f"WebSocket error: {e}")