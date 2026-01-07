# !/usr/bin/env python
# -*-coding:utf-8 -*-

import asyncio
import json
import time
import uuid
import platform
import re
import socket
import os
import sys
import traceback
import subprocess
import multiprocessing
import websockets
import builtins # 用于注入全局变量
from script.log import SLog, current_run_id, current_flow_id
from driver.brain.core.manager import Manager
from driver.tentacle.common.mPath import get_adb_path

# 服务端 WebSocket 地址 (根据实际部署修改)
DEFAULT_SERVER_URL = "ws://miniorange.local:10104/ws"

# 生成或读取持久化的设备SN (此处示例为基于MAC生成)
def get_mac_address():
    mac = uuid.getnode()
    return ':'.join(('%012X' % mac)[i:i+2] for i in range(0, 12, 2))

DEVICE_SN = f"device_{get_mac_address()}"
TAG = "DeviceClient"

# --- 本地任务执行器 (替代 driver.agent.actuator) ---

def process_runner_wrapper(run_data, run_id, flow_id, msg_queue, server_http_url, shared_responses):
    """
    在独立进程中执行任务的包装器
    """
    # 注入远程 API 地址供 PositionManager 使用
    builtins.REMOTE_API_URL = server_http_url

    # 注入通用查询函数 (通过 Queue -> WS -> Server -> WS -> SharedDict 获取数据)
    def query_server(action, params, timeout=10):
        req_id = str(uuid.uuid4())
        # 1. 发送请求到主进程
        msg_queue.put({
            "type": "query",
            "req_id": req_id,
            "action": action,
            "params": params
        })
        # 2. 轮询共享字典等待响应
        start_time = time.time()
        while time.time() - start_time < timeout:
            if req_id in shared_responses:
                return shared_responses.pop(req_id)
            time.sleep(0.05)
        return None
    
    builtins.SERVER_QUERY = query_server

    # 定义基于队列的日志回调
    def _queue_log_writer(run_id, flow_id, node_id, level, tag, message):
        try:
            msg_queue.put({
                "type": "log",
                "data": {
                    "run_id": run_id,
                    "flow_id": flow_id,
                    "node_id": node_id,
                    "level": level,
                    "tag": tag,
                    "message": message
                }
            })
        except Exception:
            pass

    # 1. 设置日志上下文
    SLog.set_log_callback(_queue_log_writer)
    token_run = current_run_id.set(run_id)
    token_flow = current_flow_id.set(str(flow_id))

    try:
        SLog.i("System", f"Task Process Started PID:{os.getpid()}")
        # 2. 执行业务逻辑
        runner = Manager(run_data)
        runner.run()
        
        # 3. 任务结束后，回传 Report
        from script.mTask import report
        msg_queue.put({"type": "report", "data": report})
        
    except Exception:
        SLog.e("System", f"Task Failed: {traceback.format_exc()}")
    finally:
        # 3. 清理上下文
        current_run_id.reset(token_run)
        current_flow_id.reset(token_flow)

