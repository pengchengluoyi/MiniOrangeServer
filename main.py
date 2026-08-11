import os
import sys
import json
import asyncio
import platform
import socket
import time
import multiprocessing
import psutil
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from zeroconf import IPVersion, ServiceInfo, Zeroconf, ServiceBrowser
from zeroconf.asyncio import AsyncZeroconf
from pydantic import BaseModel
from script.log import SLog

TAG = "Main"
# 🚀 强制 stdout 行缓冲
sys.stdout.reconfigure(line_buffering=True)

BOOT_START_TIME = time.time()

# -------------------------------------------------------------
# 1. 模块导入 (移除这里的 SLog.i，减少子进程噪音)
# -------------------------------------------------------------
from server.core.migration import run_auto_migration
from server.core.database import engine, Base, APP_DATA_DIR
from server.core.security import SecurityManager
from server.core.log_database import log_engine, LogBase
from server.routers import rWorkflow as wf_router
from server.routers import rLog as log_router
from server.routers import rFile as file_router
from server.routers import rAppGraph as app_graph_router
from server.websocket import rWebsocket as websocket_router
from server.routers import rProject as project_router
from server.routers import rTask as task_router
from server.routers import rWorkflowRun as workflowRun_router
from server.routers import rDevice as device_router
from server.routers import rAbility as ability_router
from server.routers import rSchedule as schedule_router
from server.routers import rFeishuRegression as feishu_router
from server.routers import rCaseRunner as case_runner_router
from server.routers import rAppAutomation as app_automation_router
from server.routers import rSettings as settings_router
from server.routers import rClawNode as clawnode_router
from server.routers import rHitl as hitl_router
import server.models.app_regression_run  # noqa: F401 — register ORM table
import server.models.app_icon_target  # noqa: F401 — register ORM table
import server.models.case_baseline  # noqa: F401 — Step 6: case baseline / run trace

# Windows COM Init (仅在 Windows 下执行)
if platform.system() == "Windows":
    try:
        import comtypes
        import comtypes.client

        try:
            import comtypes.gen
        except ImportError:
            import types

            gen = types.ModuleType("comtypes.gen")
            sys.modules["comtypes.gen"] = gen
            comtypes.gen = gen

        # 简化路径逻辑
        gen_path = os.path.abspath(os.path.join('.', "comtypes_cache"))
        if not os.path.exists(gen_path): os.makedirs(gen_path)
        comtypes.client._generate_cache = gen_path
        comtypes.gen.__path__ = [gen_path]
    except Exception:
        pass

# 🔥 路径配置
BASE_DIR = APP_DATA_DIR
UPLOAD_DIR = os.path.join(APP_DATA_DIR, "uploads")
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)


# -------------------------------------------------------------
# 2. 辅助函数
# -------------------------------------------------------------
def configure_proxy_bypass():
    """配置环境变量以绕过系统代理，防止局域网连接被拦截"""

    # 1. 强制移除代理设置 (新增代码)
    # 这样底层库就不会去尝试加载 python-socks，也不会尝试连接代理服务器
    keys_to_remove = ["http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"]
    for key in keys_to_remove:
        if key in os.environ:
            del os.environ[key]
            SLog.i(TAG, f"--- [Network] Unset system proxy var: {key} ---")

    # 2. 保留原有的 no_proxy 逻辑 (作为双重保险)
    bypass_hosts = "localhost,127.0.0.1,::1,miniorange.local,0.0.0.0,100.*"

    current_no_proxy = os.environ.get("no_proxy", "")
    if current_no_proxy:
        os.environ["no_proxy"] = f"{current_no_proxy},{bypass_hosts}"
    else:
        os.environ["no_proxy"] = bypass_hosts

    # 同步设置大写变量
    os.environ["NO_PROXY"] = os.environ["no_proxy"]

def get_local_ip():
    """获取本机真实局域网 IP (过滤代理虚拟网卡)"""
    lan_ip = None
    try:
        # 方案 A: 使用 psutil 遍历网卡，优先匹配局域网段
        for interface, snics in psutil.net_if_addrs().items():
            for snic in snics:
                if snic.family == socket.AF_INET:
                    ip = snic.address
                    if ip == "127.0.0.1": continue
                    # 过滤常见的代理虚拟 IP (Clash 等常使用 198.18.x.x)
                    if ip.startswith("198.18."): continue
                    
                    # [新增] 优先匹配 Tailscale IP
                    if ip.startswith("100."):
                        return ip

                    # 优先返回常见的局域网段
                    if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                        if not lan_ip:
                            lan_ip = ip
        
        if lan_ip:
            return lan_ip

        # 方案 B: 回退到 socket 方式
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        # 如果 socket 拿到的也是虚拟 IP，则降级为 localhost
        if ip.startswith("198.18."):
            return "127.0.0.1"
        return ip
    except:
        return "127.0.0.1"


