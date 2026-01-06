import os
import sys
import asyncio
import platform
import socket  # 🚀 新增
from zeroconf import IPVersion, ServiceInfo  # 🚀 新增
from zeroconf.asyncio import AsyncZeroconf  # 🚀 改为异步版导入

# 🛡️ 只有在 Windows 平台上才执行 comtypes 的初始化逻辑
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

        if getattr(sys, 'frozen', False):
            gen_path = os.path.join(sys._MEIPASS, "comtypes_cache")
        else:
            gen_path = os.path.join(os.path.abspath('.'), "comtypes_cache")

        if not os.path.exists(gen_path):
            os.makedirs(gen_path)

        comtypes.client._generate_cache = gen_path
        comtypes.gen.__path__ = [gen_path]
        print(f"--- [System] Windows COM cache initialized at: {gen_path} ---")
    except Exception as e:
        print(f"--- [Warning] Windows COM initialization failed: {e} ---")
else:
    print(f"--- [System] Current Platform: {platform.system()} | Skipping Windows COM init ---")

# 🚀 强制 stdout 行缓冲
sys.stdout.reconfigure(line_buffering=True)

import time

BOOT_START_TIME = time.time()
import multiprocessing
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from server.core.migration import run_auto_migration

# ⏱️ [Perf] 数据库与路由导入
t_start = time.time()
from server.core.database import engine, Base, APP_DATA_DIR
from server.core.log_database import log_engine, LogBase
print(f"--- [Perf] Database Modules loaded: {time.time() - t_start:.3f}s ---")

t_start = time.time()
from server.routers import rWorkflow as wf_router
print(f"--- [Perf] rWorkflow loaded: {time.time() - t_start:.3f}s ---")

t_start = time.time()
from server.routers import rLog as log_router
print(f"--- [Perf] rLog loaded: {time.time() - t_start:.3f}s ---")

t_start = time.time()
from server.routers import rFile as file_router
print(f"--- [Perf] rFile loaded: {time.time() - t_start:.3f}s ---")

t_start = time.time()
from server.routers import rAppGraph as app_graph_router
print(f"--- [Perf] rAppGraph loaded: {time.time() - t_start:.3f}s ---")

t_start = time.time()
from server.websocket import rWebsocket as websocket_router

print(f"--- [Perf] rWebsocket loaded: {time.time() - t_start:.3f}s ---")

t_start = time.time()
from server.routers import rProject as project_router
print(f"--- [Perf] rProject loaded: {time.time() - t_start:.3f}s ---")

t_start = time.time()
from server.routers import rTask as task_router
print(f"--- [Perf] rTask loaded: {time.time() - t_start:.3f}s ---")

t_start = time.time()
from server.routers import rWorkflowRun as workflowRun_router
print(f"--- [Perf] rWorkflowRun loaded: {time.time() - t_start:.3f}s ---")

t_start = time.time()
from server.routers import rDevice as device_router
print(f"--- [Perf] rDevice loaded: {time.time() - t_start:.3f}s ---")


t_start = time.time()
from server.routers import rAbility as ability_router
print(f"--- [Perf] rAbility loaded: {time.time() - t_start:.3f}s ---")

# 确保模型被加载，以便 create_all 能扫描到

# ⏱️ [Perf] 打印导入总耗时
print(f"--- [Perf] Imports loaded in: {time.time() - BOOT_START_TIME:.3f}s ---")

# 🔥 路径策略：使用用户数据目录 (持久化存储)
BASE_DIR = APP_DATA_DIR
UPLOAD_DIR = os.path.join(APP_DATA_DIR, "uploads")

print(f"--- [Config] Server Root: {BASE_DIR} ---")
print(f"--- [Config] Upload Dir:  {UPLOAD_DIR} ---")

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)


# 🚀 [mDNS] 辅助函数：获取本机内网 IP 并注册域名
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"


# 2. 修改注册函数为 async
async def register_mdns(port):
    local_ip = get_local_ip()

    info = ServiceInfo(
        "_http._tcp.local.",
        "miniorange._http._tcp.local.",
        # 确保这里传入的是你通过 IP 能访问到的那个地址
        addresses=[socket.inet_aton(local_ip)],
        port=port,
        server="miniorange.local."
    )
    # 使用异步版 AsyncZeroconf
    aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    # 使用 await 注册
    await aiozc.async_register_service(info)

    print(f"--- [System] mDNS Registered: http://miniorange.local:{port} ({local_ip}) ---")
    return aiozc, info


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. 数据库准备 (保持不变)
    run_auto_migration()
    Base.metadata.create_all(bind=engine)
    LogBase.metadata.create_all(bind=log_engine)

    # 2. 异步注册 mDNS 🚀
    aiozc, srv_info = await register_mdns(10104)

    # 3. 启动后台客户端 (保持不变)
    from driver.client import DeviceClient, DEVICE_SN
    client = DeviceClient("ws://127.0.0.1:10104/ws", DEVICE_SN)
    bg_task = asyncio.create_task(client.start())

    print("--- [LifeSpan] Backend services & Database ready ---")

    yield

    # 4. 异步注销服务 🚀
    print("--- [LifeSpan] Shutting down... ---")
    await aiozc.async_unregister_service(srv_info)
    await aiozc.async_close()

    bg_task.cancel()
    try:
        await bg_task
    except asyncio.CancelledError:
        pass

app = FastAPI(lifespan=lifespan)

# 🔥 挂载静态目录
app.mount("/static", StaticFiles(directory=UPLOAD_DIR), name="static")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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


@app.get("/")
def health_check():
    return {"status": "ok", "version": "0.0.60", "upload_dir": UPLOAD_DIR}


@app.get("/get_api")
def get_api():
    from driver.tentacle.component.scan import scan
    return scan()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    import uvicorn

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
    # 如果是开发模式，必须用字符串路径
    if not is_frozen:
        run_config["app"] = "main:app"

    print(
        f"--- [Server] Starting Uvicorn (Frozen: {is_frozen}) | Total Boot Time: {time.time() - BOOT_START_TIME:.3f}s ---")
    uvicorn.run(**run_config)