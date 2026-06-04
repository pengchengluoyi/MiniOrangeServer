# driver/client.py
# !/usr/bin/env python
# -*-coding:utf-8 -*-
import threading
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
import random
from pathlib import Path
import traceback
import subprocess
import pathlib
import zipfile
import tempfile
import shutil
import psutil
import multiprocessing
import struct
import websockets
import concurrent.futures
import builtins  # 用于注入全局变量
from urllib.parse import urlparse
from script.log import SLog, current_run_id, current_flow_id
import adbutils
from driver.agent.Core.orchestrator import Orchestrator
from driver.tentacle.common.mPath import get_adb_path, get_scrcpy_server_path

# 🔥 [新增] 引入 SecurityManager，实现配置大一统
from server.core.security import SecurityManager

# 服务端 WebSocket 地址
DEFAULT_SERVER_URL = "ws://miniorange.local:10104/ws"


# [新增] 获取本机 IP (优先 Tailscale 100.x, 然后局域网 IP)
def get_local_ip():
    lan_ip = None
    try:
        # 优先使用 psutil
        for interface, snics in psutil.net_if_addrs().items():
            for snic in snics:
                if snic.family == socket.AF_INET:
                    ip = snic.address
                    if ip == "127.0.0.1": continue
                    if ip.startswith("169.254."): continue
                    if ip.startswith("198.18."): continue  # 过滤 Clash 虚拟 IP

                    if ip.startswith("100."):
                        return ip  # 🚀 发现 Tailscale IP 立即返回
                    elif ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("172."):
                        if not lan_ip:
                            lan_ip = ip

        if lan_ip: return lan_ip

        # 回退方案
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip.startswith("198.18."): return "127.0.0.1"
        return ip
    except:
        return "127.0.0.1"


