from typing import Dict
from fastapi import WebSocket

class ConnectionManager:
    def __init__(self):
        # 存放所有活跃的 WebSocket 连接: {client_id: websocket}
        print("--- [WebSocket] Connection Manager Initialized ---")
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        # 记录连接，如果 ID 重复则覆盖（或者你可以选择拒绝）
        self.active_connections[client_id] = websocket
        print(f"Client connected: {client_id}")

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            print(f"Client disconnected: {client_id}")

    async def send_personal_message(self, message: dict, client_id: str):
        """发送给指定用户"""
        if client_id in self.active_connections:
            await self.active_connections[client_id].send_json(message)

    async def broadcast(self, message: dict):
        """广播给所有用户"""
        for connection in self.active_connections.values():
            await connection.send_json(message)

manager = ConnectionManager()