async def register_mdns(port):
    from server.core.gateway_beacon import register_gateway_beacons
    return await register_gateway_beacons(port=port, ws_path="/ws")


# -------------------------------------------------------------
# 3. LifeSpan 生命周期管理
# -------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 数据库初始化
    run_auto_migration()
    # 安全配置初始化
    SecurityManager.load()
    
    Base.metadata.create_all(bind=engine)
    LogBase.metadata.create_all(bind=log_engine)

    # 配置代理绕过
    configure_proxy_bypass()

    # 启动后台服务
    beacon_handle = await register_mdns(10104)
    app.state.gateway_beacon = beacon_handle

    from server.websocket.device_manager import DeviceManager
    # [ClawNode] 捕获主 event loop，供 RemoteEngine 从 worker 线程提交 WS 发送
    DeviceManager().loop = asyncio.get_running_loop()
    asyncio.create_task(DeviceManager().monitor_heartbeats())

    from server.core.scheduler import SchedulerService
    SchedulerService().start()

    # 启动 Client (服务端内置 Client)
    from driver.client import DeviceClient, DEVICE_SN
    
    # 直接使用 SecurityManager 的配置 (lifespan 开头已 load)
    config = SecurityManager._config
    # 获取候选列表
    candidate_urls = config.get("candidate_urls", [])

    # 🔥🔥🔥 [修复] 强制类型检查：如果是字符串，转换成列表 🔥🔥🔥
    if isinstance(candidate_urls, str):
        candidate_urls = [candidate_urls]

    if not candidate_urls and config.get("target_url"):
        candidate_urls = [config.get("target_url")]

    client = None

    if candidate_urls:
        SLog.i(TAG, f"--- [System] Node Mode Active. Candidates: {len(candidate_urls)} ---")

        from driver.client import NetworkSelector
        best_url = await NetworkSelector.select_best_url(candidate_urls)

        if best_url:
            SLog.i(TAG, f"--- [System] Selected Best Relay: {best_url} ---")
            client = DeviceClient(best_url, DEVICE_SN, role="node")
        else:
            SLog.i(TAG, "--- [Error] All candidates unreachable, retrying later... ---")
            # 可以做一个定时重试的任务
    else:
        # Server 模式逻辑不变
        client = DeviceClient(["ws://127.0.0.1:10104/ws"], DEVICE_SN, role="client", token=SecurityManager.get_token())
    # 🔥 [关键步骤] 将 client 实例挂载到 app.state
    # 这样后续的 API 路由就可以通过 request.app.state.device_client 访问到它了
    app.state.device_client = client

    # 🔥 [修改] 将 task 也挂载到 app.state，以便在不重启进程的情况下取消它
    app.state.device_client_task = None
    if client:
        app.state.device_client_task = asyncio.create_task(client.start())

    # CLIP 启动预加载（首次会下载权重，须完成后再接请求，避免用例执行线程卡 50s+）
    SLog.i(TAG, "--- [CLIP] warmup starting... ---")
    try:
        from server.core.vision.clip_service import warmup_clip_service

        clip_status = await asyncio.to_thread(warmup_clip_service)
        if clip_status.get("ok"):
            SLog.i(
                TAG,
                f"--- [CLIP] ready model={clip_status.get('model')} dim={clip_status.get('dim')} ---",
            )
        else:
            SLog.w(TAG, f"--- [CLIP] unavailable: {clip_status.get('reason')} ---")
    except Exception as e:
        SLog.w(TAG, f"--- [CLIP] warmup failed: {e} ---")

    # HITL Transport: 把 FastAPI 主 event loop 注入到 WebSocketHitlTransport，
    # 让 HitlExecutor（运行在 worker 线程）可以反向通过 run_coroutine_threadsafe 推送 hitl_request。
    try:
        from server.services.regression.hitl import (
            WebSocketHitlTransport,
            set_transport,
        )

        hitl_transport = WebSocketHitlTransport()
        hitl_transport.set_app_loop(asyncio.get_running_loop())
        set_transport(hitl_transport)
        SLog.i(TAG, "--- [HITL] WebSocketHitlTransport bound to app loop ---")
    except Exception as e:
        SLog.w(TAG, f"--- [HITL] bind transport failed: {e} ---")

    # 只有主进程才打印这个 Ready
    SLog.i(TAG, "--- [LifeSpan] Backend services & Database ready ---")

    yield

    SLog.i(TAG, "--- [LifeSpan] Shutting down... ---")
    from server.core.gateway_beacon import unregister_gateway_beacons
    await unregister_gateway_beacons(getattr(app.state, "gateway_beacon", None))

    try:
        from server.services.regression.hitl import get_session_manager
        revoked = get_session_manager().revoke_all(reason="server_shutdown")
        if revoked:
            SLog.i(TAG, f"--- [HITL] revoked {revoked} pending sessions on shutdown ---")
    except Exception as e:
        SLog.w(TAG, f"--- [HITL] shutdown revoke failed: {e} ---")
    
    if app.state.device_client_task:
        app.state.device_client_task.cancel()
        try:
            await app.state.device_client_task
        except asyncio.CancelledError:
            pass


