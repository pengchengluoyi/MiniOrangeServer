import traceback
import os
from script.log import SLog, current_run_id, current_flow_id
from server.core.log_database import LogSessionLocal
from server.models.log import WorkflowLog
from driver.agent.Core.orchestrator import Orchestrator
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
        SLog.e("System", f"Log Write Error: {e}")
    finally:
        db.close()


def _workflow_device_type(run_data) -> str | None:
    """工作流 nodes 里若只有一个 platform，则作为 m_device.device_type 用于选机。"""
    nodes = (run_data or {}).get("nodes") if isinstance(run_data, dict) else None
    if not isinstance(nodes, dict):
        return None
    types = {
        str(n.get("platform")).lower()
        for n in nodes.values()
        if isinstance(n, dict) and n.get("platform")
    }
    return types.pop() if len(types) == 1 else None


def _setup_run_target_sn(run_data) -> None:
    """与 driver/client.py ProcessRunner 一致，供 Memory / Driver 按 sn 查 m_device。"""
    import builtins
    import os

    sn = None
    if isinstance(run_data, dict):
        sn = run_data.get("target_sn") or run_data.get("sn")
    if sn:
        builtins.TARGET_DEVICE_SN = str(sn)
        SLog.i("System", f"Target Device SN set to: {sn}")
        return

    device_type = _workflow_device_type(run_data)
    if not device_type:
        return

    from server.services.device_service import DeviceService

    sn = DeviceService.pick_sn(device_type=device_type)
    if not sn and device_type == "ios":
        try:
            from driver.tentacle.engine.mobile.ios_config import list_usb_devices

            usbs = list_usb_devices()
            if usbs:
                sn = usbs[0].udid
        except Exception:
            pass
        if not sn:
            sn = os.environ.get("IOS_UDID")
    if sn:
        builtins.TARGET_DEVICE_SN = str(sn)
        SLog.i("System", f"Target Device SN set to: {sn} (type={device_type})")


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
        # 必须在任何可能走 ServerBridge 的逻辑之前注入（含 Memory load_async 后台线程）
        from driver.agent.in_process_server_query import install_in_process_server_query

        install_in_process_server_query()

        _setup_run_target_sn(run_data)

        SLog.i("System", "start")
        SLog.i("System", f"任务进程启动 PID:{os.getpid()}")
        SLog.i("System", f"输入数据{run_data}")

        # HTTP /workflow/{id}/run 传入的 nodes 写入内联，供 Orchestrator 使用
        nodes = (run_data or {}).get("nodes") if isinstance(run_data, dict) else None
        if isinstance(nodes, dict) and nodes:
            import builtins

            builtins.WORKFLOW_INLINE_NODES = nodes

        run_service.create_run()
        runner = Orchestrator()
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