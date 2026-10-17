"""审计问题的真实 PG 回归：作用域、产物、知识隔离与并发。"""
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, func

from app.config import get_settings
from app.db import SessionLocal
from app.main import app
from app.models import User, Project, DataSource, Event, AgentRun
from app.repositories.agent_run import AgentRunRepository
from app.repositories.event import EventRepository
from app.repositories.project import ProjectRepository
from app.analysis.runner import RunRequest, create_run
from app.analysis.knowledge_staging import write_candidates, load_candidates, staging_path

pytestmark = pytest.mark.integration
UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture()
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    with TestClient(app) as client:
        response = client.post("/api/register", json={"email": f"audit-{uuid4().hex}@example.com", "password": "audit-password"})
        assert response.status_code == 200, response.text
        headers = {"Authorization": "Bearer " + response.json()["access_token"]}
        pid = client.post("/api/projects", headers=headers, json={"name": "audit-regression", "budget_total": 10}).json()["id"]
        sid = client.post(f"/api/projects/{pid}/data-sources", headers=headers, json={"format": "txt"}).json()["id"]
        yield client, headers, pid, sid, tmp_path


def test_sql_scope_filters_source_time_severity_type_and_literal_keyword(context):
    client, headers, pid, sid, _ = context
    with SessionLocal() as session:
        second = DataSource(project_id=pid, type="file_upload", format="jsonl", location="other")
        session.add(second)
        session.flush()
        for source, moment, severity, kind, message in [
            (sid, T0, "high", "log", "error 100%_exact"),
            (sid, T0, "low", "log", "error 100%_exact"),
            (sid, T0, "high", "metric", "error 100%_exact"),
            (sid, T0 + timedelta(days=1), "high", "log", "error 100%_exact"),
            (second.id, T0, "high", "log", "error 100%_exact"),
            (sid, T0, "high", "log", "error 100AAexact"),
        ]:
            session.add(Event(project_id=pid, source_id=source, timestamp=moment,
                              severity=severity, event_type=kind, message=message))
        session.commit()
        matches = EventRepository(session).pipeline_events(pid, source_id=sid,
            time_start=T0, time_end=T0, filters={"severity": ["high"], "event_type": ["log"], "keyword": "%_exact"})
        assert len(matches) == 1 and matches[0]["message"] == "error 100%_exact"
        with pytest.raises(ValueError, match="未静默截断"):
            EventRepository(session).pipeline_events(pid, limit=1)


def test_real_worker_saves_l0_report_history_progress_and_reuses_input(context):
    from app.tasks.analysis import _execute
    client, headers, pid, sid, _ = context
    sample = Path(__file__).resolve().parents[1] / "datasets" / "normal" / "system.log"
    result = client.post(f"/api/projects/{pid}/upload", headers=headers,
        files={"file": ("13812345678-user@example.com.log", sample.read_bytes())}, data={"fmt": "txt", "data_source_id": sid})
    assert result.status_code == 200, result.text
    assert "13812345678" not in result.json()["stored_filename"]
    assert "example.com" not in result.json()["stored_filename"]
    with patch("app.api.routes_runs._dispatch"):
        created = client.post(f"/api/projects/{pid}/analysis-runs", headers=headers, json={"data_source_id": sid}).json()
    _execute(session_factory=SessionLocal, run_id=created["run_id"], project_id=pid, start_tier="L2")
    run = client.get(f"/api/runs/{created['run_id']}", headers=headers).json()
    assert run["status"] == "completed"
    assert run["run_metadata"]["rules_only_report"]["statistics"]["total"] > 0
    assert {phase["phase"] for phase in run["phase_history"]} >= {"statistics", "grouping", "detection", "persistence"}
    assert run["run_metadata"]["model_attempts"] == []
    assert client.get(f"/api/projects/{pid}/runs", headers=headers).json()[0]["id"] == created["run_id"]
    groups = client.get(f"/api/projects/{pid}/groups", headers=headers).json()
    assert sum(group["event_count"] for group in groups) == 60
    members = client.get(f"/api/projects/{pid}/groups/{groups[0]['id']}/events", headers=headers).json()
    assert len(members) == groups[0]["event_count"] and members[0]["message"]
    assert client.get(f"/api/projects/{pid}/incidents", headers=headers).json()
    with patch("app.api.routes_runs._dispatch") as dispatch:
        repeated = client.post(f"/api/projects/{pid}/analysis-runs", headers=headers, json={"data_source_id": sid}).json()
    assert repeated["reused"] and repeated["run_id"] == created["run_id"]
    dispatch.assert_not_called()
    duplicate = client.post(f"/api/projects/{pid}/upload", headers=headers,
        files={"file": ("again.log", sample.read_bytes())}, data={"fmt": "txt", "data_source_id": sid}).json()
    assert duplicate["events_persisted"] == 0 and duplicate["parse"]["duplicate"]


