"""独立 beat 的调度循环就绪证据；任务执行由 maintenance Worker 探针验证。"""
import json
import time
from typing import Any
from celery.beat import PersistentScheduler
from app.utils.atomic_files import atomic_write
from app.worker_supervisor import state_path


class HealthScheduler(PersistentScheduler):
    def tick(self, *args: Any, **kwargs: Any) -> float:
        delay = super().tick(*args, **kwargs)
        atomic_write(state_path("beat"), json.dumps({"healthy": True, "updated_at": time.time()}))
        return delay
