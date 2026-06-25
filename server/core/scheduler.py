import json
import uuid
import asyncio
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from server.core.database import SessionLocal
from server.models.schedule import ScheduledTask, ScheduledTaskHistory
from server.models.workflow import Workflow
from server.websocket.device_manager import DeviceManager
from script.log import SLog


class SchedulerService:
    _instance = None
    scheduler = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SchedulerService, cls).__new__(cls)
            cls._instance.scheduler = AsyncIOScheduler()
        return cls._instance

    def start(self):
        if not self.scheduler.running:
            self.load_jobs()
            self.scheduler.start()
            SLog.i("Scheduler", "Scheduler started")

    def shutdown(self):
        if self.scheduler.running:
            self.scheduler.shutdown()

    def load_jobs(self):
        """Load active jobs from database on startup"""
        db = SessionLocal()
        try:
            tasks = db.query(ScheduledTask).filter(ScheduledTask.is_active == True).all()
            for task in tasks:
                self.add_job(task)
        except Exception as e:
            SLog.e("Scheduler", f"Error loading jobs: {e}")
        finally:
            db.close()

    def add_job(self, task: ScheduledTask):
        try:
            # APScheduler CronTrigger expects 5 fields: minute, hour, day, month, day_of_week
            trigger = CronTrigger.from_crontab(task.cron_expression)
            self.scheduler.add_job(
                execute_scheduled_task,
                trigger,
                id=task.id,
                args=[task.id],
                replace_existing=True
            )
            SLog.i("Scheduler", f"Added job {task.id} [{task.name}] cron: {task.cron_expression}")
        except Exception as e:
            SLog.e("Scheduler", f"Failed to add job {task.id}: {e}")

    def remove_job(self, task_id: str):
        if self.scheduler.get_job(task_id):
            self.scheduler.remove_job(task_id)
            SLog.i("Scheduler", f"Removed job {task_id}")

    def update_job(self, task: ScheduledTask):
        self.remove_job(task.id)
        if task.is_active:
            self.add_job(task)


def _get_task_context(task_id: str):
    """
    Synchronous helper to fetch task and workflow data from DB.
    Running this in a thread pool prevents blocking the asyncio loop.
    """
    db = SessionLocal()
    try:
        task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
        if not task or not task.is_active:
            return None

        workflow = db.query(Workflow).filter(Workflow.id == task.flow_id).first()
        if not workflow:
            SLog.e("Scheduler", f"Workflow {task.flow_id} not found for task {task_id}")
            return None

        # Safe JSON decoding
        try:
            nodes_data = json.loads(workflow.nodes) if workflow.nodes else {}
        except (json.JSONDecodeError, TypeError):
            nodes_data = {}

        try:
            skip_nodes = json.loads(task.skip_nodes) if task.skip_nodes else []
        except (json.JSONDecodeError, TypeError):
            skip_nodes = []

        return {
            "task_name": task.name,
            "target_sn": task.target_sn,
            "skip_nodes": skip_nodes,
            "workflow_id": workflow.id,
            "workflow_name": workflow.name,
            "workflow_updated_at": str(workflow.updated_at) if workflow.updated_at else None,
            "nodes_data": nodes_data
        }
    except Exception as e:
        SLog.e("Scheduler", f"DB Error in _get_task_context: {e}")
        return None
    finally:
        db.close()


def _record_history_start(task_id: str, run_id: str):
    """Record the start of a scheduled task execution"""
    db = SessionLocal()
    try:
        history = ScheduledTaskHistory(
            schedule_id=task_id,
            run_id=run_id,
            status="pending",
            details="Preparing to dispatch..."
        )
        db.add(history)
        db.commit()
        return history.id
    except Exception as e:
        SLog.e("Scheduler", f"DB Error in _record_history_start: {e}")
        return None
    finally:
        db.close()


def _update_history_result(history_id: str, task_id: str, status: str, details: str):
    """Update history status and task last_run_time"""
    db = SessionLocal()
    try:
        # 1. Update History
        history = db.query(ScheduledTaskHistory).filter(ScheduledTaskHistory.id == history_id).first()
        if history:
            history.status = status
            history.details = details

        # 2. Update Task Last Run Time (only on success/dispatch)
        if status == "dispatched":
            task = db.query(ScheduledTask).filter(ScheduledTask.id == task_id).first()
            if task:
                task.last_run_time = datetime.now()

        db.commit()
    except Exception as e:
        SLog.e("Scheduler", f"DB Error in _update_history_result: {e}")
    finally:
        db.close()


async def execute_scheduled_task(task_id: str):
    """Logic to trigger the workflow run (Non-blocking)"""
    SLog.i("Scheduler", f"Triggering task {task_id}")
    
    loop = asyncio.get_running_loop()
    
    # 1. Fetch Data (Run in Thread)
    ctx = await loop.run_in_executor(None, _get_task_context, task_id)
    if not ctx:
        return

    target = ctx["target_sn"]
    if not target:
        SLog.w("Scheduler", f"No target_sn for task {ctx['task_name']}, skipping execution.")
        return

    # 2. Prepare Params
    run_id = str(uuid.uuid4())
    run_data = {
        "id": ctx["workflow_id"],
        "name": ctx["workflow_name"],
        "nodes": ctx["nodes_data"],
        "updated_at": ctx["workflow_updated_at"]
    }
    # 3. Record History (Start)
    history_id = await loop.run_in_executor(None, _record_history_start, task_id, run_id)

    params = {
        "run_id": run_id,
        "flow_id": ctx["workflow_id"],
        "run_data": run_data,
        "target_sn": target,
        "trigger_type": "schedule",
        "schedule_id": task_id,
        "skip_nodes": ctx["skip_nodes"]
    }

    # 4. Send Command (Async)
    try:
        out = await DeviceManager().send_command(target, "run_task", params, wait_timeout=None)
        success = out.get("sent", False) if isinstance(out, dict) else bool(out)
        if success:
            SLog.i("Scheduler", f"Task {ctx['task_name']} sent to {target}")
            await loop.run_in_executor(None, _update_history_result, history_id, task_id, "dispatched",
                                       f"Sent to {target}")
        else:
            SLog.e("Scheduler", f"Failed to send task {ctx['task_name']} to {target} (Offline?)")
            await loop.run_in_executor(None, _update_history_result, history_id, task_id, "failed",
                                       f"Device {target} offline or unreachable")
    except Exception as e:
        SLog.e("Scheduler", f"Error sending command: {e}")
        await loop.run_in_executor(None, _update_history_result, history_id, task_id, "error", str(e))

