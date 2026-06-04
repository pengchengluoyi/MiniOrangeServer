# memory/long_term.py
import builtins
import threading

from driver.agent.Common.ws import WS
from script.log import SLog, current_flow_id


def _resolve_dot_path(data, path: str):
    if not path or data is None:
        return None
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class LongTermMemory:
    def __init__(self):
        self._context = {
            "app": {},
            "graph": {"variables": {}},
            "sop": {"variables": {}},
            "workflow": {"variables": {}},
            "device": {},
            "world": {},
        }
        self.app_graph = {}
        self.world_model = {}
        self.is_loaded = False

    def load_async(self):
        flow_id = current_flow_id.get()
        sn = getattr(builtins, "TARGET_DEVICE_SN", None)
        env_profile = getattr(builtins, "RUN_ENV_PROFILE", None)
        t = threading.Thread(target=self._fetch_bg, args=(flow_id, sn, env_profile))
        t.daemon = True
        t.start()

    def _fetch_bg(self, flow_id, sn, env_profile=None):
        try:
            res = WS.fetch_run_context(flow_id, sn, env_profile)
            if isinstance(res, dict):
                payload = res.get("data") if isinstance(res.get("data"), dict) else res
                ctx = payload.get("context")
                if isinstance(ctx, dict):
                    self._context = ctx
                ag = payload.get("app_graph")
                if isinstance(ag, dict):
                    self.app_graph = ag
            self.world_model = self._context.get("world") or {}
            self.is_loaded = True
            SLog.i("Memory", "✅ 长期记忆(配置/图谱) 加载完毕")
        except Exception as e:
            SLog.w("Memory", f"❌ 长期记忆加载失败: {e}")

    def _infer_device_type(self) -> str:
        import builtins

        sn = getattr(builtins, "TARGET_DEVICE_SN", None)
        if sn:
            try:
                from server.services.device_service import DeviceService

                dev = DeviceService.get_by_sn(str(sn))
                if dev and dev.device_type:
                    return str(dev.device_type).strip().lower()
            except Exception:
                pass
        dt = _resolve_dot_path(self._context.get("device", {}), "device_type")
        return str(dt).strip().lower() if dt else "android"

    def _resolve_mobile_target(self):
        app = self._context.get("app") or {}
        if self._infer_device_type() == "ios":
            return _resolve_dot_path(app, "ios.bundle")
        return _resolve_dot_path(app, "android.package")

    def get(self, path: str):
        if not path:
            return None
        path = path.strip()
        if path.startswith("app."):
            sub = path[4:]
            if sub in ("mobile.target", "mobile.package"):
                return self._resolve_mobile_target()
            return _resolve_dot_path(self._context.get("app", {}), sub)
        if path.startswith("graph."):
            return _resolve_dot_path(
                self._context.get("graph", {}).get("variables", {}), path[6:]
            )
        if path.startswith("sop."):
            return _resolve_dot_path(
                self._context.get("sop", {}).get("variables", {}), path[4:]
            )
        if path.startswith("workflow."):
            return _resolve_dot_path(
                self._context.get("workflow", {}).get("variables", {}), path[9:]
            )
        if path.startswith("device."):
            return _resolve_dot_path(self._context.get("device", {}), path[7:])
        if path.startswith("world."):
            return _resolve_dot_path(self._context.get("world", {}), path[6:])
        world = self._context.get("world", {})
        if isinstance(world, dict) and path in world:
            return world[path]
        return None

    def get_config(self, key):
        return self.get(key)
