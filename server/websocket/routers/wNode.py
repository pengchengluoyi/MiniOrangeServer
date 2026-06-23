# server/websocket/ws_handlers.py

import sys
import os
import time
import asyncio
import json
import socket
import psutil
import platform
import threading
from pathlib import Path
from server.core.security import SecurityManager
from driver.client import DeviceClient, DEVICE_SN
from script.log import SLog

TAG = "wsHandlers"


# --- 辅助函数 (原 main.py 中的逻辑) ---

def _get_local_ip():
    """获取本机真实局域网 IP"""
    lan_ip = None
    try:
        for interface, snics in psutil.net_if_addrs().items():
            for snic in snics:
                if snic.family == socket.AF_INET:
                    ip = snic.address
                    if ip == "127.0.0.1": continue
                    if ip.startswith("169.254."): continue  # 🔥 [新增] 过滤无效链路地址
                    if ip.startswith("198.18."): continue
                    if ip.startswith("172.17."): continue
                    if ip.startswith("100."): return ip

                    if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                        if not lan_ip: lan_ip = ip
        return lan_ip if lan_ip else "127.0.0.1"
    except:
        return "127.0.0.1"


def _get_all_server_urls(port, token=None):
    """获取本机所有可用的 WebSocket 连接地址"""
    urls = []
    # 1. 外网配置
    external = SecurityManager.get_external_url()
    if external: urls.append(external)

    # 2. 网卡 IP
    try:
        for interface, snics in psutil.net_if_addrs().items():
            for snic in snics:
                if snic.family == socket.AF_INET:
                    ip = snic.address
                    # 🔥 [新增] 严格过滤 169.254
                    if ip == "127.0.0.1" or ip.startswith("169.254.") or ip.startswith("172.17."):
                        continue
                    urls.append(f"ws://{ip}:{port}/ws")
    except:
        pass

    urls = list(set(urls))
    # 排序：让 192.168 或 10. 开头的排在前面，提高 iOS 命中率
    urls.sort(key=lambda x: 0 if "192.168" in x or "10." in x else 1)

    if token:
        return [f"{u}{'&' if '?' in u else '?'}token={token}" for u in urls]
    return urls


async def reload_device_client(app):
    """
    🔥 [新增] 动态重载 DeviceClient，无需重启整个进程
    """
    SLog.i(TAG, "--- [System] Hot Reloading DeviceClient... ---")

    # 1. 停止旧的 Client
    old_client = getattr(app.state, "device_client", None)
    old_task = getattr(app.state, "device_client_task", None)

    if old_client:
        SLog.i(TAG, "Stopping old client...")
        old_client.stop()
    
    if old_task:
        old_task.cancel()
        try:
            await old_task
        except asyncio.CancelledError:
            pass

    # 2. 加载新配置
    config = SecurityManager._config
    candidate_urls = config.get("candidate_urls", [])
    if isinstance(candidate_urls, str):
        candidate_urls = [candidate_urls]
    
    if not candidate_urls and config.get("target_url"):
        candidate_urls = [config.get("target_url")]

    # 3. 创建新 Client
    new_client = None
    if candidate_urls:
        SLog.i(TAG, f"Switching to Node Mode. Candidates: {len(candidate_urls)}")
        # 直接传入列表，DeviceClient.start() 内部会自动进行竞速选择
        new_client = DeviceClient(candidate_urls, DEVICE_SN, role="node")
    else:
        SLog.i(TAG, "Switching to Server Mode.")
        token = SecurityManager.get_token()
        SLog.i(TAG, f"Server Mode Token: {'[HAS_TOKEN]' if token else '[NO_TOKEN]'}")
        new_client = DeviceClient(["ws://127.0.0.1:10104/ws"], DEVICE_SN, role="client", token=token)

    # 4. 启动并挂载
    app.state.device_client = new_client
    app.state.device_client_task = asyncio.create_task(new_client.start())
    
            
    SLog.i(TAG, "DeviceClient reloaded successfully.")




# --- WebSocket Handlers ---

async def handle_get_server_info(websocket, data: dict):
    """
    [WS版] 获取服务端信息 (用于生成配网二维码)
    """
    try:
        ip = _get_local_ip()
        port = 10104
        hostname = platform.node()
        token = SecurityManager.get_token()

        # 🔥 [新增] 获取当前设备角色，供 App 扫码时判断
        client = getattr(websocket.app.state, "device_client", None)
        role = client.role if client else "server"

        candidate_urls = _get_all_server_urls(port, token)

        qr_payload = json.dumps({
            "v": 1,
            "type": "provisioning",
            "n": f"MiniOrange-{hostname}",
            "u": candidate_urls,
            # 配网二维码不带 Token，或根据需求携带
        })

        return {
            "code": 200,
            "data": {
                "hostname": hostname,
                "candidate_urls": candidate_urls,
                "token": token,
                "qr_payload": qr_payload,
                "role": role  # 🔥 返回角色，App 看到是 "server"/ "client" 才会显示"加入集群"按钮
            }
        }
    except Exception as e:
        return {"code": 500, "msg": str(e)}


async def handle_update_server_config(websocket, data: dict):
    """
    [WS版] 更新配置 (如 External URL)
    """
    external_url = data.get("external_url")
    clawnode_log_prefix = data.get("clawnode_log_prefix")

    # external_url 允许为 None 或 空字符串 (表示清除)
    if external_url is not None:
        url = external_url.strip()
        if not url:
            SecurityManager.set_external_url(None)
        else:
            if not (url.startswith("ws://") or url.startswith("wss://")):
                url = f"ws://{url}"
            SecurityManager.set_external_url(url)

    if clawnode_log_prefix is not None:
        prefix = str(clawnode_log_prefix).strip()
        SecurityManager.set_clawnode_log_prefix(prefix)

    return {"code": 200, "msg": "Config updated"}


