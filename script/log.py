import logging
import os
import threading
from datetime import datetime
import contextvars  # <--- [新增]

# ================= 1. 定义上下文变量 =================
# 这些变量是线程/协程安全的，用来存当前的运行ID
current_run_id = contextvars.ContextVar("run_id", default=None)
current_flow_id = contextvars.ContextVar("flow_id", default=None)
current_node_id = contextvars.ContextVar("node_id", default=None)

# ================= 2. 日志格式化 (保持你原来的) =================
RESET = "\033[0m"
RED = "\033[91m"
YELLOW = "\033[93m"
WHITE = "\033[37m"
LIGHT_WHITE = "\033[97m"

class LogFormatter(logging.Formatter):
    def format(self, record):
        date_str = datetime.now().strftime("%m-%d %H:%M:%S.") + datetime.now().strftime("%f")[:3]
        process_id = os.getpid()
        thread_id = threading.get_ident()
        level = record.levelname[0]
        tag = getattr(record, 'tag', 'default_tag')

        if level == 'D': color = WHITE
        elif level == 'I': color = LIGHT_WHITE
        elif level == 'W': color = YELLOW
        elif level == 'E': color = RED
        else: color = RESET

        return f"{color}{date_str}  {process_id}  {thread_id} {level} {tag}: {record.getMessage()}{RESET}"

# 配置基础 Logger (只负责控制台输出)
logger = logging.getLogger("AndroidLog")
logger.setLevel(logging.DEBUG)
console_handler = logging.StreamHandler()
console_handler.setFormatter(LogFormatter())
logger.addHandler(console_handler)

# ================= 3. SLog 类改造 =================
class SLog:
    # 定义一个类变量，用来存回调函数
    _db_write_callback = None

    @classmethod
    def set_log_callback(cls, callback):
        """
        供外部(主进程或子进程Wrapper)调用，注入写入数据库的方法
        """
        cls._db_write_callback = callback

    @classmethod
    def _log(cls, level, tag, message):
        # --- A. 原有的控制台输出 ---
        extra = {'tag': tag}
        if level == 'debug': logger.debug(message, extra=extra)
        elif level == 'info': logger.info(message, extra=extra)
        elif level == 'warning': logger.warning(message, extra=extra)
        elif level == 'error': logger.error(message, extra=extra)

        # --- B. [新增] 数据库回调逻辑 ---
        # 只有当 set_log_callback 被调用过，且上下文里有 run_id 时才写入
        run_id = current_run_id.get()
        if cls._db_write_callback and run_id:
            try:
                flow_id = current_flow_id.get()
                node_id = current_node_id.get()
                # 调用注入的回调函数
                cls._db_write_callback(run_id, flow_id, node_id, level.upper(), tag, message)
            except Exception:
                pass # 忽略日志写入错误，防止影响业务

    # 静态方法接口保持不变
    @staticmethod
    def d(tag, message): SLog._log('debug', tag, message)
    @staticmethod
    def i(tag, message): SLog._log('info', tag, message)
    @staticmethod
    def w(tag, message): SLog._log('warning', tag, message)
    @staticmethod
    def e(tag, message): SLog._log('error', tag, message)