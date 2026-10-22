import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch
from uuid import uuid4

import psutil
import pytest
from sqlalchemy import select, func

from app.celery_app import celery_app
from app.config import get_settings
from app.db import SessionLocal
from app.models import Event
from app.repositories.upload_job import UploadJobRepository
from app.worker_supervisor import healthy, read_state, state_path, terminate_tree
from tests.integration.test_audit_regressions_pg import context

pytestmark = pytest.mark.integration


def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.1)
    raise AssertionError("后台进程/任务未在时限内达到预期状态")


def prepare_http(context):
    client, headers, pid, sid, data = context
    with patch("app.api.routes_runs._dispatch_upload"):
        response = client.post(f"/api/projects/{pid}/upload", headers=headers,
            files={"file": ("ops@example.com.txt", b"Jun 14 10:00:00 h app[1]: contact ops@example.com\n")},
            data={"fmt": "txt", "data_source_id": sid})
    assert response.status_code == 202, response.text
    return response.json()


def test_post_only_masks_and_queues_before_real_worker_runs(context):
    client, headers, pid, sid, data = context
    accepted = prepare_http(context)
    assert accepted["status"] == "queued"
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Event).where(Event.project_id == pid)) == 0
        _, job = UploadJobRepository(session).get(pid, accepted["job_id"])
        masked = (Path(job["uploads_dir"]) / str(pid) / job["stored_filename"]).read_text(encoding="utf-8")
        assert "ops@example.com" not in masked and "[EMAIL_" in masked
    refused = client.post(f"/api/projects/{pid}/analysis-runs", headers=headers, json={"data_source_id": sid})
    assert refused.status_code == 409
    task = celery_app.send_task("app.tasks.upload.execute_upload", queue=get_settings().upload_queue,
        kwargs={"project_id": pid, "source_id": sid, "job_id": accepted["job_id"]})
    task.get(timeout=20)
    result = client.get(f"/api/projects/{pid}/uploads/{accepted['job_id']}", headers=headers).json()
    assert result["status"] == "completed" and result["events_persisted"] == 1
    assert "uploads_dir" not in result and "digest" not in result
    celery_app.send_task("app.tasks.upload.execute_upload", queue=get_settings().upload_queue,
        kwargs={"project_id": pid, "source_id": sid, "job_id": accepted["job_id"]}).get(timeout=20)
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Event).where(Event.project_id == pid)) == 1


def test_dispatch_failure_and_retry_are_visible(context):
    client, headers, pid, sid, _ = context
    with patch("app.api.routes_runs._dispatch_upload", side_effect=ConnectionError("offline")):
        response = client.post(f"/api/projects/{pid}/upload", headers=headers,
            files={"file": ("a.txt", b"Jun 14 10:00:00 h app[1]: hello\n")}, data={"fmt": "txt"})
    failed = response.json()
    assert response.status_code == 202 and failed["status"] == "failed" and failed["error"]
    retried = client.post(f"/api/projects/{pid}/uploads/{failed['job_id']}/retry", headers=headers)
    assert retried.status_code == 202 and retried.json()["job_id"] != failed["job_id"]
    url = f"/api/projects/{pid}/uploads/{retried.json()['job_id']}"
    wait_for(lambda: client.get(url, headers=headers).json()["status"] == "completed")
    assert client.get(url, headers=headers).json()["events_persisted"] == 1


def test_upload_job_is_project_scoped(context):
    client, headers, pid, sid, _ = context
    accepted = prepare_http(context)
    other = client.post("/api/projects", headers=headers, json={"name": "other"}).json()["id"]
    assert client.get(f"/api/projects/{other}/uploads/{accepted['job_id']}", headers=headers).status_code == 404
    assert client.post(f"/api/projects/{other}/uploads/{accepted['job_id']}/retry", headers=headers).status_code == 404
    assert client.get(f"/api/projects/{other}/uploads", headers=headers).json() == []


def test_queued_upload_without_delivery_is_reclaimed(context):
    from app.tasks.maintenance import reclaim_uploads
    _, _, pid, sid, _ = context
    accepted = prepare_http(context)
    with SessionLocal() as session:
        repo = UploadJobRepository(session)
        repo.update(pid, sid, accepted["job_id"], {"created_at": "2000-01-01T00:00:00+00:00"})
        session.commit()
        assert reclaim_uploads(session) >= 1
        session.commit()
        assert repo.get(pid, accepted["job_id"])[1]["status"] == "failed"