class DeviceClient:
    def __init__(self, server_url, sn, role="node", shared_responses=None):
        self.server_url = server_url
        self.sn = sn
        self.role = role
        self.shared_responses = shared_responses # 进程间共享的响应字典
        self.websocket = None
        self.is_running = False
        self.msg_queue = multiprocessing.Queue() # 进程间通信队列

    async def start(self):
        """启动客户端主循环"""
        self.is_running = True
        SLog.i(TAG, f"Device Client Starting... SN: {self.sn}")
        
        while self.is_running:
            try:
                SLog.i(TAG, f"Connecting to {self.server_url}...")
                async with websockets.connect(self.server_url) as ws:
                    self.websocket = ws
                    SLog.i(TAG, "Connected to server.")
                    
                    # 1. 发送注册包
                    if await self.register():
                        # 2. 注册成功后，并发运行 心跳任务 和 消息监听任务
                        await asyncio.gather(
                            self.heartbeat_loop(),
                            self.listen_loop(),
                            self.queue_consumer_loop() # 新增队列消费循环
                        )
            except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                SLog.w(TAG, f"Connection lost or failed: {e}. Retrying in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                SLog.e(TAG, f"Unexpected error: {e}")
                await asyncio.sleep(5)
            finally:
                self.websocket = None

    async def register(self):
        """向服务端注册设备信息"""
        # 1. 注册本机 (PC/Mac)
        pc_info = self._get_device_info()
        pc_result = await self._send_register_packet(pc_info)

        # 2. 扫描并注册连接的移动设备
        sub_devices = self._scan_connected_devices()
        for dev_info in sub_devices:
            await self._send_register_packet(dev_info)

        return pc_result

    async def _send_register_packet(self, info):
        """发送单个设备的注册包"""
        payload = {
            "action": "register",
            "req_id": str(uuid.uuid4()),
            "data": info
        }
        try:
            await self.websocket.send(json.dumps(payload))
            response = await self.websocket.recv()
            res_data = json.loads(response)
            
            if res_data.get("code") == 200:
                SLog.i(TAG, f"Registration successful: {info.get('sn')}")
                return True
            else:
                SLog.e(TAG, f"Registration failed for {info.get('sn')}: {res_data}")
                return False
        except Exception as e:
            SLog.e(TAG, f"Registration error for {info.get('sn')}: {e}")
            return False

    def _scan_connected_devices(self):
        """扫描连接的 Android/iOS 设备"""
        devices = []
        
        # --- Android (ADB) ---
        try:
            # 获取集成 ADB 路径 (去除引号，因为 subprocess list 参数不需要引号)
            adb_path = get_adb_path().strip('"')
            # 尝试执行 adb devices -l
            cmd = [adb_path, "devices", "-l"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                for line in lines[1:]: # 跳过第一行 List of devices attached
                    if not line.strip(): continue
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == 'device':
                        sn = parts[0]
                        # 尝试解析 model: product:Pixel_4 model:Pixel_4 device:flame
                        model = "Android Device"
                        for part in parts:
                            if part.startswith("model:"):
                                model = part.split(":")[1]
                        
                        devices.append({
                            "sn": sn,
                            "type": "android",
                            "role": "hub",  # 按照要求，USB连接的设备标记为 hub
                            "model": model,
                            "ip": self._get_android_ip(sn),  # 尝试获取具体 IP
                            "os_version": "Android",
                            "mac": sn
                        })
        except Exception as e:
            SLog.w(TAG, f"ADB scan failed: {e}")

        # --- iOS (tidevice) ---
        try:
            # 检查 tidevice 是否可用
            cmd = ["tidevice", "list"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                for line in lines:
                    if not line.strip(): continue
                    # tidevice list 输出格式: UDID Name
                    parts = line.split()
                    if len(parts) >= 1:
                        udid = parts[0]
                        name = parts[1] if len(parts) > 1 else "iOS Device"
                        devices.append({
                            "sn": udid,
                            "type": "ios",
                            "role": "hub",  # 按照要求，USB连接的设备标记为 hub
                            "model": name,
                            "ip": "USB",
                            "os_version": "iOS",
                            "mac": udid
                        })
        except Exception:
            pass # tidevice 可能未安装，忽略

        return devices

    def _get_android_ip(self, sn):
        """尝试获取 Android 设备 IP"""
        try:
            adb_path = get_adb_path().strip('"')
            # 方法 1: ip route (适用于较新 Android)
            # 输出示例: ... src 192.168.0.105 ...
            cmd = [adb_path, "-s", sn, "shell", "ip", "route"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                match = re.search(r'src\s+(\d{1,3}(?:\.\d{1,3}){3})', result.stdout)
                if match:
                    return match.group(1)
            
            # 方法 2: ifconfig wlan0 (适用于旧 Android)
            cmd = [adb_path, "-s", sn, "shell", "ifconfig", "wlan0"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                match = re.search(r'inet\s+(?:addr:)?(\d{1,3}(?:\.\d{1,3}){3})', result.stdout)
                if match:
                    return match.group(1)
        except Exception:
            pass
        return "USB"

    async def heartbeat_loop(self):
        """定时发送心跳"""
        while self.websocket:
            try:
                # 1. 发送本机心跳
                payload = {
                    "action": "heartbeat",
                    "data": {"sn": self.sn}
                }
                await self.websocket.send(json.dumps(payload))

                # 2. 发送子设备心跳
                sub_devices = self._scan_connected_devices()
                for dev in sub_devices:
                    payload_sub = {
                        "action": "heartbeat",
                        "data": {"sn": dev["sn"]}
                    }
                    await self.websocket.send(json.dumps(payload_sub))

                await asyncio.sleep(5)
            except Exception as e:
                SLog.e(TAG, f"Heartbeat error: {e}")
                break

    async def listen_loop(self):
        """监听服务端下发的指令"""
        while self.websocket:
            try:
                message = await self.websocket.recv()
                await self.handle_message(message)
            except websockets.ConnectionClosed:
                SLog.i(TAG, "WebSocket connection closed.")
                break
            except Exception as e:
                SLog.e(TAG, f"Receive error: {e}")

    async def queue_consumer_loop(self):
        """消费子进程产生的日志和报告，通过 WS 发送"""
        while self.websocket:
            while not self.msg_queue.empty():
                try:
                    msg = self.msg_queue.get_nowait()
                    if msg["type"] == "log":
                        payload = {"action": "client_log", "data": msg["data"]}
                        await self.websocket.send(json.dumps(payload))
                    elif msg["type"] == "report":
                        payload = {"action": "task_report", "data": msg["data"]}
                        await self.websocket.send(json.dumps(payload))
                    elif msg["type"] == "query":
                        # 转发子进程的查询请求
                        payload = {
                            "action": msg["action"], 
                            "req_id": msg["req_id"],
                            "data": msg["params"]
                        }
                        await self.websocket.send(json.dumps(payload))
                except Exception as e:
                    SLog.e(TAG, f"Queue send error: {e}")
            
            # 避免空转占用 CPU
            await asyncio.sleep(0.1)

    async def handle_message(self, message):
        """处理具体的业务消息"""
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            action = data.get("action")
            
            if msg_type == "command":
                command = data.get("command")
                params = data.get("params", {})
                SLog.i(TAG, f"Received command: {command}")
                
                if command == "run_task":
                    self.execute_task(params)
                else:
                    SLog.w(TAG, f"Unknown command: {command}")
            
            else:
                # 处理查询响应 (非 command 类型的消息)
                req_id = data.get("req_id")
                if req_id and self.shared_responses is not None:
                    self.shared_responses[req_id] = data.get("data")
                    
        except json.JSONDecodeError:
            SLog.e(TAG, "Invalid JSON received")

    def execute_task(self, params):
        """在独立进程中执行任务"""
        run_id = params.get("run_id")
        flow_id = params.get("flow_id")
        run_data = params.get("run_data")
        
        if not (run_id and flow_id and run_data):
            SLog.e(TAG, "Missing task parameters (run_id, flow_id, or run_data)")
            return

        SLog.i(TAG, f"Spawning process for task RunID: {run_id}")
        
        # 转换 WS URL 为 HTTP URL 供子进程使用 (简单替换)
        http_url = self.server_url.replace("ws://", "http://").replace("/ws", "")

        # 使用 multiprocessing 启动任务，避免阻塞 WebSocket 通信
        p = multiprocessing.Process(
            target=process_runner_wrapper,
            args=(run_data, run_id, flow_id, self.msg_queue, http_url, self.shared_responses)
        )
        p.start()

    def _get_device_info(self):
        """收集设备信息"""
        return {
            "sn": self.sn,
            "type": "pc",  # 默认为 PC 节点
            "role": self.role,
            "model": platform.node(),
            "ip": self._get_ip(),
            "os_version": f"{platform.system()} {platform.release()}",
            "mac": self.sn.replace("device_", "")
        }

    def _get_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

if __name__ == "__main__":
    # 确保 multiprocessing 在 Windows/macOS 上正常工作
    multiprocessing.freeze_support()
    
    # 自动选择连接地址: 优先尝试本地，失败则使用 mDNS 域名
    target_url = DEFAULT_SERVER_URL
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        # 检查本地 10104 端口是否开放
        if sock.connect_ex(('127.0.0.1', 10104)) == 0:
            target_url = "ws://127.0.0.1:10104/ws"
            SLog.i(TAG, "Detected local server, switching to localhost.")
        sock.close()
    except Exception:
        pass

    # 使用 Manager 创建跨进程共享字典
    from multiprocessing import Manager as SyncManager
    with SyncManager() as manager:
        shared_responses = manager.dict()
        client = DeviceClient(target_url, DEVICE_SN, role="node", shared_responses=shared_responses)
        try:
            asyncio.run(client.start())
        except KeyboardInterrupt:
            SLog.i(TAG, "Stopped by user")