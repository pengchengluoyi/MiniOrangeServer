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

# 🚀 强制 stdout 行缓冲
sys.stdout.reconfigure(line_buffering=True)

BOOT_START_TIME = time.time()

# -------------------------------------------------------------
# 1. 模块导入 (移除这里的 print，减少子进程噪音)
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
    # 追加常见的本地回环和局域网地址到 no_proxy
    # 注意：requests/websockets 等库会读取此环境变量
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
    local_ip = get_local_ip()
    # [修改] 使用主机名作为服务标识，防止局域网内多实例名称冲突
    hostname = platform.node().split('.')[0]
    safe_hostname = "".join(c for c in hostname if c.isalnum() or c == "-") or "miniorange"

    mdns_hostname = f"miniorange-{safe_hostname}.local."
    info = ServiceInfo(
        "_http._tcp.local.",
        f"miniorange-{safe_hostname}._http._tcp.local.",
        addresses=[socket.inet_aton(local_ip)],
        port=port,
        server=mdns_hostname
    )
    aiozc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    try:
        await aiozc.async_register_service(info, allow_name_change=True)
        print(f"--- [System] mDNS Registered: http://{mdns_hostname.rstrip('.')}:{port} ({local_ip}) ---")
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
    # 安全配置初始化
    SecurityManager.load()
    
    Base.metadata.create_all(bind=engine)
    LogBase.metadata.create_all(bind=log_engine)

    # 配置代理绕过
    configure_proxy_bypass()

    # 启动后台服务
    aiozc, srv_info = await register_mdns(10104)

    from server.websocket.device_manager import DeviceManager
    asyncio.create_task(DeviceManager().monitor_heartbeats())

    from server.core.scheduler import SchedulerService
    SchedulerService().start()

    # 启动 Client (服务端内置 Client)
    from driver.client import DeviceClient, DEVICE_SN, load_config

    config = load_config()
    target_url = config.get("target_url")

    if target_url:
        print(f"--- [System] Node Mode Active. Connecting to Cluster: {target_url} ---")
        # Node 模式：连接到远程 Server，但本地 Web 服务依然启动，方便管理
        client = DeviceClient(target_url, DEVICE_SN, role="node")
    else:
        # Server 模式：连接到本地
        client = DeviceClient("ws://127.0.0.1:10104/ws", DEVICE_SN, role="client", token=SecurityManager.get_token())


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
    hostname = platform.node().split('.')[0]
    safe_hostname = "".join(c for c in hostname if c.isalnum() or c == "-") or "miniorange"

    return {
        "status": "ok",
        "version": "0.0.87",
        "ip": get_local_ip(),
        "mdns": f"http://miniorange-{safe_hostname}.local:10104",
        "port": 10104,
        "upload_dir": UPLOAD_DIR
    }


@app.get("/get_api")
def get_api():
    from driver.tentacle.component.scan import scan
    return scan()


@app.get("/sys/server_info")
def get_server_info():
    """
    [新增] 获取服务端连接信息
    前端可以使用 data.connect_url 或 data.ip/port 生成二维码
    移动端 App (已登录账号) 扫码后，解析此 URL 建立 WebSocket 连接
    """
    ip = get_local_ip()
    port = 10104
    hostname = platform.node()
    
    # 获取安全配置
    token = SecurityManager.get_token()
    external_url = SecurityManager.get_external_url()
    
    # 优先使用配置的外网 URL，否则使用局域网 IP
    # [修复] 增加有效性检查，防止配置为空字符串或仅有协议头时导致连接失败
    base_url = external_url if (external_url and len(external_url) > 6) else f"ws://{ip}:{port}/ws"

    # 拼接 Token
    connect_url = f"{base_url}?token={token}"

    # [新增] 生成标准化的 QR Code 内容 (JSON 格式)
    # 移动端扫码后解析此 JSON，获取名称、URL 和 Token
    qr_payload = json.dumps({
        "n": f"MiniOrange-{hostname}", # Name (用于显示)
        "u": base_url,                 # URL (ws://...)
        "t": token                     # Token (安全令牌)
    })

    return {
        "code": 200,
        "data": {
            "ip": ip,
            "port": port,
            "hostname": hostname,
            "connect_url": connect_url,
            "token": token,
            "qr_payload": qr_payload, # 前端请使用此字段生成二维码
            "external_url": external_url,
            "server_name": f"MiniOrange-{hostname}"
        }
    }