# -------------------------------------------------------------
# 4. App 定义
# -------------------------------------------------------------
app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory=UPLOAD_DIR), name="static")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# 挂载路由
app.include_router(wf_router.router)
app.include_router(log_router.router)
app.include_router(file_router.router)
app.include_router(app_graph_router.router)
app.include_router(websocket_router.router)
app.include_router(project_router.router)
app.include_router(task_router.router)
app.include_router(workflowRun_router.router)
app.include_router(ability_router.router)
app.include_router(device_router.router)
app.include_router(schedule_router.router)
app.include_router(feishu_router.router)
app.include_router(app_automation_router.router)
app.include_router(settings_router.router)
app.include_router(clawnode_router.router)
app.include_router(hitl_router.router)
app.include_router(case_runner_router.router)


@app.get("/")
def health_check():
    from server.core.gateway_beacon import build_gateway_identity

    identity = build_gateway_identity(get_local_ip())
    port = 10104
    ws_path = "/ws"
    ip = identity["local_ip"]
    return {
        "status": "ok",
        "version": "0.0.99",
        "ip": ip,
        "mdns": f"http://{identity['lan_host']}:{port}",
        "gateway": {
            "role": "gateway",
            "transport": "gateway",
            "displayName": identity["display_name"],
            "instanceId": identity["instance_id"],
            "lanHost": identity["lan_host"],
            "gatewayPort": port,
            "path": ws_path,
            "wsUrl": f"ws://{ip}:{port}{ws_path}",
        },
    }

@app.get("/get_api")
def get_api():
    from driver.tentacle.component.scan import scan
    return scan()

# -------------------------------------------------------------
# 5. 启动入口
# -------------------------------------------------------------
if __name__ == "__main__":
    multiprocessing.freeze_support()
    configure_proxy_bypass()

    import uvicorn

    # 🔥🔥🔥 只有在这里才打印 System/Perf 日志 🔥🔥🔥
    # 这样子进程 import main.py 时就不会刷屏了
    SLog.i(TAG, f"--- [System] Current Platform: {platform.system()} ---")
    SLog.i(TAG, f"--- [Config] Server Root: {BASE_DIR} ---")
    SLog.i(TAG, f"--- [Config] Upload Dir:  {UPLOAD_DIR} ---")
    SLog.i(TAG, f"--- [Perf] Imports loaded in: {time.time() - BOOT_START_TIME:.3f}s ---")

    is_frozen = getattr(sys, 'frozen', False)
    run_config = {
        "app": app,
        "host": "0.0.0.0",
        "port": 10104,
        "reload": not is_frozen,
        "access_log": True,
        "log_level": "info",
        "workers": 1
    }
    if not is_frozen:
        run_config["app"] = "main:app"

    # [修改] 移除自动发现和降级逻辑
    # 始终作为服务端启动，等待移动端扫码连接
    SLog.i(TAG, f"--- [Server] Starting Uvicorn (Frozen: {is_frozen}) ---")
    
    uvicorn.run(**run_config)
