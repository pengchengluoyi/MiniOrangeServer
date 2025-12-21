from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from server.manager import manager

router = APIRouter(prefix="/ws", tags=["websocket"])

@router.websocket("")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # 保持连接，接收客户端消息（如果有的话）
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket Error: {e}")
        manager.disconnect(websocket)

# [新增] 内部广播接口：接收 HTTP 请求 -> 转为 WebSocket 广播
@router.post("/internal/broadcast")
async def internal_broadcast(message: dict):
    await manager.broadcast(message)
    return {"status": "ok"}