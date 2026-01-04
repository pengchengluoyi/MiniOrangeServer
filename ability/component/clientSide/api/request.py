# !/usr/bin/env python
# -*-coding:utf-8 -*-
import requests
import json
import time
import logging
import requests
from typing import Dict, Any, Optional
from dataclasses import dataclass
from pathlib import Path

from script.log import SLog
from ability.component.template import Template
from ability.component.router import BaseRouter

TAG = "ApiComponent"
# !/usr/bin/env python3
"""
自定义 HTTP 客户端组件 - Python 实现
支持启动服务端和发送自定义 HTTP 请求
"""
import json
import time
import logging
import requests
from typing import Dict, Any, Optional
from dataclasses import dataclass
from pathlib import Path


@dataclass
class HTTPResponse:
    """HTTP 响应数据结构"""
    status_code: int
    headers: Dict[str, str]
    body: str
    response_time: float
    success: bool
    error_message: str = ""
    response_size: int = 0


class CustomHTTPClient:
    """自定义 HTTP 客户端"""

    def __init__(self, config: Dict[str, Any]):
        """
        初始化 HTTP 客户端

        Args:
            config: 配置字典，包含所有输入参数
        """
        self.config = config
        self.session = requests.Session()
        self.setup_logging()

    def setup_logging(self):
        """设置日志"""
        log_level = logging.DEBUG if self.config.get('enable_logging', True) else logging.WARNING
        logging.basicConfig(
            level=log_level,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)

    def prepare_headers(self) -> Dict[str, str]:
        """准备请求头"""
        headers = self.config.get('request_headers', {})

        # 根据请求体类型自动设置 Content-Type
        body_type = self.config.get('request_body_type', 'none')
        if 'Content-Type' not in headers:
            if body_type == 'json':
                headers['Content-Type'] = 'application/json'
            elif body_type == 'form':
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
            elif body_type == 'text':
                headers['Content-Type'] = 'text/plain'
            elif body_type == 'file':
                headers['Content-Type'] = 'application/octet-stream'

        return headers

    def prepare_body(self):
        """准备请求体"""
        body_type = self.config.get('request_body_type', 'none')

        if body_type == 'none':
            return None
        elif body_type == 'json':
            body_str = self.config.get('request_body_json', '{}')
            try:
                return json.loads(body_str)
            except json.JSONDecodeError:
                self.logger.warning(f"无效的JSON格式: {body_str}")
                return body_str
        elif body_type == 'form':
            form_data = self.config.get('request_body_form', {})
            return form_data
        elif body_type == 'text':
            return self.config.get('request_body_text', '')
        elif body_type == 'file':
            file_path = self.config.get('file_path', '')
            if file_path and Path(file_path).exists():
                with open(file_path, 'rb') as f:
                    return f.read()
            else:
                self.logger.error(f"文件不存在: {file_path}")
                raise FileNotFoundError(f"文件不存在: {file_path}")
        return None

    def send_request(self) -> HTTPResponse:
        """
        发送 HTTP 请求

        Returns:
            HTTPResponse: 响应数据
        """
        url = self.config.get('server_url', '')
        method = self.config.get('http_method', 'GET')
        headers = self.prepare_headers()
        body = self.prepare_body()
        timeout = self.config.get('timeout', 30)
        verify_ssl = self.config.get('verify_ssl', True)
        allow_redirects = self.config.get('allow_redirects', True)
        max_retries = self.config.get('max_retries', 0)

        self.logger.info(f"发送 {method} 请求到 {url}")
        self.logger.debug(f"请求头: {headers}")

        if body:
            self.logger.debug(f"请求体: {body}")

        start_time = time.time()

        try:
            # 配置请求参数
            request_kwargs = {
                'method': method,
                'url': url,
                'headers': headers,
                'timeout': timeout,
                'verify': verify_ssl,
                'allow_redirects': allow_redirects
            }

            # 根据请求体类型设置正确的参数
            if body is not None:
                content_type = headers.get('Content-Type', '').lower()
                if 'application/json' in content_type and isinstance(body, (dict, list)):
                    request_kwargs['json'] = body
                elif 'multipart/form-data' in content_type:
                    request_kwargs['files'] = body
                elif isinstance(body, dict):
                    request_kwargs['data'] = body
                else:
                    request_kwargs['data'] = body

            # 重试逻辑
            for attempt in range(max_retries + 1):
                try:
                    response = self.session.request(**request_kwargs)
                    break
                except (requests.exceptions.Timeout,
                        requests.exceptions.ConnectionError) as e:
                    if attempt < max_retries:
                        wait_time = 2 ** attempt  # 指数退避
                        self.logger.warning(f"请求失败，{wait_time}秒后重试: {e}")
                        time.sleep(wait_time)
                    else:
                        raise

            response_time = time.time() - start_time

            # 处理响应
            response_headers = dict(response.headers)
            response_body = response.text
            response_size = len(response.content)

            # 保存响应到文件
            if self.config.get('save_response', False):
                output_file = self.config.get('output_file', 'response.txt')
                self.save_response(output_file, response_body, response_headers)

            success = 200 <= response.status_code < 300

            return HTTPResponse(
                status_code=response.status_code,
                headers=response_headers,
                body=response_body,
                response_time=response_time,
                success=success,
                response_size=response_size
            )

        except Exception as e:
            response_time = time.time() - start_time
            self.logger.error(f"请求失败: {str(e)}")

            return HTTPResponse(
                status_code=0,
                headers={},
                body="",
                response_time=response_time,
                success=False,
                error_message=str(e)
            )

    def save_response(self, file_path: str, body: str, headers: Dict[str, str]):
        """保存响应到文件"""
        try:
            # 尝试解析为 JSON
            try:
                json_data = json.loads(body)
                output_data = {
                    'headers': headers,
                    'body': json_data,
                    'timestamp': time.time()
                }
                with open(file_path, 'w', encoding='utf-8') as f:
                    json.dump(output_data, f, indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                # 如果不是 JSON，保存为纯文本
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(f"Headers:\n{json.dumps(headers, indent=2)}\n\n")
                    f.write(f"Body:\n{body}")

            self.logger.info(f"响应已保存到: {file_path}")
        except Exception as e:
            self.logger.error(f"保存响应失败: {str(e)}")


class HTTPServer:
    """HTTP 服务端实现"""

    def __init__(self, host: str = 'localhost', port: int = 8000):
        """
        初始化 HTTP 服务端

        Args:
            host: 监听地址
            port: 监听端口
        """
        self.host = host
        self.port = port
        self.server = None
        self.logger = logging.getLogger(__name__)

    def start(self):
        """启动 HTTP 服务端"""
        try:
            from http.server import HTTPServer, BaseHTTPRequestHandler
            import threading

            class RequestHandler(BaseHTTPRequestHandler):
                """自定义请求处理器"""

                def do_GET(self):
                    """处理 GET 请求"""
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()

                    response_data = {
                        'status': 'success',
                        'message': 'GET request received',
                        'path': self.path,
                        'method': 'GET',
                        'timestamp': time.time()
                    }

                    self.wfile.write(json.dumps(response_data).encode('utf-8'))

                def do_POST(self):
                    """处理 POST 请求"""
                    content_length = int(self.headers.get('Content-Length', 0))
                    post_data = self.rfile.read(content_length)

                    try:
                        request_body = json.loads(post_data.decode('utf-8'))
                    except:
                        request_body = post_data.decode('utf-8')

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()

                    response_data = {
                        'status': 'success',
                        'message': 'POST request received',
                        'path': self.path,
                        'method': 'POST',
                        'body': request_body,
                        'timestamp': time.time()
                    }

                    self.wfile.write(json.dumps(response_data).encode('utf-8'))

                def log_message(self, format, *args):
                    """自定义日志输出"""
                    self.logger.info(f"{self.address_string()} - {format % args}")

            # 创建服务器
            self.server = HTTPServer((self.host, self.port), RequestHandler)

            # 在单独的线程中启动服务器
            server_thread = threading.Thread(target=self.server.serve_forever)
            server_thread.daemon = True
            server_thread.start()

            self.logger.info(f"HTTP 服务器已启动: http://{self.host}:{self.port}")
            return True

        except Exception as e:
            self.logger.error(f"启动 HTTP 服务器失败: {str(e)}")
            return False

    def stop(self):
        """停止 HTTP 服务端"""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.logger.info("HTTP 服务器已停止")
        self.server = None


@BaseRouter.route('api/request')
class ApiRequest(Template):
    """
        This component will
    """

    META = {
        "name": "custom_http_client",
        "desc": "自定义 HTTP 客户端",
        "version": "1.0.0",
        "inputs": [
            {
                "name": "server_url",
                "type": "str",
                "desc": "服务器地址",
                "required": True,
                "defaultValue": "http://localhost:8000",
                "placeholder": "例如: http://localhost:8000 或 https://api.example.com"
            },
            {
                "name": "http_method",
                "type": "select",
                "desc": "HTTP 方法",
                "options": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                "defaultValue": "GET"
            },
            {
                "name": "request_headers",
                "type": "keyValue",
                "desc": "请求头",
                "defaultValue": {
                    "Content-Type": "application/json",
                    "User-Agent": "Custom-HTTP-Client/1.0"
                },
                "keyPlaceholder": "Header名称",
                "valuePlaceholder": "Header值",
                "allowEmpty": True
            },
            {
                "name": "request_body_type",
                "type": "select",
                "desc": "请求体类型",
                "options": [
                    {"label": "JSON", "value": "json"},
                    {"label": "表单数据", "value": "form"},
                    {"label": "文本", "value": "text"},
                    {"label": "文件上传", "value": "file"},
                    {"label": "无请求体", "value": "none"}
                ],
                "defaultValue": "json",
                "condition": {
                    "field": "http_method",
                    "value": ["POST", "PUT", "PATCH"]
                }
            },
            {
                "name": "request_body_json",
                "type": "text",
                "desc": "JSON 请求体",
                "rows": 6,
                "placeholder": "{\"key\": \"value\"}",
                "condition": {
                    "field": "request_body_type",
                    "value": "json"
                },
                "validation": {
                    "type": "json",
                    "message": "请输入有效的 JSON 格式"
                }
            },
            {
                "name": "request_body_form",
                "type": "keyValue",
                "desc": "表单数据",
                "defaultValue": {
                    "key1": "value1",
                    "key2": "value2"
                },
                "condition": {
                    "field": "request_body_type",
                    "value": "form"
                }
            },
            {
                "name": "request_body_text",
                "type": "text",
                "desc": "文本请求体",
                "rows": 4,
                "defaultValue": "Hello World",
                "condition": {
                    "field": "request_body_type",
                    "value": "text"
                }
            },
            {
                "name": "file_path",
                "type": "str",
                "desc": "文件路径",
                "placeholder": "/path/to/file.txt",
                "condition": {
                    "field": "request_body_type",
                    "value": "file"
                }
            },
            {
                "name": "timeout",
                "type": "int",
                "desc": "超时时间(秒)",
                "defaultValue": 30,
                "min": 1,
                "max": 300
            },
            {
                "name": "verify_ssl",
                "type": "bool",
                "desc": "验证SSL证书",
                "defaultValue": True,
                "trueText": "验证",
                "falseText": "不验证"
            },
            {
                "name": "allow_redirects",
                "type": "bool",
                "desc": "允许重定向",
                "defaultValue": True,
                "trueText": "允许",
                "falseText": "不允许"
            },
            {
                "name": "max_retries",
                "type": "int",
                "desc": "最大重试次数",
                "defaultValue": 0,
                "min": 0,
                "max": 10
            },
            {
                "name": "save_response",
                "type": "bool",
                "desc": "保存响应到文件",
                "defaultValue": False,
                "trueText": "保存",
                "falseText": "不保存"
            },
            {
                "name": "output_file",
                "type": "str",
                "desc": "输出文件路径",
                "placeholder": "/path/to/output.json",
                "condition": {
                    "field": "save_response",
                    "value": True
                }
            },
            {
                "name": "enable_logging",
                "type": "bool",
                "desc": "启用详细日志",
                "defaultValue": True,
                "trueText": "启用",
                "falseText": "禁用"
            }
        ],
        "defaultData": {
            "server_url": "http://localhost:8000",
            "http_method": "GET",
            "request_headers": {
                "Content-Type": "application/json",
                "User-Agent": "Custom-HTTP-Client/1.0"
            },
            "request_body_type": "json",
            "request_body_json": "{}",
            "request_body_form": {},
            "request_body_text": "",
            "file_path": "",
            "timeout": 30,
            "verify_ssl": True,
            "allow_redirects": True,
            "max_retries": 0,
            "save_response": False,
            "output_file": "",
            "enable_logging": True
        },
        "outputVars": [
            {"key": "status_code", "type": "int", "desc": "HTTP状态码"},
            {"key": "response_headers", "type": "object", "desc": "响应头"},
            {"key": "response_body", "type": "str", "desc": "响应体"},
            {"key": "response_time", "type": "float", "desc": "响应时间(秒)"},
            {"key": "success", "type": "bool", "desc": "请求是否成功"},
            {"key": "error_message", "type": "str", "desc": "错误信息"},
            {"key": "response_size", "type": "int", "desc": "响应大小(字节)"}
        ]
    }

    def on_check(self):
        ...

    def execute(self):
        config = {'timeout': 30}
        http_method = self.get_param_value("http_method")
        config["server_url"] = self.get_param_value("server_url")
        config["http_method"] = http_method

        if http_method == "POST":
            config["request_body_type"]  = self.get_param_value("request_body_type")
            config["request_headers"] = self.get_param_value("request_headers")

        # 创建 HTTP 客户端
        client = CustomHTTPClient(config)

        # 发送请求
        response = client.send_request()


        if not response.status_code:
            self.result.fail()
        else:
            self.result.success()
        self.memory.set(self.info, "status_code", response.status_code)
        return self.result