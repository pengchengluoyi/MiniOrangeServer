# 动作分发映射
from server.websocket.wsFile import handle_upload, handle_get_file
from server.websocket.device_manager import DeviceManager
from server.websocket.ws_handlers import handle_run_workflow, handle_get_device_list, handle_get_component, handle_get_device_password, handle_get_world_model, handle_get_app_graph, handle_ask_local_ai, handle_get_workflow_detail, handle_sync_timeline, handle_get_timeline, handle_get_timeline_list
device_manager = DeviceManager()

HANDLERS = {
    "upload": handle_upload,
    "get_file": handle_get_file,
    "run_workflow": handle_run_workflow,
    "get_device_list": handle_get_device_list,
    "get_component": handle_get_component,
    "get_device_password": handle_get_device_password,
    "register": device_manager.register,
    "heartbeat": device_manager.heartbeat,
    "disconnect": device_manager.disconnect,
    "client_log": device_manager.handle_client_log,
    "task_report": device_manager.handle_task_report,
    "ask_local_ai": handle_ask_local_ai,
    "get_world_model": handle_get_world_model,
    "get_app_graph": handle_get_app_graph,
    "get_workflow_detail": handle_get_workflow_detail,
    "sync_timeline": handle_sync_timeline,
    "get_timeline": handle_get_timeline,
    "get_timeline_list": handle_get_timeline_list,
}