def test_dispatch_failure_is_visible_and_queued_run_has_timeout_basis(context):
    client, headers, pid, sid, _ = context
    with patch("app.api.routes_runs._dispatch", side_effect=ConnectionError("broker down")):
        result = client.post(f"/api/projects/{pid}/analysis-runs", headers=headers, json={"data_source_id": sid}).json()
    assert result["status"] == "timeout"
    run = client.get(f"/api/runs/{result['run_id']}", headers=headers).json()
    assert run["run_metadata"]["stop_reason"] == "dispatch_failed"
    with SessionLocal() as session:
        repo = AgentRunRepository(session)
        queued = repo.create_queued(pid, source_id=sid)
        session.flush()
        queued.run_metadata = {"queued_at": (datetime.now(UTC) - timedelta(minutes=20)).isoformat()}
        session.commit()
        assert queued.id in repo.reclaim_zombies(timeout_seconds=300)
        session.commit()
        assert repo.status_of(pid, queued.id) == "timeout"


def test_source_revision_freezes_events_and_invalidates_cached_input(context):
    from app.tasks.analysis import _execute
    client, headers, pid, sid, _ = context
    def insert_event(message):
        with SessionLocal() as session:
            session.add(Event(project_id=pid, source_id=sid, timestamp=T0,
                              severity="low", event_type="log", message=message))
            session.commit()
    insert_event("first event")
    with patch("app.api.routes_runs._dispatch"):
        first = client.post(f"/api/projects/{pid}/analysis-runs", headers=headers, json={"data_source_id": sid}).json()
        insert_event("second event at the same timestamp")
        second = client.post(f"/api/projects/{pid}/analysis-runs", headers=headers, json={"data_source_id": sid}).json()
    assert second["run_id"] != first["run_id"] and not second["reused"]
    for created, expected in [(first, 1), (second, 2)]:
        _execute(session_factory=SessionLocal, run_id=created["run_id"], project_id=pid, start_tier="L2")
        metadata = client.get(f"/api/runs/{created['run_id']}", headers=headers).json()["run_metadata"]
        assert metadata["rules_only_report"]["statistics"]["total"] == expected
        assert _execute(session_factory=SessionLocal, run_id=created["run_id"], project_id=pid, start_tier="L2")["reused"]
        assert client.get(f"/api/runs/{created['run_id']}", headers=headers).json()["run_metadata"] == metadata


def test_login_rate_limit_is_shared_by_real_redis(context):
    client, _, _, _, _ = context
    email = f"missing-{uuid4().hex}@example.com"
    for _ in range(10):
        assert client.post("/api/login", json={"email": email, "password": "wrong-password"}).status_code == 401
    rejected = client.post("/api/login", json={"email": email, "password": "wrong-password"})
    assert rejected.status_code == 429 and rejected.headers["retry-after"] == "300"


def test_real_upload_uses_configured_timezone_and_year_rollover(context, monkeypatch):
    from app.services.upload_service import UploadService
    _, _, pid, sid, data_dir = context
    monkeypatch.setattr(get_settings(), "default_timezone", "UTC")
    with SessionLocal() as session:
        service = UploadService(session, uploads_dir=data_dir / "uploads")
        uploaded = service.ingest(project_id=pid, data_source_id=sid, fmt="txt", filename="rollover.log",
            content="Dec 28 10:00:00 host app[1]: normal message\n", now=datetime(2026, 1, 3, tzinfo=UTC))
        session.commit()
        assert uploaded.events_persisted == 1
        event = session.scalar(select(Event).where(Event.project_id == pid))
        assert event.timestamp == datetime(2025, 12, 28, 10, tzinfo=UTC)


def test_project_knowledge_isolation_edit_and_reject(context):
    client, headers, pid, sid, data_dir = context
    other = client.post("/api/projects", headers=headers, json={"name": "other-owned-project"}).json()["id"]
    stats = write_candidates(data_dir=data_dir, domain_id="computer_monitoring", project_id=pid,
        candidates=[{"title": "candidate", "evidence_ids": ["1"], "confidence": .8}], valid_event_ids={"1"}, run_id=1)
    cid = load_candidates(Path(stats["path"]))[0]["id"]
    assert client.get(f"/api/projects/{other}/knowledge/candidates", headers=headers).json() == []
    assert client.post(f"/api/knowledge/candidates/{cid}/confirm?project_id={other}", headers=headers).status_code == 404
    edited = client.patch(f"/api/knowledge/candidates/{cid}?project_id={pid}", headers=headers, json={"title": "edited"})
    assert edited.status_code == 200 and edited.json()["title"] == "edited"
    assert client.patch(f"/api/knowledge/candidates/{cid}?project_id={pid}", headers=headers, json={"title": ""}).status_code == 422
    assert client.post(f"/api/knowledge/candidates/{cid}/reject?project_id={pid}", headers=headers).status_code == 200
    assert client.get(f"/api/projects/{pid}/knowledge/candidates", headers=headers).json() == []
    assert all(row["id"] != cid for row in client.get(f"/api/projects/{pid}/knowledge/confirmed", headers=headers).json())


