# driver/client.py
# !/usr/bin/env python
# -*-coding:utf-8 -*-

import asyncio
import json
import time
import base64
import uuid
import platform
import re
import socket
import os
import sys
from pathlib import Path
import traceback
import subprocess
import pathlib
import zipfile
import tempfile
import shutil
import psutil
import multiprocessing
import websockets
import builtins # 用于注入全局变量
from script.log import SLog, current_run_id, current_flow_id
from driver.agent.Core.orchestrator import Orchestrator
from driver.tentacle.common.mPath import get_adb_path

# 服务端 WebSocket 地址 (根据实际部署修改)
DEFAULT_SERVER_URL = "ws://miniorange.local:10104/ws"

def get_persistent_device_sn():
    """获取持久化的设备 SN，防止重启后 MAC 漂移导致识别为新设备"""
    # 将 SN 文件保存在用户主目录下，防止程序更新/重新解压导致文件丢失
    save_dir = Path.home() / ".miniorange"
    save_dir.mkdir(parents=True, exist_ok=True)
    sn_file = save_dir / "device_id.txt"
    
    # 1. 尝试从文件读取
    if sn_file.exists():
        try:
            with open(sn_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return content
        except Exception as e:
            SLog.w("DeviceClient", f"Error reading device_id.txt: {e}")

    # 2. 如果文件不存在或读取失败，生成新的 SN
    mac = uuid.getnode()
    mac_str = ':'.join(('%012X' % mac)[i:i+2] for i in range(0, 12, 2))
    sn = f"device_{mac_str}"

    # 3. 保存到文件
    try:
        with open(sn_file, "w", encoding="utf-8") as f:
            f.write(sn)
            SLog.i("DeviceClient", f"Generated and saved new Device SN: {sn}")
    except Exception as e:
        SLog.w("DeviceClient", f"Failed to save device_id.txt: {e}")
    
    return sn

DEVICE_SN = get_persistent_device_sn()
TAG = "DeviceClient"

# --- 本地任务执行器 (替代 driver.agent.actuator) ---

def process_runner_wrapper(run_id, flow_id, msg_queue, server_http_url, shared_responses, task_params=None):
    """
    在独立进程中执行任务的包装器
    """
    # 注入远程 API 地址供 PositionManager 使用
    builtins.REMOTE_API_URL = server_http_url

    # 🔥 注入目标设备 SN，供 Orchestrator/Driver 使用
    if task_params and "target_sn" in task_params:
        builtins.TARGET_DEVICE_SN = task_params["target_sn"]
        SLog.i("ProcessRunner", f"Target Device SN set to: {builtins.TARGET_DEVICE_SN}")

    # 注入通用查询函数 (通过 Queue -> WS -> Server -> WS -> SharedDict 获取数据)
    def query_server(action, params, timeout=10):
        if shared_responses is None:
            SLog.e("Client", "Shared responses dict is None, cannot query server.")
            return None

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
            if not isinstance(message, str):
                message = str(message)
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
        # runner = Manager(run_data)
        # runner.run()
        cns = Orchestrator()
        cns.run()

        # 3. 任务结束后，回传 Report
        from script.mTask import report
        msg_queue.put({"type": "report", "data": report})

    except Exception:
        SLog.e("System", f"Task Failed: {traceback.format_exc()}")
    finally:
        # 3. 清理上下文
        current_run_id.reset(token_run)
        current_flow_id.reset(token_flow)

class FileTransferManager:
    """
    管理设备间文件传输 (P2P via Server Relay)
    支持: 断点续传、文件夹自动压缩、自定义保存路径
    """
    CHUNK_SIZE = 40 * 1024  # 40KB chunk size (safe for WS frames)

    def __init__(self, client):
        self.client = client
        # 接收任务: {transfer_id: {file_handle, save_path, total_size, received_size}}
        self.incoming_transfers = {}
        # 发送任务: {transfer_id: {file_path, is_temp_zip}}
        self.outgoing_transfers = {}
        # 待处理的接收请求 (等待用户接受): {transfer_id: metadata}
        self.pending_offers = {}

    async def initiate_transfer(self, target_sn, file_path, save_path=None):
        """[发送方] 发起文件传输请求 (Offer)"""
        path = Path(file_path)
        if not path.exists():
            SLog.e(TAG, f"File not found: {file_path}")
            return False

        transfer_id = str(uuid.uuid4())
        
        # 1. 如果是文件夹，先压缩
        is_zip = False
        send_path = path
        if path.is_dir():
            SLog.i(TAG, f"Zipping folder: {path}")
            temp_zip = Path(tempfile.gettempdir()) / f"{path.name}.zip"
            await self._zip_folder(path, temp_zip)
            send_path = temp_zip
            is_zip = True

        file_size = send_path.stat().st_size
        filename = send_path.name

        # 记录发送任务
        self.outgoing_transfers[transfer_id] = {
            "path": send_path,
            "is_temp_zip": is_zip,
            "target_sn": target_sn
        }

        SLog.i(TAG, f"Offering file {transfer_id} -> {target_sn} ({filename}, {file_size} bytes)")

        # 2. 发送 Offer 信号
        req_payload = {
            "action": "p2p_signal",
            "data": {
                "target_sn": target_sn,
                "content": {
                    "type": "offer",
                    "transfer_id": transfer_id,
                    "filename": filename,
                    "size": file_size,
                    "is_folder": path.is_dir(), # 原始是否为文件夹
                    "save_path": save_path # 目标保存路径(用于自动接收)
                }
            }
        }
        await self.client.websocket.send(json.dumps(req_payload))

    async def accept_transfer(self, transfer_id, save_dir):
        """[接收方] 用户同意接收文件，指定保存路径"""
        if transfer_id not in self.pending_offers:
            SLog.w(TAG, f"Transfer ID {transfer_id} not found or expired.")
            return

        offer = self.pending_offers.pop(transfer_id)
        filename = offer['filename']
        total_size = offer['size']
        source_sn = offer['source_sn']

        # 构造保存路径
        save_path = Path(save_dir) / filename
        part_path = Path(save_dir) / (filename + ".part")

        # 断点续传检查
        offset = 0
        if part_path.exists():
            offset = part_path.stat().st_size
            # 如果本地文件比远程还大，说明出错了，重新下载
            if offset > total_size:
                offset = 0
                open(part_path, 'wb').close() # 清空
            elif offset == total_size:
                SLog.i(TAG, "File already downloaded.")
                return

        try:
            # 以追加模式打开
            f = open(part_path, "ab")
            self.incoming_transfers[transfer_id] = {
                "file": f,
                "path": save_path,
                "part_path": part_path,
                "total": total_size,
                "received": offset
            }

            SLog.i(TAG, f"Accepting {filename} from {offset} bytes. Saving to {save_path}")

            # 发送 Accept 信号 (带 Offset)
            payload = {
                "action": "p2p_signal",
                "data": {
                    "target_sn": source_sn,
                    "content": {
                        "type": "accept",
                        "transfer_id": transfer_id,
                        "offset": offset
                    }
                }
            }
            await self.client.websocket.send(json.dumps(payload))

        except Exception as e:
            SLog.e(TAG, f"Failed to open file for writing: {e}")

    async def _zip_folder(self, folder_path, output_path):
        """在 Executor 中压缩文件夹，避免阻塞"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._zip_folder_sync, folder_path, output_path)

    def _zip_folder_sync(self, folder_path, output_path):
        shutil.make_archive(str(output_path).replace('.zip', ''), 'zip', folder_path)

    def _send_chunks_sync(self, target_sn, transfer_id, path, offset=0):
        """同步读取文件并发送 Chunk (在 Executor 中运行)"""
        try:
            file_size = Path(path).stat().st_size
            with open(path, "rb") as f:
                if offset > 0:
                    f.seek(offset)
                    SLog.i(TAG, f"Resuming transfer {transfer_id} from offset {offset}")
                
                start_time = time.time()
                last_report_time = 0
                total_sent = 0
                
                index = int(offset / self.CHUNK_SIZE)
                while True:
                    chunk = f.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    
                    b64_data = base64.b64encode(chunk).decode('utf-8')
                    payload = {
                        "action": "p2p_signal",
                        "data": {
                            "target_sn": target_sn,
                            "content": {
                                "type": "chunk",
                                "transfer_id": transfer_id,
                                "index": index,
                                "data": b64_data
                            }
                        }
                    }
                    # 检查连接状态，避免死循环
                    if not self.client.websocket or self.client.websocket.closed:
                        raise ConnectionError("WebSocket disconnected during transfer")

                    asyncio.run_coroutine_threadsafe(
                        self.client.websocket.send(json.dumps(payload)), 
                        self.client.loop
                    )
                    index += 1
                    time.sleep(0.005) # 简单流控
                    
                    # --- Progress Reporting ---
                    total_sent += len(chunk)
                    now = time.time()
                    if now - last_report_time > 1.0: # Report every 1s
                        duration = now - start_time
                        speed = total_sent / duration if duration > 0 else 0
                        progress = ((offset + total_sent) / file_size * 100) if file_size > 0 else 0
                        
                        report_payload = {
                            "action": "transfer_progress",
                            "data": {
                                "transfer_id": transfer_id,
                                "progress": round(progress, 1),
                                "speed": int(speed), # bytes/s
                                "status": "transferring"
                            }
                        }
                        asyncio.run_coroutine_threadsafe(self.client.websocket.send(json.dumps(report_payload)), self.client.loop)
                        last_report_time = now

            # 发送完成信号
            finish_payload = {
                "action": "p2p_signal",
                "data": {
                    "target_sn": target_sn,
                    "content": {"type": "finish", "transfer_id": transfer_id}
                }
            }
            asyncio.run_coroutine_threadsafe(
                self.client.websocket.send(json.dumps(finish_payload)), 
                self.client.loop
            )
            SLog.i(TAG, f"Transfer {transfer_id} finished.")
            
            # Report 100%
            asyncio.run_coroutine_threadsafe(self.client.websocket.send(json.dumps({
                "action": "transfer_progress",
                "data": {
                    "transfer_id": transfer_id,
                    "progress": 100,
                    "speed": 0,
                    "status": "completed"
                }
            })), self.client.loop)
            
            # 清理发送端的临时文件
            if transfer_id in self.outgoing_transfers:
                task = self.outgoing_transfers[transfer_id]
                if task["is_temp_zip"] and task["path"].exists():
                    try:
                        os.remove(task["path"])
                        SLog.i(TAG, "Cleaned up temp zip file")
                    except: pass
                del self.outgoing_transfers[transfer_id]

        except Exception as e:
            SLog.e(TAG, f"Error sending file chunks: {e}")

    async def handle_signal(self, source_sn, data):
        """处理接收到的 P2P 信号"""
        msg_type = data.get("type")
        transfer_id = data.get("transfer_id")

        if msg_type == "offer":
            # [接收方] 收到文件发送请求
            SLog.i(TAG, f"Received file offer from {source_sn}: {data}")
            # 暂存请求，等待前端/用户调用 accept_transfer
            data['source_sn'] = source_sn
            
            # Check for auto-accept
            save_path = data.get("save_path")
            if save_path:
                SLog.i(TAG, f"Auto-accepting transfer {transfer_id} to {save_path}")
                self.pending_offers[transfer_id] = data
                await self.accept_transfer(transfer_id, save_path)
            else:
                self.pending_offers[transfer_id] = data
                # 这里可以通过 Log 通知前端有新文件请求
                SLog.i(TAG, f"PENDING_OFFER|{transfer_id}|{data['filename']}|{data['size']}")

        elif msg_type == "accept":
            # [发送方] 收到接收方的确认，开始发送
            offset = data.get("offset", 0)
            if transfer_id in self.outgoing_transfers:
                task = self.outgoing_transfers[transfer_id]
                SLog.i(TAG, f"Starting transmission for {transfer_id} from offset {offset}")
                
                loop = asyncio.get_running_loop()
                # 启动后台发送线程
                loop.run_in_executor(
                    None, 
                    self._send_chunks_sync, 
                    source_sn, # 这里的 source_sn 其实是 target (信号来源是接收方)
                    transfer_id, 
                    task["path"],
                    offset
                )

        elif msg_type == "chunk":
            # [接收方] 写入数据
            if transfer_id in self.incoming_transfers:
                task = self.incoming_transfers[transfer_id]
                try:
                    chunk_data = base64.b64decode(data.get("data"))
                    task["file"].write(chunk_data)
                    task["received"] += len(chunk_data)
                except Exception as e:
                    SLog.e(TAG, f"Write error: {e}")

        elif msg_type == "finish":
            # [接收方] 完成
            if transfer_id in self.incoming_transfers:
                task = self.incoming_transfers.pop(transfer_id)
                task["file"].close()
                
                # 重命名 .part 为正式文件
                if task["part_path"].exists():
                    # 如果目标文件已存在，自动重命名
                    final_path = task["path"]
                    counter = 1
                    while final_path.exists():
                        stem = final_path.stem
                        suffix = final_path.suffix
                        final_path = final_path.parent / f"{stem}_{counter}{suffix}"
                        counter += 1
                    
                    task["part_path"].rename(final_path)
                    SLog.i(TAG, f"File transfer complete: {final_path}")

class DeviceClient:
    def __init__(self, server_url, sn, role="node", shared_responses=None):
        self.server_url = server_url
        self.sn = sn
        self.role = role

        # 如果外部未传入 shared_responses，则内部自动初始化 Manager
        self._internal_manager = None
        if shared_responses is None:
            self._internal_manager = multiprocessing.Manager()
            self.shared_responses = self._internal_manager.dict()
            SLog.i(TAG, "Initialized internal Manager for shared_responses")
        else:
            self.shared_responses = shared_responses

        self.websocket = None
        if self.shared_responses is None:
            SLog.w(TAG, "Warning: shared_responses is None. IPC queries will fail.")
        self.is_running = False
        self.msg_queue = multiprocessing.Queue() # 进程间通信队列
        
        # 初始化文件传输管理器
        self.file_transfer = FileTransferManager(self)

    async def start(self):
        """启动客户端主循环"""
        self.is_running = True
        SLog.i(TAG, f"Device Client Starting... SN: {self.sn}")

        while self.is_running:
            try:
                self.loop = asyncio.get_running_loop() # 捕获当前 loop 供线程使用
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
            except websockets.ConnectionClosed:
                SLog.w(TAG, "Heartbeat connection closed.")
                break
            except Exception as e:
                SLog.e(TAG, f"Heartbeat error: {e}")
                # 遇到非连接错误(如ADB扫描异常)不要退出循环，而是等待后重试
                await asyncio.sleep(5)

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
                        params = msg["params"]
                        params["req_id"] = msg["req_id"]
                        payload = {
                            "action": msg["action"],
                            "req_id": msg["req_id"],
                            "data": params
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
                SLog.i(TAG, f"Received command: {command} Target: {params.get('target_sn')}")

                if command == "run_task":
                    self.execute_task(params)
                # --- 新增文件传输指令 ---
                elif command == "send_file":
                    # 服务端/前端控制此设备发送文件
                    target_sn = params.get("target_sn")
                    file_path = params.get("file_path")
                    save_path = params.get("save_path")
                    await self.file_transfer.initiate_transfer(target_sn, file_path, save_path)
                
                elif command == "accept_file":
                    # 服务端/前端控制此设备接受文件
                    transfer_id = params.get("transfer_id")
                    save_path = params.get("save_path")
                    await self.file_transfer.accept_transfer(transfer_id, save_path)

                elif command == "list_dir":
                    # 处理文件列表请求
                    target_path = params.get("path", "/")
                    # Windows 盘符处理
                    if platform.system() == "Windows" and target_path == "/":
                        # 简单列出盘符 (需要 psutil 或 os.list_drives 在 Py3.12+)
                        # 这里简化处理，默认 C:/
                        target_path = "C:/"

                    p = Path(target_path)
                    files = []
                    if p.exists() and p.is_dir():
                        try:
                            for item in p.iterdir():
                                try:
                                    files.append({
                                        "name": item.name,
                                        "is_dir": item.is_dir(),
                                        "size": item.stat().st_size if not item.is_dir() else 0,
                                        "time": item.stat().st_mtime
                                    })
                                except Exception:
                                    pass
                        except Exception as e:
                            SLog.e(TAG, f"List dir error: {e}")

                    # 发回结果
                    resp = {
                        "action": "dir_list",
                        "data": {
                            "path": str(p),
                            "files": files
                        }
                    }
                    await self.websocket.send(json.dumps(resp))

                else:
                    SLog.w(TAG, f"Unknown command: {command}")


            elif msg_type == "p2p_signal":
                # 处理 P2P 文件传输信号
                source_sn = data.get("source_sn")
                await self.file_transfer.handle_signal(source_sn, data.get("data"))

            else:
                # 处理查询响应 (非 command 类型的消息)
                req_id = data.get("req_id")
                # 兜底：如果最外层没有 req_id，尝试从 data 内部获取
                if not req_id and isinstance(data.get("data"), dict):
                    req_id = data.get("data").get("req_id")

                if req_id and self.shared_responses is not None:
                    self.shared_responses[req_id] = data.get("data")

        except json.JSONDecodeError:
            SLog.e(TAG, "Invalid JSON received")

    def execute_task(self, params):
        """在独立进程中执行任务"""
        run_id = params.get("run_id")
        flow_id = params.get("flow_id")

        if not (run_id and flow_id):
            SLog.e(TAG, "Missing task parameters (run_id, flow_id)")
            return

        SLog.i(TAG, f"Spawning process for task RunID: {run_id} TargetSN: {params.get('target_sn')}")

        # 转换 WS URL 为 HTTP URL 供子进程使用 (简单替换)
        http_url = self.server_url.replace("ws://", "http://").replace("/ws", "")

        # 使用 multiprocessing 启动任务，避免阻塞 WebSocket 通信
        p = multiprocessing.Process(
            target=process_runner_wrapper,
            args=(run_id, flow_id, self.msg_queue, http_url, self.shared_responses, params)
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
        """获取本机 IP (过滤代理虚拟 IP)"""
        try:
            # 优先使用 psutil
            for interface, snics in psutil.net_if_addrs().items():
                for snic in snics:
                    if snic.family == socket.AF_INET:
                        ip = snic.address
                        if ip == "127.0.0.1": continue
                        if ip.startswith("198.18."): continue # 过滤 Clash 虚拟 IP
                        if ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                            return ip
            
            # 回退方案
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip.startswith("198.18."): return "127.0.0.1"
            return ip
        except:
            return "127.0.0.1"

if __name__ == "__main__":
    # 确保 multiprocessing 在 Windows/macOS 上正常工作
    multiprocessing.freeze_support()

    # 配置代理绕过 (防止连接 ws://miniorange.local 时走代理)
    os.environ["no_proxy"] = os.environ.get("no_proxy", "") + ",localhost,127.0.0.1,::1,miniorange.local,0.0.0.0"
    os.environ["NO_PROXY"] = os.environ["no_proxy"]

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