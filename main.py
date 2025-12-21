import sys
import os
import multiprocessing
import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from server.core.database import engine, Base
from server.core.log_database import log_engine, LogBase
from server.routers import rWorkflow as wf_router
from server.routers import rLog as log_router
from server.routers import rFile as file_router
from server.routers import rAppGraph as app_graph_router
from server.routers import rWebsocket as websocket_router

# 🔥 路径策略：永远相对于 main.py 所在目录
# 这样无论是在 IDE 跑，还是打包后，都存在当前运行目录下
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")

print(f"--- [Config] Server Root: {BASE_DIR} ---")
print(f"--- [Config] Upload Dir:  {UPLOAD_DIR} ---")

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

@asynccontextmanager
async def lifespan(app: FastAPI):
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

@app.get("/")
def health_check():
    return {"status": "ok", "version": "1.0.1", "upload_dir": UPLOAD_DIR}

@app.get("/get_api")
def get_api():
    from ability.component.scan import scan
    return scan()

if __name__ == "__main__":
    # 关键修复：防止打包后多进程导致服务重复启动
    multiprocessing.freeze_support()

    is_frozen = getattr(sys, 'frozen', False)
    run_config = {
        "app": app,
        "host": "127.0.0.1",
        "port": 8001,
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

    print(f"--- [Server] Starting Uvicorn (Frozen: {is_frozen}) ---")
    uvicorn.run(**run_config)