def get_persistent_device_sn():
    """获取持久化的设备 SN，防止重启后 MAC 漂移导致识别为新设备"""
    # 🔥 [统一] 直接使用 SecurityManager
    if not SecurityManager._config:
        SecurityManager.load()
    
    config = SecurityManager._config

    # 1. 尝试从 Config 读取
    if "device_sn" in config and config["device_sn"]:
        return config["device_sn"]

    # 3. 生成新的 SN
    mac = uuid.getnode()
    mac_str = ':'.join(('%012X' % mac)[i:i + 2] for i in range(0, 12, 2))
    sn = f"device_{mac_str}"

    # 4. 保存到 Config
    config["device_sn"] = sn
    SecurityManager.save()
    SLog.i("DeviceClient", f"Generated and saved new Device SN: {sn}")

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
    if task_params and task_params.get("env_profile"):
        builtins.RUN_ENV_PROFILE = str(task_params["env_profile"])
        SLog.i("ProcessRunner", f"Run env profile: {builtins.RUN_ENV_PROFILE}")

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
    CHUNK_SIZE = 1024 * 1024  # 1MB chunk size (Binary Frame)

    def __init__(self, client):
        self.client = client
        # 接收任务: {transfer_id: {file_handle, save_path, total_size, received_size}}
        self.incoming_transfers = {}
        # 发送任务: {transfer_id: {file_path, is_temp_zip}}
        self.outgoing_transfers = {}
        # 待处理的接收请求 (等待用户接受): {transfer_id: metadata}
        self.pending_offers = {}
        # [新增] 异步写入队列，解耦网络接收与磁盘IO
        self.write_queue = asyncio.Queue()
        self._worker_running = False

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
                    "is_folder": path.is_dir(),  # 原始是否为文件夹
                    "save_path": save_path  # 目标保存路径(用于自动接收)
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

        # [修复] 检查是否有针对同一文件的旧传输任务未清理，导致文件占用
        for tid, task in list(self.incoming_transfers.items()):
            if task["part_path"] == part_path:
                SLog.w(TAG, f"Found stale transfer {tid} for {filename}, cleaning up...")
                try:
                    task["file"].close()
                except:
                    pass
                del self.incoming_transfers[tid]

        # 断点续传检查
        offset = 0
        if part_path.exists():
            offset = part_path.stat().st_size
            # 如果本地文件比远程还大，说明出错了，重新下载
            if offset > total_size:
                offset = 0
                open(part_path, 'wb').close()  # 清空
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

    def start(self):
        """启动后台写入消费者"""
        if not self._worker_running:
            self._worker_running = True
            asyncio.create_task(self._write_worker())

    async def _write_worker(self):
        """后台消费者：从队列取数据，在线程池中解码写入"""
        while True:
            try:
                task = await self.write_queue.get()
                msg_type, transfer_id, data = task

                if msg_type == "chunk":
                    # 将耗时的解码和写入放入线程池，释放 EventLoop 接收网络包
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, self._write_chunk_sync, transfer_id, data)
                elif msg_type == "finish":
                    await self._finish_transfer(transfer_id)

                self.write_queue.task_done()
            except Exception as e:
                SLog.e(TAG, f"Write worker error: {e}")

    def _write_chunk_sync(self, transfer_id, data):
        """同步写入逻辑 (运行在 Executor 线程中)"""
        if transfer_id in self.incoming_transfers:
            task = self.incoming_transfers[transfer_id]
            try:
                if isinstance(data, str):
                    chunk_data = base64.b64decode(data)
                else:
                    chunk_data = data
                task["file"].write(chunk_data)
                task["received"] += len(chunk_data)
            except Exception as e:
                SLog.e(TAG, f"Write error: {e}")

    async def _finish_transfer(self, transfer_id):
        """处理传输完成逻辑"""
        if transfer_id in self.incoming_transfers:
            task = self.incoming_transfers.pop(transfer_id)
            try:
                task["file"].close()
            except:
                pass

            # 重命名 .part 为正式文件
            if task["part_path"].exists():
                final_path = task["path"]
                counter = 1
                while final_path.exists():
                    stem = final_path.stem
                    suffix = final_path.suffix
                    final_path = final_path.parent / f"{stem}_{counter}{suffix}"
                    counter += 1

                # 重试机制解决 Windows 文件占用
                for i in range(5):
                    try:
                        task["part_path"].rename(final_path)
                        SLog.i(TAG, f"File transfer complete: {final_path}")
                        break
                    except OSError:
                        if i == 4: SLog.e(TAG, f"Failed to rename {task['part_path']} (Locked?)")
                        await asyncio.sleep(0.5)

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

                pending_futures = set()  # 存储正在发送的 Future
                MAX_PENDING = 8  # 允许并发 8 个分片 (8 * 512KB = 4MB 缓冲区)，跑满带宽的关键

                index = int(offset / self.CHUNK_SIZE)
                while True:
                    chunk = f.read(self.CHUNK_SIZE)
                    if not chunk:
                        break

                    # Binary Protocol: Magic(1)|Type(1)|SN_Len(1)|SN|TID_Len(1)|TID|Data
                    sn_bytes = target_sn.encode('utf-8')
                    tid_bytes = transfer_id.encode('utf-8')

                    header = struct.pack(
                        f'!BBB{len(sn_bytes)}sB{len(tid_bytes)}s',
                        0xAA, 0x01, len(sn_bytes), sn_bytes, len(tid_bytes), tid_bytes
                    )

                    payload = header + chunk

                    # 检查连接状态，避免死循环
                    if not self.client.websocket:
                        raise ConnectionError("WebSocket disconnected during transfer")

                    future = asyncio.run_coroutine_threadsafe(
                        self.client.websocket.send(payload),
                        self.client.loop
                    )

                    # [修复] 流水线发送：不立即等待，而是放入集合
                    pending_futures.add(future)

                    # 清理已完成的任务
                    done_futures = {f for f in pending_futures if f.done()}
                    pending_futures -= done_futures
                    for f in done_futures: f.result()  # 检查异常

                    # 只有当积压过多时才等待，保证管道始终有数据在跑
                    if len(pending_futures) >= MAX_PENDING:
                        done, pending_futures = concurrent.futures.wait(
                            pending_futures,
                            return_when=concurrent.futures.FIRST_COMPLETED
                        )

                    index += 1

                    # --- Progress Reporting ---
                    total_sent += len(chunk)
                    now = time.time()
                    if now - last_report_time > 1.0:  # Report every 1s
                        duration = now - start_time
                        speed = total_sent / duration if duration > 0 else 0
                        progress = ((offset + total_sent) / file_size * 100) if file_size > 0 else 0

                        report_payload = {
                            "action": "transfer_progress",
                            "data": {
                                "transfer_id": transfer_id,
                                "progress": round(progress, 1),
                                "speed": int(speed),  # bytes/s
                                "status": "transferring"
                            }
                        }
                        asyncio.run_coroutine_threadsafe(self.client.websocket.send(json.dumps(report_payload)),
                                                         self.client.loop)
                        last_report_time = now

            # 等待剩余分片发送完毕
            if pending_futures:
                concurrent.futures.wait(pending_futures)

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
                    except:
                        pass
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
                    source_sn,  # 这里的 source_sn 其实是 target (信号来源是接收方)
                    transfer_id,
                    task["path"],
                    offset
                )

        elif msg_type == "chunk":
            # [接收方] 写入数据 -> 放入队列，立即返回
            await self.write_queue.put(("chunk", transfer_id, data.get("data")))

        elif msg_type == "finish":
            # [接收方] 完成 -> 放入队列
            await self.write_queue.put(("finish", transfer_id, None))

    async def handle_binary_chunk(self, transfer_id, data):
        await self.write_queue.put(("chunk", transfer_id, data))


