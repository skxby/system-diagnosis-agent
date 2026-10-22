"""独立父进程监督 solo Worker；健康检查验证消费能力，超时终止整个子进程树。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any
from uuid import uuid4

import psutil

from app.config import get_settings
from app.utils.atomic_files import atomic_write, file_lock


def read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def state_path(role: str) -> Path:
    return Path(get_settings().data_dir) / "worker_health" / f"{role}.json"


def healthy(role: str) -> bool:
    state = read_state(state_path(role))
    try:
        age = time.time() - float(state.get("updated_at", 0))
        return state.get("healthy") is True and 0 <= age < 10
    except (TypeError, ValueError):
        return False


def terminate_tree(pid: int) -> None:
    """只终止本监督器持有的子进程及其后代，不按进程名杀其他 Worker。"""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    processes = root.children(recursive=True) + [root]
    for process in reversed(processes):
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(processes, timeout=3)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=3)
    if alive:
        raise RuntimeError("无法停止卡死的 Worker，不启动第二个消费者")


def stop_owned_worker(pid: int | None, created: float | None, lease: dict) -> None:
    """用创建时间与 Celery 命令核对身份，防止 PID 复用或 Windows 启动器先退出。"""
    for target, expected in ((pid, created), (lease.get("pid"), lease.get("created"))):
        if not target or expected is None:
            continue
        try:
            process = psutil.Process(target)
            if process.create_time() != expected:
                continue
            command = process.cmdline()
            if "celery" not in command or "app.celery_app" not in command:
                raise RuntimeError("进程身份不匹配，拒绝终止")
            terminate_tree(target)
        except psutil.NoSuchProcess:
            continue


def recover(active: dict[str, Any], reason: str) -> None:
    """先杀进程释放事务锁，再记录已有对象的明确失败，不新增状态转移。"""
    if not ({"run_id", "job_id"} & active.keys()):
        return
    from app.db import SessionLocal
    from sqlalchemy import text
    with SessionLocal() as session:
        # 监督器不能在故障写回时被另一个数据库事务无限挡住。
        session.execute(text("SET LOCAL lock_timeout = '3s'"))
        session.execute(text("SET LOCAL statement_timeout = '5s'"))
        if "run_id" in active:
            from app.repositories.agent_run import AgentRunRepository
            repo = AgentRunRepository(session)
            if repo.status_of(active["project_id"], active["run_id"]) in {"queued", "running"}:
                repo.apply_status(active["project_id"], active["run_id"], target="timeout",
                    reason=reason, error="Worker 执行超时或异常退出，监督器已停止进程",
                    metadata={"stop_reason": reason})
        elif "job_id" in active:
            from app.repositories.upload_job import UploadJobRepository
            UploadJobRepository(session).update(active["project_id"], active["source_id"], active["job_id"],
                {"status": "failed", "error": reason,
                 "finished_at": datetime.now(timezone.utc).isoformat()},
                expected={"queued", "running"})
        session.commit()


def supervise(role: str, queues: str, *, poll: float = 1, include: str | None = None) -> None:
    from app.celery_app import celery_app
    settings = get_settings()
    status_file = state_path(role)
    lease = status_file.with_suffix(".lease.json")
    instance = uuid4().hex
    probe_queue = f"supervision-{role}-{instance}"
    child = None
    child_created = None
    restarts = 0
    last_failure = None
    probe = None
    lock = file_lock(status_file)
    with lock:
        previous = read_state(status_file)
        if previous.get("worker_pid"):
            old_lease = read_state(lease)
            if previous.get("worker_created") is None and old_lease.get("created") is None:
                raise RuntimeError("旧 Worker 缺少进程身份，请人工核验；未启动第二个消费者")
            stop_owned_worker(previous["worker_pid"], previous.get("worker_created"), old_lease)
            recover(old_lease.get("active") or {}, "worker_supervisor_restarted")
        try:
            while True:
                if child is None:
                    lease.unlink(missing_ok=True)
                    environment = {**os.environ, "LOGAGENT_WORKER_LEASE": str(lease)}
                    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                    command = [sys.executable, "-m", "celery", "-A", "app.celery_app", "worker",
                        "-P", "solo", "-l", "info", "-Q", f"{queues},{probe_queue}", "-n", f"{role}-{instance}@%h"]
                    if include:
                        command.extend(["--include", include])
                    child = subprocess.Popen(command, env=environment, **flags)
                    try:
                        child_created = psutil.Process(child.pid).create_time()
                    except psutil.NoSuchProcess:
                        child_created = None
                    launched = time.monotonic()
                    next_probe = launched
                    probe = None
                    last_probe_ok = False
                now = time.monotonic()
                state = read_state(lease)
                active = state.get("active") or {}
                reason = None
                if child.poll() is not None:
                    reason = "worker_exited"
                elif active and now - state["at"] > settings.worker_task_timeout_seconds:
                    reason = "worker_task_timeout"
                elif not state.get("ready") and now - launched > settings.worker_startup_timeout_seconds:
                    reason = "worker_startup_timeout"
                if state.get("ready") and not active:
                    try:
                        if probe is None and now >= next_probe:
                            probe = celery_app.send_task("health.ping", queue=probe_queue, retry=False)
                            probe_started = now
                        if probe is not None:
                            if probe.ready():
                                last_probe_ok = probe.successful() and probe.get(propagate=False) == 2
                                probe.forget()
                                probe = None
                                next_probe = now + settings.worker_probe_interval_seconds
                                if not last_probe_ok:
                                    reason = "worker_probe_failed"
                            elif now - probe_started > settings.worker_probe_timeout_seconds:
                                reason = "worker_probe_timeout"
                    except Exception:
                        reason = "worker_broker_unavailable"
                elif active and probe is not None:
                    probe_started = now  # 排在真实任务后的探针不会误杀正常长任务。
                if reason:
                    stop_owned_worker(child.pid, child_created, read_state(lease))
                    child.wait(timeout=5)
                    try:
                        recover(read_state(lease).get("active") or active, reason)
                    except Exception as exc:
                        # 失败留在独立健康文件，维护队列恢复后也可回收，不假装写库成功。
                        reason += f":recovery_failed:{type(exc).__name__}"
                    restarts += 1
                    last_failure = reason
                    child = None
                atomic_write(status_file, json.dumps({"healthy": bool(child and state.get("ready") and
                    (active or last_probe_ok)), "updated_at": time.time(), "supervisor_pid": os.getpid(),
                    "worker_pid": child.pid if child else None, "restarts": restarts,
                    "worker_created": child_created if child else None,
                    "last_failure": last_failure, "active": active}))
                time.sleep(poll)
        finally:
            try:
                if child is not None:
                    active = read_state(lease).get("active") or {}
                    stop_owned_worker(child.pid, child_created, read_state(lease))
                    child.wait(timeout=5)
                    recover(read_state(lease).get("active") or active, "worker_stopped")
            finally:
                atomic_write(status_file, json.dumps({"healthy": False, "updated_at": time.time(), "stopped": True}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=["analysis", "uploads", "maintenance", "beat"], required=True)
    parser.add_argument("--queues", default="celery")
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--include", help="额外 Celery 任务模块，仅由部署者显式指定")
    args = parser.parse_args()
    if args.health:
        raise SystemExit(0 if healthy(args.role) else 1)
    def stop(*unused: Any) -> None:
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        supervise(args.role, args.queues, include=args.include)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
