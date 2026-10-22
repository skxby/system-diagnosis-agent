"""真实事务写入后冻结 Celery 整个进程，验证外部监督器而非内置计时器。"""
from datetime import datetime, timezone
import os
import time
from unittest.mock import patch

import psutil
from app.celery_app import celery_app
from app.models.event import Event
from app.services.upload_service import UploadService
from app.tasks.upload import process_upload


def _freeze_parser(self, project_id, source_id, *args, **kwargs):
    self.session.add(Event(project_id=project_id, source_id=source_id,
        timestamp=datetime.now(timezone.utc), event_type="log", severity="low", message="uncommitted-fault-fixture"))
    self.session.flush()
    psutil.Process(os.getpid()).suspend()
    while True:
        time.sleep(60)


@celery_app.task(name="tests.freeze_upload")
def freeze_upload(*, project_id: int, source_id: int, job_id: str):
    with patch.object(UploadService, "parse_masked", _freeze_parser):
        return process_upload(project_id=project_id, source_id=source_id, job_id=job_id)


@celery_app.task(name="tests.freeze_analysis")
def freeze_analysis(*, project_id: int, run_id: int):
    from app.tasks.analysis import _execute
    from app.db import SessionLocal
    def freeze(*args, **kwargs):
        psutil.Process(os.getpid()).suspend()
        while True:
            time.sleep(60)
    with patch("app.tasks.analysis.analyze", freeze):
        return _execute(session_factory=SessionLocal, project_id=project_id, run_id=run_id, start_tier="L2")
