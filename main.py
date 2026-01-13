import os
import sys
import asyncio
import platform
import socket
import time
import multiprocessing
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from zeroconf import IPVersion, ServiceInfo, Zeroconf
from zeroconf.asyncio import AsyncZeroconf

# 🚀 强制 stdout 行缓冲
sys.stdout.reconfigure(line_buffering=True)

BOOT_START_TIME = time.time()

# -------------------------------------------------------------
# 1. 模块导入 (移除这里的 print，减少子进程噪音)
# -------------------------------------------------------------
from server.core.migration import run_auto_migration
from server.core.database import engine, Base, APP_DATA_DIR
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
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"


async def register_mdns(port):
    local_ip = get_local_ip()
    info = ServiceInfo(
        "_http._tcp.local.",
        "miniorange._http._tcp.local.",
        addresses=[socket.inet_aton(local_ip)],
        port=port,
        server="miniorange.local."
    )
    aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    try:
        await aiozc.async_register_service(info, allow_name_change=True)
        print(f"--- [System] mDNS Registered: http://miniorange.local:{port} ({local_ip}) ---")
    except Exception as e:
        print(f"--- [Warning] mDNS Registration failed: {e} ---")
    return aiozc, info


# -------------------------------------------------------------
# 3. LifeSpan 生命周期管理
# -------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 数据库初始化
    run_auto_migration()
    Base.metadata.create_all(bind=engine)
    LogBase.metadata.create_all(bind=log_engine)

    # 启动后台服务
    aiozc, srv_info = await register_mdns(10104)

    from server.websocket.device_manager import DeviceManager
    asyncio.create_task(DeviceManager().monitor_heartbeats())

    from server.core.scheduler import SchedulerService
    SchedulerService().start()

    # 启动 Client (服务端内置 Client)
    from driver.client import DeviceClient, DEVICE_SN
    client = DeviceClient("ws://127.0.0.1:10104/ws", DEVICE_SN, role="client")
    bg_task = asyncio.create_task(client.start())

    # 只有主进程才打印这个 Ready
    print("--- [LifeSpan] Backend services & Database ready ---")

    yield

    print("--- [LifeSpan] Shutting down... ---")
    await aiozc.async_unregister_service(srv_info)
    await aiozc.async_close()
    bg_task.cancel()
    try:
        await bg_task
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


@app.get("/")
def health_check():
    return {"status": "ok", "version": "0.0.81", "upload_dir": UPLOAD_DIR}


@app.get("/get_api")
def get_api():
    from driver.tentacle.component.scan import scan
    return scan()


# -------------------------------------------------------------
# 5. 启动入口
# -------------------------------------------------------------
if __name__ == "__main__":
    multiprocessing.freeze_support()
    import uvicorn

    # 🔥🔥🔥 只有在这里才打印 System/Perf 日志 🔥🔥🔥
    # 这样子进程 import main.py 时就不会刷屏了
    print(f"--- [System] Current Platform: {platform.system()} ---")
    print(f"--- [Config] Server Root: {BASE_DIR} ---")
    print(f"--- [Config] Upload Dir:  {UPLOAD_DIR} ---")
    print(f"--- [Perf] Imports loaded in: {time.time() - BOOT_START_TIME:.3f}s ---")

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

    # 自动发现逻辑
    existing_server_url = None
    try:
        zc = Zeroconf()
        info = zc.get_service_info("_http._tcp.local.", "miniorange._http._tcp.local.", timeout=1000)
        zc.close()
        if info and info.addresses:
            addr = socket.inet_ntoa(info.addresses[0])
            port = info.port
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(0.5)
            if s.connect_ex((addr, port)) == 0:
                existing_server_url = f"ws://{addr}:{port}/ws"
                print(f"--- [System] Found active server at {existing_server_url} ---")
            s.close()
    except Exception:
        pass

    if existing_server_url:
        print("--- [System] Switching to Node Mode (Server found in LAN) ---")
        from driver.client import DeviceClient, DEVICE_SN

        client = DeviceClient(existing_server_url, DEVICE_SN, role="node")
        asyncio.run(client.start())
    else:
        print(f"--- [Server] Starting Uvicorn (Frozen: {is_frozen}) ---")
        uvicorn.run(**run_config)