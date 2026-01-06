# !/usr/bin/env python
# -*-coding:utf-8 -*-

import asyncio
import json
import uuid
import platform
import socket
import os
import sys
import traceback
import multiprocessing
import websockets
from script.log import SLog, current_run_id, current_flow_id
from driver.brain.core.manager import Manager
from server.core.log_database import LogSessionLocal
from server.models.log import WorkflowLog

# 服务端 WebSocket 地址 (根据实际部署修改)
SERVER_URL = "ws://127.0.0.1:10104/ws"

# 生成或读取持久化的设备SN (此处示例为基于MAC生成)
def get_mac_address():
    mac = uuid.getnode()
    return ':'.join(('%012X' % mac)[i:i+2] for i in range(0, 12, 2))

DEVICE_SN = f"device_{get_mac_address()}"
TAG = "DeviceClient"

# --- 本地任务执行器 (替代 driver.agent.actuator) ---

def _client_log_writer(run_id, flow_id, node_id, level, tag, message):
    """
    客户端日志回调。
    由于客户端无法直接连接服务端数据库，这里仅做占位或控制台输出。
    后续可通过 WebSocket 将日志回传给服务端。
    """
    # 默认 SLog 会打印到控制台，这里可以扩展为发送到 WS 队列
    db = LogSessionLocal()
    try:
        log = WorkflowLog(
            run_id=run_id,
            flow_id=flow_id,
            node_id=node_id,
            level=level,
            tag=tag,
            message=message
        )
        db.add(log)
        db.commit()
    except Exception as e:
        SLog.e("System", f"Log Write Error: {e}")
    finally:
        db.close()

def process_runner_wrapper(run_data, run_id, flow_id):
    """
    在独立进程中执行任务的包装器
    """
    # 1. 设置日志上下文
    SLog.set_log_callback(_client_log_writer)
    token_run = current_run_id.set(run_id)
    token_flow = current_flow_id.set(str(flow_id))

    try:
        SLog.i("System", f"Task Process Started PID:{os.getpid()}")
        # 2. 执行业务逻辑
        runner = Manager(run_data)
        runner.run()
    except Exception:
        SLog.e("System", f"Task Failed: {traceback.format_exc()}")
    finally:
        # 3. 清理上下文
        current_run_id.reset(token_run)
        current_flow_id.reset(token_flow)

class DeviceClient:
    def __init__(self, server_url, sn):
        self.server_url = server_url
        self.sn = sn
        self.websocket = None
        self.is_running = False

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
                            self.listen_loop()
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
        info = self._get_device_info()
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
                SLog.i(TAG, "Registration successful.")
                return True
            else:
                SLog.e(TAG, f"Registration failed: {res_data}")
                return False
        except Exception as e:
            SLog.e(TAG, f"Registration error: {e}")
            return False

    async def heartbeat_loop(self):
        """定时发送心跳"""
        while self.websocket:
            try:
                payload = {
                    "action": "heartbeat",
                    "data": {"sn": self.sn}
                }
                await self.websocket.send(json.dumps(payload))
                SLog.i(TAG, "Heartbeat sent")
                await asyncio.sleep(1)  # 测试期间改为10秒，方便观察
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
            elif action == "heartbeat":
                SLog.i(TAG, "Heartbeat Ack received")
                    
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
        
        # 使用 multiprocessing 启动任务，避免阻塞 WebSocket 通信
        p = multiprocessing.Process(
            target=process_runner_wrapper,
            args=(run_data, run_id, flow_id)
        )
        p.start()

    def _get_device_info(self):
        """收集设备信息"""
        return {
            "sn": self.sn,
            "type": "pc",  # 默认为 PC 节点
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
    
    client = DeviceClient(SERVER_URL, DEVICE_SN)
    try:
        asyncio.run(client.start())
    except KeyboardInterrupt:
        SLog.i(TAG, "Stopped by user")