async def handle_get_node_status(websocket, data: dict):
    """
    获取节点状态 (WebSocket版)
    """
    status = {
        "role": "unknown",
        "connected": False,
        "is_master": False,
        "candidates": [],
        "sn": ""
    }

    # [修改] 3. 通过 websocket.app 获取全局 state
    # FastAPI/Starlette 的 websocket 对象同样挂载了 app 实例
    client = getattr(websocket.app.state, "device_client", None)

    if client:
        status["role"] = client.role
        status["sn"] = client.sn
        status["candidates"] = client.candidate_urls

        if client.role == 'client':
            status["is_master"] = True

        client_ws = client.websocket
        status["connected"] = (client_ws is not None and getattr(client_ws, "open", False))
    else:
        status["role"] = "gateway"
        status["connected"] = True

    return {"code": 200, "data": status}


async def handle_join_cluster(websocket, data: dict):
    """
    [WS版] 接收移动端扫码后下发的 Master 信息，将本机绑定为 Node 节点
    此接口由移动端 App 调用，告诉本机："这是 Master 的地址，你现在是 Node，去连它！"
    """
    target_urls = data.get("target_urls")
    token = data.get("token")
    
    SLog.i(TAG, f"🔗 [Join] Received Master Info from App. URL: {target_urls}")

    if not target_urls:
        return {"code": 400, "msg": "Missing target_urls (Master Address)"}

    if not token:
        return {"code": 400, "msg": "Missing token"}

    try:
        # 🔥 [核心修改] 直接使用 SecurityManager 操作配置
        # 这样能确保 Server 读取和写入的是同一个内存对象/文件
        SecurityManager.load()

        # 1. 构造候选连接列表
        candidates = []
        if target_urls:
            if isinstance(target_urls, str):
                candidates.append(target_urls)
            elif isinstance(target_urls, list):
                candidates = target_urls

        final_candidates = []
        for url in candidates:
            sep = "&" if "?" in url else "?"
            if "token=" not in url:
                final_candidates.append(f"{url}{sep}token={token}")
            else:
                final_candidates.append(url)

        # 2. 更新 SecurityManager 的内存配置
        SecurityManager._config["candidate_urls"] = final_candidates
        if final_candidates:
            SecurityManager._config["target_url"] = final_candidates[0]

        SLog.i(TAG, f"🔗 [Join] Config before setting access_token: {SecurityManager._config.get('access_token')}")
        # 3. 🔥 保存 Token (最关键的一步)
        SecurityManager._config["access_token"] = token
        SLog.i(TAG, f"🔗 [Join] Config after setting access_token: {SecurityManager._config.get('access_token')}")

        SLog.i(TAG, f"🔗 [Join] Saving config via SecurityManager. Token: {token[:6]}...")

        # 4. 写入磁盘
        SecurityManager.save()

    except Exception as e:
        SLog.e(TAG, f"Save failed: {e}")
        return {"code": 500, "msg": f"Failed to save config: {e}"}

    # 5. 触发热重载 (不重启进程)
    # 使用 create_task 异步执行，防止阻塞 WebSocket 响应导致 ASGI Error
    asyncio.create_task(reload_device_client(websocket.app))
    return {"code": 200, "msg": "Login successful. Switched to Node Mode."}


async def handle_leave_cluster(websocket, data: dict):
    try:
        print(f"👋 [Leave] ENTERING HANDLER. Config Token: {SecurityManager._config.get('access_token')}")
        SLog.i(TAG, ">>> EXECUTING LEAVE CLUSTER <<<")

        # --- 🔥 [Commercial Grade] 严格的任务管控 ---
        app = websocket.app
        
        # 1. 强制取消后台任务 (Kill the task)
        # 只要任务被 Cancel 并 await，它就绝对不可能再有机会在后台写入旧配置
        task = getattr(app.state, "device_client_task", None)
        if task and not task.done():
            print("Strictly cancelling DeviceClient task...")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # 2. 清理对象引用
        app.state.device_client = None

        # --- 第二步：执行清除 ---
        # 此时后台任务已死，文件系统已加锁(原子操作)，写入是绝对安全的
        SecurityManager.clear_cluster_config()
        print(f"👋 [Leave] After clear_cluster_config. Config now: {SecurityManager._config.get('access_token')}")

        # 🔥 [Fix] 不要调用 get_token() 打印日志，防止触发 load() 导致并发竞争
        print("Cluster config cleared atomically.")

        # SLog.i(TAG, f"👋 [Leave] Before reload_device_client. Config now: {SecurityManager._config.get('access_token')}")
    except Exception as e:
        SLog.e(TAG, f"Leave cluster error: {e}")
        return {"code": 500, "msg": f"Error: {e}"}

    # --- 第三步：重载 ---
    # 此时 reload 内部虽然会尝试找 old_client，但我们在第一步已经处理并置空了
    # 它会直接读取磁盘上最新的（已清除的）配置来启动
    await reload_device_client(websocket.app)
    print(f"👋 [Leave] After reload_device_client. Config now: {SecurityManager._config.get('access_token')}")

    return {"code": 200, "msg": "Left cluster. Switched to Server Mode."}