def test_concurrent_budget_updates_do_not_lose_money(context):
    _, _, pid, _, _ = context
    barrier = Barrier(8)

    def add_cost(index):
        with SessionLocal() as session:
            barrier.wait(timeout=10)
            ProjectRepository(session).add_budget_used(pid, .01)
            session.commit()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(add_cost, range(8)))
    with SessionLocal() as session:
        assert float(session.get(Project, pid).budget_used) == pytest.approx(.08)


def test_concurrent_same_input_creates_one_run(context):
    _, _, pid, sid, _ = context
    barrier = Barrier(2)

    def create(index):
        with SessionLocal() as session:
            barrier.wait(timeout=10)
            result = create_run(AgentRunRepository(session), RunRequest(project_id=pid,
                source_id=sid, time_start=T0, time_end=T0, domain_id="computer_monitoring", domain_version="1.0.0"))
            session.commit()
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, range(2)))
    assert results[0][0] == results[1][0]
    assert sorted(result[2] for result in results) == [False, True]
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(AgentRun).where(AgentRun.project_id == pid)) == 1


def test_concurrent_candidate_writers_preserve_all_records(context):
    _, _, pid, _, data_dir = context
    def write(index):
        return write_candidates(data_dir=data_dir, domain_id="computer_monitoring", project_id=pid,
            candidates=[{"title": f"candidate-{index}", "evidence_ids": ["1"]}], valid_event_ids={"1"})
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(write, range(16)))
    assert len(load_candidates(staging_path(data_dir, "computer_monitoring", pid))) == 16


def _process_candidate(args):
    data_dir, project_id, index = args
    return write_candidates(data_dir=data_dir, domain_id="computer_monitoring", project_id=project_id,
        candidates=[{"title": f"process-{index}", "evidence_ids": ["1"]}], valid_event_ids={"1"})["written"]


def test_candidate_file_lock_works_across_processes(context):
    _, _, pid, _, data_dir = context
    with ProcessPoolExecutor(max_workers=4) as executor:
        assert sum(executor.map(_process_candidate, [(str(data_dir), pid, i) for i in range(12)])) == 12
    assert len(load_candidates(staging_path(data_dir, "computer_monitoring", pid))) == 12


@pytest.mark.parametrize("competitor", ["cancelled", "reaper"])
def test_terminal_races_never_resurrect_or_write_two_terminal_states(context, competitor):
    from app.analysis.state_machine import IllegalTransitionError
    from app.models.enums import AGENT_RUN_TERMINAL_STATES
    _, _, pid, sid, _ = context
    for _ in range(50):
        with SessionLocal() as session:
            repo = AgentRunRepository(session)
            run = repo.create_queued(pid, source_id=sid)
            session.flush()
            run_id = run.id
            repo.apply_status(pid, run_id, target="running")
            run.started_at = run.last_heartbeat = datetime.now(UTC) - timedelta(hours=1)
            session.commit()
        barrier = Barrier(2)

        def finish(target):
            with SessionLocal() as session:
                repo = AgentRunRepository(session)
                barrier.wait(timeout=10)
                try:
                    if target == "reaper":
                        repo.reclaim_zombies(project_id=pid, timeout_seconds=300)
                    else:
                        repo.apply_status(pid, run_id, target=target)
                    session.commit()
                except IllegalTransitionError:
                    session.rollback()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(finish, ["completed", competitor]))
        with SessionLocal() as session:
            stored = session.get(AgentRun, run_id)
            assert stored.status in {"completed", "cancelled", "timeout"}
            terminal = [item for item in stored.phase_history if item["status"] in AGENT_RUN_TERMINAL_STATES]
            assert len(terminal) == 1


def test_logout_deletes_httponly_cookie_and_closed_registration_is_enforced(context, monkeypatch):
    client, headers, pid, sid, _ = context
    monkeypatch.setattr(get_settings(), "allow_registration", False)
    assert client.post("/api/register", json={"email": f"blocked-{uuid4().hex}@example.com", "password": "test-password"}).status_code == 403
    client.cookies.set("access_token", "old-token")
    assert client.post("/api/logout").status_code == 200
    assert "Max-Age=0" in client.post("/api/logout").headers["set-cookie"]