from dataclasses import dataclass
from typing import Optional


@dataclass
class StreamConfig:
    resolution: int = 0  # 0 = 原生分辨率 (不限制), 2560 = 2K, 3840 = 4K
    bitrate: int = 4000000  # 默认 8Mbps (适合 1080p/2K)
    fps: int = 5  # 帧率
    encoder: Optional[str] = None  # None = 自动选择 (推荐), 指定字符串则强制使用

    @classmethod
    def preset_native_smooth(cls):
        """原生分辨率，高码率，流畅"""
        # return cls(resolution=0, bitrate=10000000, fps=60)
        return cls(resolution=1080, bitrate=4000000, fps=15)

    @classmethod
    def preset_2k_quality(cls):
        """2K 分辨率，画质优先"""
        return cls(resolution=2560, bitrate=12000000, fps=30)

    @classmethod
    def preset_4k_ultra(cls):
        """4K 超高清 (需手机硬件支持)"""
        return cls(resolution=3840, bitrate=20000000, fps=30)

    @classmethod
    def preset_compatibility(cls):
        """兼容模式 (类似你之前的救砖配置)"""
        return cls(resolution=1024, bitrate=1000000, fps=30, encoder="c2.android.avc.encoder")


class StreamManager:
    """
    [新增] 流媒体管理器
    负责将连接在 PC 上的 Android 设备屏幕画面，通过 WebSocket 推流给远程控制端 (Launcher)
    """

    def __init__(self, client):
        self.client = client
        # { target_sn (viewer): subprocess }
        self.active_streams = {}
        self.is_streaming = False  # [新增] 全局流状态标志

    async def start_stream(self, device_sn, viewer_sn, config: StreamConfig = None):
        """
        [修复 & 增强] 支持高清、低延迟配置
        """
        # 如果没有传入配置，使用默认的高清配置
        if config is None:
            config = StreamConfig.preset_native_smooth()

        if viewer_sn in self.active_streams:
            SLog.w(TAG, f"Stream to {viewer_sn} already exists. Force restarting...")
            old_process = self.active_streams.get(viewer_sn)
            if old_process:
                try:
                    old_process.terminate()
                    try:
                        old_process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        old_process.kill()
                except Exception as e:
                    SLog.e(TAG, f"Error killing old stream: {e}")
            self.active_streams.pop(viewer_sn, None)
            await asyncio.sleep(0.5)

        SLog.i(TAG, f"Starting stream: {device_sn} -> {viewer_sn} | Config: {config}")

        # 将 config 传给线程
        thread = threading.Thread(target=self._start_stream_sync, args=(device_sn, viewer_sn, config))
        thread.daemon = True
        thread.start()

    def _start_stream_sync(self, device_sn, viewer_sn, config: StreamConfig):
        """
        同步方法：启动 ADB 录屏并持续读取 stdout 发送
        [最终方案 v5] 针对 Android 12 + 华为设备的终极救砖配置：
                     1. 使用 c2.android.avc.encoder (更现代的软编)
                     2. 限制码率 500kbps (防止软编过载卡死)
                     3. 保持 Hex SCID 和版本号参数
        """
        self.is_streaming = True  # 开始时置为 True
        adb_path = get_adb_path().strip('"')
        scrcpy_server_path = get_scrcpy_server_path().strip('"')

        if not os.path.exists(scrcpy_server_path):
            SLog.e(TAG, f"Scrcpy server file not found: {scrcpy_server_path}")
            return

        # 协议头
        sn_bytes = viewer_sn.encode('utf-8')
        header = struct.pack(f'!BBB{len(sn_bytes)}s', 0xAA, 0x02, len(sn_bytes), sn_bytes)

        SLog.i(TAG, f"Stream thread started for {viewer_sn}")

        def _recv_exact(sock, n):
            data = b''
            while len(data) < n:
                chunk = sock.recv(n - len(data))
                if not chunk: raise ConnectionError(f"Socket closed")
                data += chunk
            return data

        def _get_free_port():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', 0))
                return s.getsockname()[1]

        def _fetch_logcat_error():
            try:
                cmd = [adb_path, "-s", device_sn, "shell", "logcat -d -t 200"]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                return res.stdout.strip()
            except:
                return "Failed to fetch logcat."

        # 在推流开始前，强制 Android 屏幕常亮 (Stay On when Plugged in)
        # 3 = Charging via USB, 7 = Any power source
        SLog.i(TAG, f"Enable Stay-Awake for {device_sn}")
        subprocess.run([adb_path, "-s", device_sn, "shell", "svc power stayon true"], stdout=subprocess.DEVNULL)

        # 1. 初始化
        device_server_path = "/data/local/tmp/scrcpy-server.jar"
        local_port = _get_free_port()

        # Hex SCID
        scid_int = random.randint(10000000, 99999999) & 0x7FFFFFFF
        scid_hex = f"{scid_int:08x}"

        socket_name = f"scrcpy_{scid_int:08x}"
        SLog.i(TAG, f"Generated SCID: {scid_hex} -> Local Port: {local_port}")

        process = None
        video_socket = None
        forward_created = False

        try:
            # 推送 & 清理
            subprocess.run([adb_path, "-s", device_sn, "push", scrcpy_server_path, device_server_path],
                           stdout=subprocess.DEVNULL)
            subprocess.run([adb_path, "-s", device_sn, "shell", "logcat -c"], stdout=subprocess.DEVNULL)

            # 转发
            subprocess.run([adb_path, "-s", device_sn, "forward", f"tcp:{local_port}", f"localabstract:{socket_name}"],
                           check=True)
            forward_created = True

            # 2. 启动 Server (高稳定性配置)
            server_args = [
                f"scid={scid_hex}",
                "log_level=info",
                "video=true", "audio=false", "control=false",
                "video_codec=h264",
                # "video_bit_rate=500000",  # 🔥 关键修改: 限制 500Kbps，防止 CPU 软解卡死
                f"video_bit_rate={config.bitrate}",
                f"max_fps={config.fps}",
                "tunnel_forward=true",
                "send_frame_meta=true", "send_device_meta=true", "send_codec_meta=false", "send_dummy_byte=true",
                "i_frame_interval=2",
            ]
            # 3. 动态分辨率 (解决模糊问题)
            # 如果 config.resolution 为 0，则不传 max_size，scrcpy 默认使用原生分辨率
            if config.resolution > 0:
                server_args.append(f"max_size={config.resolution}")

            # 4. 编码器选择 (解决延迟/卡顿问题)
            # 除非 config 明确指定了 encoder (比如为了救砖)，否则不要传 video_encoder 参数
            # scrcpy-server 会自动寻找最佳的硬件编码器 (如 OMX.qcom.video.encoder.avc)
            if config.encoder:
                SLog.w(TAG, f"Force using encoder: {config.encoder}")
                server_args.append(f"video_encoder={config.encoder}")

            # 保持 "3.3.3" 版本号参数
            shell_cmd = f"CLASSPATH={device_server_path} app_process / com.genymobile.scrcpy.Server 3.3.3 {' '.join(server_args)}"
            full_cmd = [adb_path, "-s", device_sn, "shell", shell_cmd]

            SLog.i(TAG, f"Launching: {full_cmd}")
            process = subprocess.Popen(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.active_streams[viewer_sn] = process

            time.sleep(1.5)

            # 3. 连接
            connected = False
            for i in range(10):
                if viewer_sn not in self.active_streams: break

                if process.poll() is not None:
                    SLog.e(TAG, "🔥 Scrcpy Died Early")
                    SLog.e(TAG, f"👉 LOGCAT:\n{_fetch_logcat_error()}")
                    break

                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(2.0)
                    sock.connect(('127.0.0.1', local_port))

                    dummy = sock.recv(1)
                    if not dummy:
                        SLog.e(TAG, "Socket closed immediately.")
                        sock.close()
                        SLog.e(TAG, f"👉 LOGCAT:\n{_fetch_logcat_error()}")
                        return

                    name = _recv_exact(sock, 64)
                    sock.settimeout(None)
                    video_socket = sock
                    connected = True
                    SLog.i(TAG, f"Connected! Device Name: {name.decode('utf-8', 'ignore').strip()}")
                    break

                except (ConnectionRefusedError, socket.timeout, OSError):
                    if 'sock' in locals() and sock: sock.close()
                    time.sleep(0.5)

            if not connected:
                if process.poll() is None:
                    SLog.e(TAG, f"Timeout. Logs:\n{_fetch_logcat_error()}")
                return

            # 4. 转发循环
            while viewer_sn in self.active_streams and self.is_streaming:
                try:
                    # 读取 Meta
                    meta = _recv_exact(video_socket, 12)
                    pts, packet_size = struct.unpack("!QI", meta)

                    if packet_size > 30 * 1024 * 1024:
                        SLog.w(TAG, f"Packet too large: {packet_size}, potential error.")
                        break

                    # 读取数据
                    data = _recv_exact(video_socket, packet_size)
                    payload = header + data

                    # 发送
                    if self.client.websocket:
                        future = asyncio.run_coroutine_threadsafe(
                            self.client.websocket.send(payload),
                            self.client.loop
                        )
                        # 可选：不等待结果以提高吞吐量，或等待以捕获错误
                    else:
                        break
                except Exception as e:
                    SLog.e(TAG, f"Forward Loop Error: {e}")
                    break

        except Exception as e:
            SLog.e(TAG, f"Stream Error: {e}")

        finally:
            # [恢复] 允许屏幕休眠
            self.is_streaming = False  # 确保退出
            subprocess.run([adb_path, "-s", device_sn, "shell", "svc power stayon false"], stdout=subprocess.DEVNULL)
            if video_socket: video_socket.close()
            if forward_created:
                try:
                    subprocess.run([adb_path, "-s", device_sn, "forward", "--remove", f"tcp:{local_port}"],
                                   stderr=subprocess.DEVNULL)
                except:
                    pass
            if process:
                try:
                    process.terminate(); process.wait(timeout=1)
                except:
                    pass
            if viewer_sn in self.active_streams: self.active_streams.pop(viewer_sn, None)
            SLog.i(TAG, f"Stream ended: {viewer_sn}")

    def stop_stream(self, viewer_sn):
        """停止推流"""
        self.is_streaming = False  # [关键] 立即置为 False
        if viewer_sn in self.active_streams:
            proc = self.active_streams.pop(viewer_sn)
            if proc:
                try:
                    proc.terminate()
                    # 不在此处 wait，由 _start_stream_sync 线程负责 wait，避免阻塞主线程
                except Exception:
                    pass
            SLog.i(TAG, f"Stopped stream for {viewer_sn}")

    async def handle_command(self, command, params):
        """处理流媒体相关指令"""
        if command == "start_stream":
            # 远程 Launcher 请求看某个设备的屏幕
            device_sn = params.get("device_sn")  # PC 上插着的手机
            viewer_sn = params.get("viewer_sn")  # 发起请求的 Launcher
            if device_sn and viewer_sn:
                await self.start_stream(device_sn, viewer_sn)

        elif command == "stop_stream":
            viewer_sn = params.get("viewer_sn")
            if viewer_sn:
                self.stop_stream(viewer_sn)

    # [解决 Point 5: 控制接口]
    def handle_control(self, params):
        """
        处理来自 iOS 的控制指令，通过 ADB 执行
        params: { "target_sn": "...", "action": "click/swipe", "x": 100, "y": 200, ... }
        """
        SLog.i("handle_control", params)
        device_sn = params.get("target_sn")
        action = params.get("action")
        adb_path = get_adb_path().strip('"')

        if not device_sn: return

        # 使用 asyncio.create_task 或 线程池执行，避免阻塞主线程
        threading.Thread(target=self._exec_adb_input, args=(adb_path, device_sn, action, params)).start()

    def _exec_adb_input(self, adb, sn, action, params):
        try:
            if action == "click" or action == "tap":
                x, y = params.get("x"), params.get("y")
                SLog.i("", f"{x} {y}")
                SLog.i("", f"{x} {y}")
                # ADB 命令: input tap <x> <y>
                subprocess.run([adb, "-s", sn, "shell", "input", "tap", str(x), str(y)])

            elif action == "swipe":
                x1, y1 = params.get("x1"), params.get("y1")
                x2, y2 = params.get("x2"), params.get("y2")
                duration = params.get("duration", 300)  # ms
                # ADB 命令: input swipe <x1> <y1> <x2> <y2> <duration>
                subprocess.run(
                    [adb, "-s", sn, "shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration)])

            elif action == "text":
                context = params.get("text")
                # ADB 命令: input text <text>
                subprocess.run(
                    [adb, "-s", sn, "shell", "input", "text", str(context)])

            elif action == "keyevent":
                keyevent = params.get("keyevent")
                # ADB 命令: input text <text>
                subprocess.run(
                    [adb, "-s", sn, "shell", "input", "keyevent", str(keyevent)])

        except Exception as e:
            SLog.e(TAG, f"Control error: {e}")


class NetworkSelector:
    """网络竞速器：并发测试多个节点，返回最快的一个"""

    @staticmethod
    async def measure_latency(url, timeout=2.0):
        try:
            parsed = urlparse(url)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == 'wss' else 80)

            start_time = time.time()
            # 建立 TCP 连接 (比 WS 握手更轻量)
            waiter = asyncio.open_connection(host, port)
            # 分离 reader, writer，设置超时
            reader, writer = await asyncio.wait_for(waiter, timeout)

            # 🔥 [修复核心] 只要连上就算成功，立即计算耗时
            latency = (time.time() - start_time) * 1000

            # 2. 独立的清理块 (即使这里报错也不影响结果)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                # Uvicorn 可能会因为非法 HTTP 请求主动断开，忽略此错误
                pass

            # SLog.d("NetworkSelector", f"Ping {host}: {latency:.1f}ms")
            return url, latency
        except Exception:
            return url, 99999

    @staticmethod
    async def select_best_url(urls: list):
        if not urls: return None
        SLog.i("NetworkSelector", f"{urls}")
        # 如果只有一个地址，直接返回，不浪费时间测速
        if len(urls) == 1: return urls[0]

        SLog.i("NetworkSelector", f"Racing {len(urls)} candidates...")
        tasks = [NetworkSelector.measure_latency(url) for url in urls]
        results = await asyncio.gather(*tasks)

        # 过滤超时 (99999) 的地址
        valid = [r for r in results if r[1] < 90000]
        if not valid:
            return None

        # 按延迟排序
        valid.sort(key=lambda x: x[1])
        best_url, best_latency = valid[0]
        SLog.i("NetworkSelector", f"🏆 Winner: {best_url} ({best_latency:.1f}ms)")
        return best_url


class DeviceClient:
    def __init__(self, candidates, sn, role="node", shared_responses=None, token=None):
        if isinstance(candidates, str):
            self.candidate_urls = [candidates]
        else:
            self.candidate_urls = candidates or []
        self.sn = sn
        
        self.role = role
        self.token = token

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
        self.msg_queue = multiprocessing.Queue()  # 进程间通信队列

        # 初始化文件传输管理器
        self.file_transfer = FileTransferManager(self)
        # [新增] 初始化流媒体管理器
        self.stream_manager = StreamManager(self)
        self.current_connected_url = None
        self.connected_event = asyncio.Event()  # 🔥 [新增] 连接状态事件

    def _resolve_server_http_url(self) -> str:
        """子进程 REMOTE_API_URL：由当前 WS 地址推导 HTTP 根路径。"""
        ws_url = self.current_connected_url
        if not ws_url and self.candidate_urls:
            ws_url = self.candidate_urls[0]
        if not ws_url:
            ws_url = "ws://127.0.0.1:10104/ws"
        http_url = ws_url.replace("wss://", "https://").replace("ws://", "http://")
        if http_url.endswith("/ws"):
            http_url = http_url[:-3]
        return http_url.rstrip("/")

    def stop(self):
        """停止客户端，清理所有子进程和任务 (线程安全版)"""
        SLog.i(TAG, "Stopping DeviceClient...")
        self.is_running = False
        self.connected_event.clear()  # 🔥 [新增] 重置状态

        # 1. 安全关闭 WebSocket
        if self.websocket:
            try:
                # 方案 A: 如果当前就在异步线程里 (比如被 async 函数调用)
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.websocket.close())
                except RuntimeError:
                    # 方案 B: 如果当前在同步线程里 (比如 Restart 线程)
                    # 我们需要把它"抛"回 start() 所在的那个 loop 去执行
                    if hasattr(self, 'loop') and self.loop and self.loop.is_running():
                        asyncio.run_coroutine_threadsafe(self.websocket.close(), self.loop)
                    else:
                        SLog.w(TAG, "Cannot close websocket gracefully: No running loop found.")
            except Exception as e:
                SLog.w(TAG, f"Error closing websocket: {e}")

        # 2. 停止所有流媒体进程
        if hasattr(self, 'stream_manager'):
            for viewer_sn in list(self.stream_manager.active_streams.keys()):
                self.stream_manager.stop_stream(viewer_sn)

        SLog.i(TAG, "DeviceClient stopped.")

    async def start(self):
        """启动客户端主循环"""
        self.is_running = True
        SLog.i(TAG, f"Device Client Starting... SN: {self.sn}")

        while self.is_running:
            try:
                self.loop = asyncio.get_running_loop()  # 捕获当前 loop 供线程使用

                if not self.candidate_urls:
                    SLog.w(TAG, "No candidate URLs provided. Sleeping...")
                    await asyncio.sleep(5)
                    continue

                target_url = await NetworkSelector.select_best_url(self.candidate_urls)

                if not target_url:
                    SLog.w(TAG, "All networks unreachable. Retrying in 5s...")
                    self.current_connected_url = None  # 更新状态
                    await asyncio.sleep(5)
                    continue

                # 构造带 Token 的最终 URL
                connect_url = target_url
                if self.token and "token=" not in connect_url:
                    sep = "&" if "?" in connect_url else "?"
                    connect_url = f"{connect_url}{sep}token={self.token}"

                # ---------------------------------------------------------
                # Step 2: 建立连接
                # ---------------------------------------------------------
                SLog.i(TAG, f"Connecting to {target_url}...")

                async with websockets.connect(connect_url) as ws:
                    self.websocket = ws
                    self.current_connected_url = target_url  # 更新状态给 UI 看
                    self.connected_event.set()  # 🔥 [新增] 标记为已连接

                    SLog.i(TAG, "Connected to server.")

                    # 1. 发送注册包
                    if await self.register():
                        # [新增] 启动文件传输后台消费者
                        self.file_transfer.start()

                        # 2. 注册成功后，并发运行 心跳任务 和 消息监听任务
                        tasks = [
                            asyncio.create_task(self.heartbeat_loop()),
                            asyncio.create_task(self.listen_loop()),
                            asyncio.create_task(self.queue_consumer_loop())
                        ]
                        # 只要有一个任务退出（如连接断开），就终止所有任务并重连
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

                        for task in tasks:
                            if not task.done():
                                task.cancel()

                        # 确保所有任务都已清理
                        await asyncio.gather(*tasks, return_exceptions=True)
            # ---------------------------------------------------------
            # Step 3: 异常处理与重试
            # ---------------------------------------------------------
            except (websockets.ConnectionClosed, ConnectionRefusedError, OSError) as e:
                SLog.w(TAG, f"❌ Connection lost ({type(e).__name__}). Switching network...")
                self.websocket = None
                self.current_connected_url = None
                # 这里不 sleep 太久，立即尝试寻找下一个可用网络
                await asyncio.sleep(1)
            except Exception as e:
                SLog.e(TAG, f"Unexpected error: {e}")
                await asyncio.sleep(5)
            finally:
                self.websocket = None
                self.connected_event.clear()  # 🔥 [新增] 标记为断开

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
                for line in lines[1:]:  # 跳过第一行 List of devices attached
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
                    if not line.strip():
                        continue
                    # tidevice list 首行常为表头: "UDID Name" / "UDID SerialNumber"
                    parts = line.split()
                    if len(parts) < 1:
                        continue
                    udid = parts[0]
                    if udid.upper() in ("UDID", "NAME", "SERIALNUMBER"):
                        continue
                    from server.services.device_service import is_valid_sn

                    if not is_valid_sn(udid):
                        continue
                    name = parts[1] if len(parts) > 1 else "iOS Device"
                    if name.upper() in ("NAME", "SERIALNUMBER", "UDID"):
                        name = "iOS Device"
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
            pass  # tidevice 可能未安装，忽略

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
                if isinstance(message, bytes):
                    await self.handle_binary_message(message)
                else:
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
                    elif msg["type"] == "crawl_complete":
                        payload = {
                            "action": "crawl_complete",
                            "data": {
                                "req_id": msg.get("req_id"),
                                "payload": msg.get("data"),
                            },
                        }
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
                elif command == "crawl_app":
                    self.execute_crawl(params)
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

                # --- 新增流媒体指令 ---
                elif command in ["start_stream", "stop_stream"]:
                    await self.stream_manager.handle_command(command, params)

                elif command == "exit_node_mode":
                    # [新增] 退出 Node 模式，恢复为独立 Server
                    self.reset_to_server_mode()

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
                elif command == "control":
                    # 服务端转发来的控制指令 -> 交给 StreamManager 处理
                    self.stream_manager.handle_control(params)
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

    async def handle_binary_message(self, message):
        """处理二进制消息 (文件流)"""
        try:
            # Protocol: Magic(1)|Type(1)|SN_Len(1)|SN|TID_Len(1)|TID|Data
            if len(message) < 5: return

            magic, mtype, sn_len = struct.unpack_from('!BBB', message, 0)
            if magic != 0xAA: return

            offset = 3
            # target_sn = message[offset:offset+sn_len].decode('utf-8') # Routing info
            offset += sn_len

            tid_len = message[offset]
            offset += 1
            transfer_id = message[offset:offset + tid_len].decode('utf-8')
            offset += tid_len

            data = message[offset:]

            if mtype == 0x01:  # Chunk
                await self.file_transfer.handle_binary_chunk(transfer_id, data)
        except Exception as e:
            SLog.e(TAG, f"Binary message error: {e}")

    def execute_task(self, params):
        """在独立进程中执行任务"""
        run_id = params.get("run_id")
        flow_id = params.get("flow_id")

        if not (run_id and flow_id):
            SLog.e(TAG, "Missing task parameters (run_id, flow_id)")
            return

        SLog.i(TAG, f"Spawning process for task RunID: {run_id} TargetSN: {params.get('target_sn')}")

        http_url = self._resolve_server_http_url()

        # 使用 multiprocessing 启动任务，避免阻塞 WebSocket 通信
        p = multiprocessing.Process(
            target=process_runner_wrapper,
            args=(run_id, flow_id, self.msg_queue, http_url, self.shared_responses, params)
        )
        p.start()

    def execute_crawl(self, params):
        """在设备节点子进程中跑图（ADB 在本机）。"""
        req_id = params.get("req_id")
        if not req_id:
            SLog.e(TAG, "crawl_app missing req_id")
            return

        SLog.i(TAG, f"Spawning crawl process req_id={req_id}")
        http_url = self._resolve_server_http_url()
        from driver.agent.Crawl.crawl_runner import crawl_runner_wrapper

        p = multiprocessing.Process(
            target=crawl_runner_wrapper,
            args=(params, self.msg_queue, http_url, self.shared_responses),
        )
        p.start()

    def reset_to_server_mode(self):
        """退出 Node 模式，移除 target_url 配置并重启"""
        try:
            # 使用 SecurityManager 的原子清除方法，确保彻底清除
            SecurityManager.clear_cluster_config()
            SLog.i(TAG, "Cluster config cleared, switching back to Server Mode.")
        except Exception as e:
            SLog.e(TAG, f"Failed to update config: {e}")

        SLog.i(TAG, "Restarting application to restore Server Mode...")
        time.sleep(1)
        python = sys.executable
        os.execl(python, python, *sys.argv)

    def _get_device_info(self):
        """收集设备信息"""
        return {
            "sn": self.sn,
            "type": "pc",  # 默认为 PC 节点
            "role": self.role,
            "model": platform.node(),
            "ip": get_local_ip(),
            "os_version": f"{platform.system()} {platform.release()}",
            "mac": self.sn.replace("device_", "")
        }


if __name__ == "__main__":
    # 确保 multiprocessing 在 Windows/macOS 上正常工作
    multiprocessing.freeze_support()

    # 配置代理绕过 (防止连接 ws://miniorange.local 时走代理)
    os.environ["no_proxy"] = os.environ.get("no_proxy", "") + ",localhost,127.0.0.1,::1,miniorange.local,0.0.0.0,100.*"
    os.environ["NO_PROXY"] = os.environ["no_proxy"]

    # 1. 加载配置
    # 🔥 [统一] 直接使用 SecurityManager
    if not SecurityManager._config:
        SecurityManager.load()
    config = SecurityManager._config

    # 获取候选列表：优先读取 'candidate_urls'，如果没有则回退到 'target_url'
    candidate_urls = config.get("candidate_urls", [])

    # 🔥🔥🔥 [修复] 强制类型检查 🔥🔥🔥
    if isinstance(candidate_urls, str):
        candidate_urls = [candidate_urls]

    if not candidate_urls and config.get("target_url"):
        candidate_urls = [config.get("target_url")]

    # 如果配置为空，且本地开启了 Server，则添加本地回环地址作为保底
    if not candidate_urls:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.2)
            if sock.connect_ex(('127.0.0.1', 10104)) == 0:
                # 注意：本地连接通常不需要 token，或者使用默认机制
                candidate_urls.append("ws://127.0.0.1:10104/ws")
            sock.close()
        except:
            pass


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

        # 3. 循环重试逻辑 (包含自动选路)
        # 如果连接断开，我们希望重新进行一次 "选路"，因为网络环境可能变了
        while True:
            if not candidate_urls:
                SLog.w(TAG, "No target URLs configured. Waiting...")
                time.sleep(5)
                # 重新加载配置，也许用户通过 API 更新了配置
                SecurityManager.load()
                config = SecurityManager._config
                
                candidate_urls = config.get("candidate_urls", [])
                continue

            # 🔥 4. 运行竞速选择 (使用 asyncio 运行异步函数)
            SLog.i(TAG, "Starting Network Selection...")
            best_url = asyncio.run(NetworkSelector.select_best_url(candidate_urls))

            if not best_url:
                SLog.e(TAG, "All connection attempts failed. Retrying in 5s...")
                time.sleep(5)
                continue

            # 5. 启动 Client
            # 注意：DeviceClient 内部如果断开连接，会退出 start()
            # 退出后，外层的 while True 会再次触发 NetworkSelector，从而实现环境切换自适应
            client = DeviceClient(best_url, DEVICE_SN, role="node", shared_responses=shared_responses)
            try:
                asyncio.run(client.start())
            except KeyboardInterrupt:
                break
            except Exception as e:
                SLog.e(TAG, f"Client crashed: {e}. Rebooting client logic...")
                time.sleep(3)
