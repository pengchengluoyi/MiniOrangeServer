# 动作分发映射
from server.websocket.wsFile import handle_upload, handle_get_file

HANDLERS = {
    "upload": handle_upload,
    "get_file": handle_get_file
}