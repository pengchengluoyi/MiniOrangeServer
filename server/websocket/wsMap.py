# 动作分发映射
from server.websocket.wsFile import handle_upload, handle_get_file
from server.websocket.device_manager import DeviceManager
from server.websocket.ws_handlers import handle_run_workflow, handle_get_device_list, handle_get_component, \
    handle_get_device_password, handle_set_device_password, handle_get_world_model, handle_get_app_graph, handle_ask_local_ai, \
    handle_get_workflow_detail, handle_sync_timeline, handle_get_timeline, handle_get_timeline_list
from server.websocket.routers.wAppGraph import (
    handle_app_graph_list,
    handle_app_graph_create,
    handle_app_graph_detail,
    handle_save_node_detail,
    handle_sync_layout,
    handle_add_empty_node,
    handle_sop_create,
    handle_sop_update,
    handle_sop_delete,
    handle_match_solution,
    handle_train_skeleton
)

device_manager = DeviceManager()

HANDLERS = {
    "upload": handle_upload,
    "get_file": handle_get_file,
    "run_workflow": handle_run_workflow,
    "get_device_list": handle_get_device_list,
    "get_component": handle_get_component,
    "get_device_password": handle_get_device_password,
    "set_device_password": handle_set_device_password,
    "register": device_manager.register,
    "heartbeat": device_manager.heartbeat,
    "disconnect": device_manager.disconnect,
    "client_log": device_manager.handle_client_log,
    "task_report": device_manager.handle_task_report,
    "list_dir": device_manager.handle_list_dir,
    "p2p_signal": device_manager.handle_p2p_signal,
    "dir_list": device_manager.handle_dir_list,
    "transfer_progress": device_manager.handle_transfer_progress,
    "ask_local_ai": handle_ask_local_ai,
    "get_world_model": handle_get_world_model,
    "get_app_graph": handle_get_app_graph,
    "get_workflow_detail": handle_get_workflow_detail,
    "sync_timeline": handle_sync_timeline,
    "get_timeline": handle_get_timeline,
    "get_timeline_list": handle_get_timeline_list,
    "app_graph/list": handle_app_graph_list,
    "app_graph/create": handle_app_graph_create,
    "app_graph/detail": handle_app_graph_detail,
    "app_graph/save_node_detail": handle_save_node_detail,
    "app_graph/sync_layout": handle_sync_layout,
    "app_graph/add_empty_node": handle_add_empty_node,
    "sop/create": handle_sop_create,
    "sop/update": handle_sop_update,
    "sop/delete": handle_sop_delete,
    "app_graph/match_solution": handle_match_solution,
    "app_graph/train_skeleton": handle_train_skeleton,
    "start_stream": device_manager.handle_start_stream,
    "stop_stream": device_manager.handle_stop_stream,
    "device/control": device_manager.handle_control,
}
