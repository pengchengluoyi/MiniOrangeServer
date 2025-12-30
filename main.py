import os
import sys
import platform

# 🛡️ 只有在 Windows 平台上才执行 comtypes 的初始化逻辑
if platform.system() == "Windows":
    try:
        import comtypes
        import comtypes.client

        # 尝试导入或动态创建 comtypes.gen
        try:
            import comtypes.gen
        except ImportError:
            import types

            gen = types.ModuleType("comtypes.gen")
            sys.modules["comtypes.gen"] = gen
            comtypes.gen = gen

        # 设置缓存路径 (打包后 sys._MEIPASS 会很有用)
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
    # macOS 或 Linux 环境下直接跳过
    print(f"--- [System] Current Platform: {platform.system()} | Skipping Windows COM init ---")

import sys
# 🚀 [Fix] 尽早强制 stdout 使用行缓冲，确保 import 阶段的日志也能被 Electron 捕获
# 解决第一次启动看不到 [Perf] 日志的问题
sys.stdout.reconfigure(line_buffering=True)

import time
# ⏱️ [Perf] 记录启动开始时间
BOOT_START_TIME = time.time()
import os
import multiprocessing
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from server.core.migration import run_auto_migration

# ⏱️ [Perf] 细粒度耗时分析
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
from server.routers import rAbility as ability_router
print(f"--- [Perf] rAbility loaded: {time.time() - t_start:.3f}s ---")

# ⏱️ [Perf] 打印导入总耗时
print(f"--- [Perf] Imports loaded in: {time.time() - BOOT_START_TIME:.3f}s ---")

# 🔥 路径策略：使用用户数据目录 (持久化存储)
BASE_DIR = APP_DATA_DIR
UPLOAD_DIR = os.path.join(APP_DATA_DIR, "uploads")

print(f"--- [Config] Server Root: {BASE_DIR} ---")
print(f"--- [Config] Upload Dir:  {UPLOAD_DIR} ---")

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 自动迁移数据库
    run_auto_migration()
    # 初始化数据库
    Base.metadata.create_all(bind=engine)
    LogBase.metadata.create_all(bind=log_engine)
    yield

app = FastAPI(lifespan=lifespan)

# 🔥 挂载静态目录
app.mount("/static", StaticFiles(directory=UPLOAD_DIR), name="static")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(wf_router.router)
app.include_router(log_router.router)
app.include_router(file_router.router)
app.include_router(app_graph_router.router)
app.include_router(websocket_router.router)
app.include_router(project_router.router)
app.include_router(task_router.router)
app.include_router(workflowRun_router.router)
app.include_router(ability_router.router)

@app.get("/")
def health_check():
    return {"status": "ok", "version": "0.0.40", "upload_dir": UPLOAD_DIR}

@app.get("/get_api")
def get_api():
    from ability.component.scan import scan
    return scan()

if __name__ == "__main__":
    # 关键修复：防止打包后多进程导致服务重复启动
    multiprocessing.freeze_support()

    # 🚀 [Perf] 懒加载 Uvicorn，减少启动时的模块解析时间
    import uvicorn
    is_frozen = getattr(sys, 'frozen', False)
    run_config = {
        "app": app,
        "host": "127.0.0.1",
        "port": 10104,
        "reload": False,
        "access_log": True,
        "log_level": "info",
        "workers": 1
    }

    if not is_frozen:
        run_config.update({
            "reload": True,
            "app": "main:app"
        })

    print(f"--- [Server] Starting Uvicorn (Frozen: {is_frozen}) | Total Boot Time: {time.time() - BOOT_START_TIME:.3f}s ---")
    uvicorn.run(**run_config)