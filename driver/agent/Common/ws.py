# driver/brain/common/graph_loader.py
from driver.agent.Common.bridge import ServerBridge

class WS:
    @staticmethod
    def fetch_app_graph(flow_id):
        return ServerBridge.query("get_app_graph", {"flow_id": flow_id})

    @staticmethod
    def get_workflow_detail(flow_id):
        return ServerBridge.query("get_workflow_detail", {"flow_id": flow_id})

    @staticmethod
    def fetch_world_model():
        return ServerBridge.query("get_world_model")