@app.get("/sys/scan_lan_servers")
def scan_lan_servers():
    """
    [新增] 扫描局域网内的其他 Server 节点
    前端调用此接口获取列表，用户选择后调用 /sys/join_cluster
    """
    found_servers = []

    class ServiceListener:
        def add_service(self, zc: Zeroconf, type_: str, name: str):
            try:
                info = zc.get_service_info(type_, name)
                if info and info.addresses:
                    ip = socket.inet_ntoa(info.addresses[0])
                    # 过滤掉非 miniorange 服务
                    if "miniorange" in name:
                        found_servers.append({
                            "name": name,
                            "ip": ip,
                            "port": info.port,
                            "server": info.server,
                            "url": f"ws://{ip}:{info.port}/ws"
                        })
            except Exception:
                pass

        def remove_service(self, zc: Zeroconf, type_: str, name: str): pass
        def update_service(self, zc: Zeroconf, type_: str, name: str): pass

    try:
        zc = Zeroconf()
        ServiceBrowser(zc, "_http._tcp.local.", ServiceListener())
        time.sleep(1.5) # 等待扫描结果
        zc.close()
    except Exception as e:
        print(f"--- [Scan] Error: {e} ---")

    return {"code": 200, "data": found_servers}


class ServerConfigReq(BaseModel):
    external_url: str = None

@app.post("/sys/config")
def update_server_config(req: ServerConfigReq):
    """[新增] 更新服务端配置 (如外网映射地址)"""
    if req.external_url is not None:
        # 简单的格式修正，确保是 ws:// 或 wss:// 开头
        url = req.external_url.strip()
        
        if not url:
            SecurityManager.set_external_url(None)
        else:
            # [修复] 强制添加协议头，防止生成无效的二维码 URL
            if not (url.startswith("ws://") or url.startswith("wss://")):
                url = f"ws://{url}"
            SecurityManager.set_external_url(url)
        
    return {"code": 200, "message": "Config updated"}


class JoinClusterRequest(BaseModel):
    target_url: str
    token: str = None # 支持带 Token 加入

@app.post("/sys/join_cluster")
def join_cluster(req: JoinClusterRequest):
    """
    [新增] 切换到 Node 模式并连接指定 Server
    1. 保存目标 URL 到配置文件
    2. 重启自身，进入 Node 模式 (不启动 Uvicorn)
    """
    # [修改] 使用 driver.client 中的统一配置管理
    from driver.client import load_config, save_config
    
    try:
        config = load_config()
        config["target_url"] = req.target_url
        # 如果 URL 里没有 token 且请求带了 token，则拼上去
        if req.token and "token=" not in req.target_url:
            sep = "&" if "?" in req.target_url else "?"
            config["target_url"] = f"{req.target_url}{sep}token={req.token}"
            
        save_config(config)
    except Exception as e:
        return {"code": 500, "message": f"Failed to save config: {e}"}

    def restart_server():
        time.sleep(1)
        print("--- [System] Restarting into Node Mode... ---")
        python = sys.executable
        os.execl(python, python, *sys.argv)

    import threading
    threading.Thread(target=restart_server).start()

    return {"code": 200, "message": "Switching to Node Mode..."}


@app.post("/sys/leave_cluster")
def leave_cluster():
    """
    [新增] 退出集群模式 (恢复为独立 Server)
    """
    from driver.client import load_config, save_config
    try:
        config = load_config()
        if "target_url" in config:
            del config["target_url"]
            save_config(config)
    except Exception as e:
        return {"code": 500, "message": f"Failed to update config: {e}"}

    def restart_server():
        time.sleep(1)
        print("--- [System] Restarting to restore Server Mode... ---")
        python = sys.executable
        os.execl(python, python, *sys.argv)

    import threading
    threading.Thread(target=restart_server).start()

    return {"code": 200, "message": "Leaving cluster and restarting..."}


# -------------------------------------------------------------
# 5. 启动入口
# -------------------------------------------------------------
if __name__ == "__main__":
    multiprocessing.freeze_support()
    
    # [修改] 检查是否处于 Node 模式 (读取 config.json 中的 target_url)
    # 如果是，则直接启动 DeviceClient 连接目标 Server，不再启动 Uvicorn
    try:
        # 临时导入 driver.client 获取配置 (此时 DEVICE_SN 也会被初始化/读取)
        from driver.client import load_config, DeviceClient, DEVICE_SN
        
        config = load_config()
        target_url = config.get("target_url")
        
        if target_url:
            print(f"--- [System] Node Mode Active. Connecting to {target_url} ---")
            # 仅运行 Client，不启动 Web Server
            client = DeviceClient(target_url, DEVICE_SN, role="node")
            # 注意：不再需要注入 cluster_config_path，Client 内部会使用 load_config/save_config
            asyncio.run(client.start())
            sys.exit(0) # Client 退出后结束进程
    except Exception as e:
        print(f"--- [Error] Failed to start Node Mode: {e} ---")
        # 如果出错，继续向下执行，尝试启动 Server 模式

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

    # [修改] 移除自动发现和降级逻辑
    # 始终作为服务端启动，等待移动端扫码连接
    print(f"--- [Server] Starting Uvicorn (Frozen: {is_frozen}) ---")
    
    uvicorn.run(**run_config)