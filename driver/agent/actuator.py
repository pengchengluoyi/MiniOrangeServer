import traceback
import os
from script.log import SLog, current_run_id, current_flow_id
from server.core.log_database import LogSessionLocal
from server.models.log import WorkflowLog
from driver.core.manager import Manager
from server.services import run_service
from script.mTask import report



# 1. 定义写入数据库的具体逻辑
# 这个函数会在子进程中被 SLog 调用
def _db_writer(run_id, flow_id, node_id, level, tag, message):
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
        print(f"Log Write Error: {e}")
    finally:
        db.close()


# 2. 包装器函数
def process_runner_wrapper(run_data, run_id, flow_id):
    """
    这是一个运行在子进程中的 wrapper。
    它负责初始化环境，然后执行真正的业务脚本。
    """
    # --- A. 初始化 SLog 回调 ---
    # 在这个新进程里，把写入数据库的能力注入给 SLog
    SLog.set_log_callback(_db_writer)

    # --- B. 设置上下文 ---
    # 让后续的 SLog.i() 知道当前的 ID
    token_run = current_run_id.set(run_id)
    token_flow = current_flow_id.set(str(flow_id))

    try:
        SLog.i("System", "start")
        SLog.i("System", f"任务进程启动 PID:{os.getpid()}")
        SLog.i("System", f"输入数据{run_data}")

        run_service.create_run()
        # --- C. 执行真正的业务脚本 ---
        runner = Manager(run_data)
        runner.run()

        run_service.finish_run("success", report)
    except Exception as e:
        run_service.finish_run("failed", report)
        error_msg = traceback.format_exc()
        SLog.e("System", f"任务异常崩溃: {error_msg}")
        SLog.i("System", "error")
    finally:
        # 清理上下文
        current_run_id.reset(token_run)
        current_flow_id.reset(token_flow)
        SLog.i("System", "end")