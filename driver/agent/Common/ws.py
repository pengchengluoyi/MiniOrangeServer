# driver/brain/common/graph_loader.py
from driver.agent.Common.bridge import ServerBridge

class WS:
    @staticmethod
    def fetch_get_file(filename):
        return ServerBridge.query("get_file", {"name": filename})

    @staticmethod
    def fetch_upload_file(filename, b64_str):
        return ServerBridge.query("upload", {"name": filename, "content": b64_str}, timeout=60)

    @staticmethod
    def fetch_app_graph(flow_id):
        return ServerBridge.query("get_app_graph", {"flow_id": flow_id})

    @staticmethod
    def get_workflow_detail(flow_id):
        return ServerBridge.query("get_workflow_detail", {"flow_id": flow_id})

    @staticmethod
    def fetch_world_model():
        return ServerBridge.query("get_world_model")

    @staticmethod
    def get_device_password(sn):
        """与 ws_handlers.handle_get_device_password 一致，统一为 ``{"password": "..."}``。"""
        res = ServerBridge.query("get_device_password", {"sn": sn})
        if not res:
            return None
        if isinstance(res.get("password"), str) or res.get("password") is None:
            return {"password": res.get("password")}
        data = res.get("data")
        if isinstance(data, dict):
            return {"password": data.get("password")}
        return None