def test_enqueue_does_not_wait_for_event_foreign_key_lock(context):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone
    client, headers, pid, sid, _ = context
    with SessionLocal() as writer:
        writer.add(Event(project_id=pid, source_id=sid, timestamp=datetime.now(timezone.utc),
                         severity="low", event_type="log", message="uncommitted concurrent import"))
        writer.flush()  # 持有来源 KEY SHARE，直到 finally 回滚。
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                future = executor.submit(prepare_http, context)
                assert future.result(timeout=3)["status"] == "queued"
            finally:
                writer.rollback()


@pytest.mark.parametrize("kind", ["upload", "analysis"])
def test_external_supervisor_kills_frozen_worker_rolls_back_and_consumes_again(context, kind):
    client, headers, pid, sid, data = context
    accepted = prepare_http(context)
    payload = {"project_id": pid, "source_id": sid, "job_id": accepted["job_id"]}
    if kind == "analysis":
        celery_app.send_task("app.tasks.upload.execute_upload", queue=get_settings().upload_queue, kwargs=payload).get(timeout=20)
        with patch("app.api.routes_runs._dispatch"):
            run = client.post(f"/api/projects/{pid}/analysis-runs", headers=headers, json={"data_source_id": sid}).json()
        payload = {"project_id": pid, "run_id": run["run_id"]}
    queue = "fault-" + uuid4().hex
    environment = {**os.environ, "DATA_DIR": str(data), "WORKER_TASK_TIMEOUT_SECONDS": "2",
        "WORKER_PROBE_INTERVAL_SECONDS": "1", "WORKER_PROBE_TIMEOUT_SECONDS": "5"}
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with (data / "supervisor-fault.log").open("w", encoding="utf-8") as output:
        parent = subprocess.Popen([sys.executable, "-m", "app.worker_supervisor", "--role", "analysis",
            "--queues", queue, "--include", "tests.fixtures.worker_faults"], env=environment,
            stdout=output, stderr=subprocess.STDOUT, **flags)
        try:
            wait_for(lambda: healthy("analysis"), timeout=40)
            celery_app.send_task("tests.freeze_" + kind, queue=queue, expires=30, kwargs=payload)
            lease = state_path("analysis").with_suffix(".lease.json")
            key = "job_id" if kind == "upload" else "run_id"
            busy = wait_for(lambda: (state if ((state := read_state(lease)).get("active") or {}).get(key)
                                    == payload[key] else None))
            frozen = psutil.Process(busy["pid"])
            wait_for(lambda: read_state(state_path("analysis")).get("restarts", 0) >= 1, timeout=25)
            assert not frozen.is_running()
            with SessionLocal() as session:
                if kind == "upload":
                    _, job = UploadJobRepository(session).get(pid, accepted["job_id"])
                    assert job["status"] == "failed" and job["error"] == "worker_task_timeout"
                    assert session.scalar(select(func.count()).select_from(Event).where(Event.project_id == pid)) == 0
                else:
                    from app.models import AgentRun
                    stopped = session.get(AgentRun, payload["run_id"])
                    assert stopped.status == "timeout" and stopped.run_metadata["stop_reason"] == "worker_task_timeout"
            wait_for(lambda: healthy("analysis"), timeout=40)
            assert celery_app.send_task("health.ping", queue=queue).get(timeout=15) == 2
            assert read_state(state_path("analysis"))["last_failure"] == "worker_task_timeout"
            evidence_path = Path(__file__).resolve().parents[2] / ".dsh/junit/async-runtime-process-evidence.json"
            evidence_path.parent.mkdir(parents=True, exist_ok=True)
            previous = [] if kind == "upload" else json.loads(evidence_path.read_text(encoding="utf-8"))
            previous.append({"kind": kind, "frozen_pid": busy["pid"], "old_process_dead": not frozen.is_running(),
                "restarts": read_state(state_path("analysis"))["restarts"], "probe_after_restart": 2,
                "terminal": "failed" if kind == "upload" else "timeout", "rollback_verified": kind == "upload"})
            evidence_path.write_text(json.dumps(previous, indent=2), encoding="utf-8")
        finally:
            terminate_tree(parent.pid)
            parent.wait(timeout=10)
