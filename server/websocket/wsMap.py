# 动作分发映射
from server.websocket.wsFile import handle_upload, handle_get_file
from server.websocket.device_manager import DeviceManager

device_manager = DeviceManager()

HANDLERS = {
    "upload": handle_upload,
    "get_file": handle_get_file,
    "register": device_manager.register,
    "heartbeat": device_manager.heartbeat,
    "disconnect": device_manager.disconnect
}