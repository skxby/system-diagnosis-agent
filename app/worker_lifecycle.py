"""Celery 子进程在真实任务边界写租约；没有定时线程伪造任务进度。"""
import json
import os
from pathlib import Path
import time
import psutil
from typing import Any

from app.utils.atomic_files import atomic_write


def _write(active: dict[str, Any] | None = None, ready: bool = True) -> None:
    path = os.environ.get("LOGAGENT_WORKER_LEASE")
    if path:
        atomic_write(Path(path), json.dumps({"ready": ready, "active": active, "pid": os.getpid(),
                                            "created": psutil.Process().create_time(),
                                            "at": time.monotonic()}))


def _ready(**kwargs: Any) -> None:
    _write()


def _started(task_id: str | None = None, task: Any = None,
             kwargs: dict[str, Any] | None = None, **other: Any) -> None:
    values = kwargs or {}
    _write({"task_id": task_id, "task": task.name, **{key: values[key] for key in
            ("project_id", "source_id", "run_id", "job_id") if key in values}})


def _finished(**kwargs: Any) -> None:
    _write()


def install_signals() -> None:
    from celery.signals import worker_ready, task_prerun, task_postrun
    worker_ready.connect(_ready, weak=False)
    task_prerun.connect(_started, weak=False)
    task_postrun.connect(_finished, weak=False)
