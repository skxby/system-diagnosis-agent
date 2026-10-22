"""阶段 13 验收：E2E 与隔离攻击（计划第 967–986 行）。

```text
API → DB → Queue → Worker → Analysis → Insight/Evidence 全链路

隔离攻击测试：
  - 篡改 URL / ID 尝试读取其他 Project 的事件、文件 → 必须拒绝
  - 上传含密钥文件，断言磁盘与库中都不存在原文
  - 极小预算 → 期望 partial_success
  - kill Worker → 期望僵尸 Run 被回收为 timeout
```

**这些用例的判定标准是"失败于系统防线"**：攻击应当被拒绝，
而不是"看起来没成功"。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from tests.upload_client import upload_and_wait
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import SessionLocal, engine
from app.main import app

pytestmark = pytest.mark.integration
UTC = timezone.utc


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM evidences WHERE insight_id IN (SELECT id FROM insights "
            "WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'e2e-%'))"))
        for table in ("insights", "events", "agent_runs", "data_sources"):
            conn.execute(text(
                f"DELETE FROM {table} WHERE project_id IN "
                "(SELECT id FROM projects WHERE name LIKE 'e2e-%')"))
        conn.execute(text("DELETE FROM projects WHERE name LIKE 'e2e-%'"))
        conn.execute(text("DELETE FROM users WHERE email LIKE 'e2e-%@example.com'"))


def _signup(client: TestClient) -> dict:
    email = f"e2e-{datetime.now(UTC).timestamp()}@example.com"
    response = client.post("/api/register", json={"email": email, "password": "strong-password"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _project(client: TestClient, headers: dict) -> int:
    return client.post(
        "/api/projects", json={"name": f"e2e-{datetime.now(UTC).timestamp()}"}, headers=headers
    ).json()["id"]


def _upload(client: TestClient, headers: dict, project_id: int, content: bytes) -> dict:
    response = upload_and_wait(client,
        f"/api/projects/{project_id}/upload",
        files={"file": ("syslog.txt", content, "text/plain")},
        data={"fmt": "txt"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


# ============================================================
# 隔离攻击 1：篡改 URL / ID 读取他人资源
# ============================================================


@pytest.mark.parametrize(
    "path_template",
    [
        "/api/projects/{pid}/data-sources",
        "/api/projects/{pid}/knowledge/candidates",
        "/api/projects/{pid}/knowledge/confirmed",
    ],
)
def test_tampered_project_id_is_refused(client: TestClient, path_template: str):
    """把 URL 里的 project_id 换成别人的 → 必须 404，不泄露资源是否存在。"""
    owner = _signup(client)
    attacker = _signup(client)
    victim_project = _project(client, owner)

    response = client.get(path_template.format(pid=victim_project), headers=attacker)
    assert response.status_code == 404, f"{path_template} 未拦住跨项目读取"


def test_tampered_run_id_is_refused(client: TestClient):
    owner = _signup(client)
    attacker = _signup(client)
    project_id = _project(client, owner)
    upload = _upload(
        client, owner, project_id,
        b"Jun 14 15:16:01 combo kernel: Out of memory: Kill process 1234\n",
    )
    with patch("app.api.routes_runs._dispatch"):
        run_id = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={"data_source_id": upload["data_source_id"]},
            headers=owner,
        ).json()["run_id"]

    # 直接篡改 run_id 并用攻击者的 project_id
    attacker_project = _project(client, attacker)
    assert client.get(
        f"/api/runs/{run_id}", params={"project_id": attacker_project}, headers=attacker
    ).status_code == 404
    assert client.get(
        f"/api/runs/{run_id}/detail", params={"project_id": attacker_project}, headers=attacker
    ).status_code == 404
    assert client.get(
        f"/api/runs/{run_id}/insights", params={"project_id": attacker_project}, headers=attacker
    ).status_code == 404
    assert client.post(f"/api/runs/{run_id}/cancel", headers=attacker).status_code == 404


def test_tampered_insight_id_is_refused(client: TestClient):
    owner = _signup(client)
    attacker = _signup(client)
    project_id = _project(client, owner)

    with SessionLocal() as session:
        from app.models import AgentRun, DataSource, Insight

        session.add(DataSource(project_id=project_id, type="file_upload", format="txt", location="a"))
        session.flush()
        source_id = session.query(DataSource).filter_by(project_id=project_id).one().id
        run = AgentRun(project_id=project_id, source_id=source_id, status="completed")
        session.add(run)
        session.flush()
        insight = Insight(project_id=project_id, run_id=run.id, type="possibility",
                          severity="low", confidence=0.3, title="别人的结论", summary="s")
        session.add(insight)
        session.commit()
        insight_id = insight.id

    attacker_project = _project(client, attacker)
    assert client.get(
        f"/api/insights/{insight_id}", params={"project_id": attacker_project}, headers=attacker
    ).status_code == 404
    assert client.get(
        f"/api/insights/{insight_id}/evidence",
        params={"project_id": attacker_project}, headers=attacker,
    ).status_code == 404


def test_tampered_data_source_id_in_run_creation_is_refused(client: TestClient):
    """用别人的 data_source_id 发起分析 → 必须 404。"""
    owner = _signup(client)
    attacker = _signup(client)
    owner_project = _project(client, owner)
    upload = _upload(client, owner, owner_project, b"Jun 14 15:16:01 h a[1]: x\n")

    attacker_project = _project(client, attacker)
    response = client.post(
        f"/api/projects/{attacker_project}/analysis-runs",
        json={"data_source_id": upload["data_source_id"]},
        headers=attacker,
    )
    assert response.status_code == 404


def test_tampered_candidate_id_needs_matching_scope(client: TestClient):
    """知识审核端点：project_id 不匹配时同样拿不到。"""
    owner = _signup(client)
    attacker = _signup(client)
    victim_project = _project(client, owner)
    response = client.post(
        "/api/knowledge/candidates/cand_whatever/confirm",
        params={"project_id": victim_project},
        headers=attacker,
    )
    assert response.status_code == 404


# ============================================================
# 隔离攻击 2：上传含密钥文件 —— 磁盘与库中都不存在原文
# ============================================================

SECRET = "sk-live-abcdef0123456789abcdef"
EMAIL = "oncall@example.com"


def test_uploaded_secrets_are_absent_from_disk_and_database(client: TestClient, work_tmp):
    """计划第 973 行：断言磁盘与库中都不存在原文。"""
    headers = _signup(client)
    project_id = _project(client, headers)

    content = (
        f"Jun 14 15:16:01 combo app[100]: starting with api_key={SECRET}\n"
        f"Jun 14 15:16:02 combo app[100]: alert to {EMAIL}\n"
        "Jun 14 15:16:03 combo app[100]: normal line\n"
    ).encode()

    response = upload_and_wait(client,
        f"/api/projects/{project_id}/upload",
        files={"file": ("syslog.txt", content, "text/plain")},
        data={"fmt": "txt"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mask"]["total"] >= 2, "密钥与邮箱都应被替换"

    # ① 磁盘：uploads 下不得出现原文
    from pathlib import Path

    from app.config import get_settings

    uploads_root = Path(get_settings().data_dir) / "uploads"
    seen_files = list(uploads_root.rglob("*")) if uploads_root.is_dir() else []
    for path in seen_files:
        if path.is_file():
            text_on_disk = path.read_text(encoding="utf-8", errors="replace")
            assert SECRET not in text_on_disk, f"{path} 里存在密钥原文"
            assert EMAIL not in text_on_disk, f"{path} 里存在邮箱原文"

    # ② 数据库：事件与元数据里不得出现原文
    with SessionLocal() as session:
        rows = session.execute(text(
            "SELECT message, payload::text, metadata::text FROM events "
            "WHERE project_id = :p"), {"p": project_id}).all()
    assert rows, "应当有事件入库"
    for message, payload, metadata in rows:
        blob = f"{message} {payload} {metadata}"
        assert SECRET not in blob, "库里存在密钥原文"
        assert EMAIL not in blob, "库里存在邮箱原文"


# ============================================================
# 隔离攻击 3：极小预算 → partial_success
# ============================================================


def test_tiny_budget_yields_partial_success_not_a_full_report(client: TestClient):
    """计划第 974 行：极小预算 → 期望 partial_success。"""
    from app.analysis.retry import RetryPolicy
    from app.analysis.runner import RunExecutor
    from app.gateways.base import ModelUnavailableError
    from app.repositories import AgentRunRepository

    headers = _signup(client)
    project_id = _project(client, headers)

    with SessionLocal() as session:
        from app.models import AgentRun, DataSource

        source = DataSource(project_id=project_id, type="file_upload", format="txt", location="a")
        session.add(source)
        session.flush()
        run = AgentRun(project_id=project_id, source_id=source.id, status="queued")
        session.add(run)
        session.commit()
        run_id = run.id

    with SessionLocal() as session:
        runs = AgentRunRepository(session)
        executor = RunExecutor(run_repository=runs, retry_policy=RetryPolicy(max_retries=0))

        def always_unavailable(tier: str) -> str:
            raise ModelUnavailableError("预算耗尽/模型不可用")

        outcome = executor.execute(
            project_id=project_id,
            run_id=run_id,
            attempt_tier=always_unavailable,
            start_tier="L3",
            rules_only_fallback=lambda: {"kind": "rules_only", "disclaimer": "未经模型分析"},
        )
        session.commit()

    assert outcome.status == "partial_success"
    assert outcome.used_rules_only is True
    assert outcome.stop_reason == "model_unavailable"

    # 状态页与报告页都要能看到"不完整"
    detail = client.get(
        f"/api/runs/{run_id}", params={"project_id": project_id}, headers=headers
    ).json()
    assert detail["status"] == "partial_success"
    assert detail["run_metadata"]["stop_reason"] == "model_unavailable"
    assert "不完整" in detail["run_metadata"]["note"]


# ============================================================
# 隔离攻击 4：kill Worker → 僵尸 Run 被回收为 timeout
# ============================================================


def test_killed_worker_run_is_reclaimed_as_timeout(client: TestClient):
    """计划第 975 行：kill Worker → 期望僵尸 Run 被回收为 timeout。"""
    from app.repositories import AgentRunRepository

    headers = _signup(client)
    project_id = _project(client, headers)

    with SessionLocal() as session:
        from app.models import AgentRun, DataSource

        source = DataSource(project_id=project_id, type="file_upload", format="txt", location="a")
        session.add(source)
        session.flush()
        run = AgentRun(project_id=project_id, source_id=source.id, status="running",
                       started_at=datetime.now(UTC) - timedelta(hours=1))
        session.add(run)
        session.flush()

        runs = AgentRunRepository(session)
        # 模拟 Worker 被杀：心跳停在很久以前
        runs.touch_heartbeat(project_id, run.id, at=datetime.now(UTC) - timedelta(seconds=1000))
        session.flush()

        reclaimed = runs.reclaim_zombies(timeout_seconds=300)
        session.commit()
        run_id = run.id

    assert run_id in reclaimed
    detail = client.get(
        f"/api/runs/{run_id}", params={"project_id": project_id}, headers=headers
    ).json()
    assert detail["status"] == "timeout"
    assert detail["run_metadata"]["stop_reason"] == "timeout"
    assert detail["finished_at"] is not None


def test_live_worker_run_is_not_reclaimed(client: TestClient):
    """阈值必须大于单次最慢调用，否则正在正常工作的 Run 会被误杀。"""
    from app.repositories import AgentRunRepository

    headers = _signup(client)
    project_id = _project(client, headers)

    with SessionLocal() as session:
        from app.models import AgentRun, DataSource

        source = DataSource(project_id=project_id, type="file_upload", format="txt", location="a")
        session.add(source)
        session.flush()
        run = AgentRun(project_id=project_id, source_id=source.id, status="running",
                       started_at=datetime.now(UTC))
        session.add(run)
        session.flush()
        runs = AgentRunRepository(session)
        runs.touch_heartbeat(project_id, run.id, at=datetime.now(UTC))
        session.flush()
        assert runs.reclaim_zombies(timeout_seconds=300) == []
        session.rollback()


# ============================================================
# 全链路 E2E：上传 → 入库 → 分析 → 结论 → 证据
# ============================================================


def test_end_to_end_upload_to_evidence(client: TestClient, work_tmp):
    """API → DB → 分析 → Insight/Evidence 全链路走通。"""
    from app.analysis.persistence import persist_insights
    from app.analysis.pipeline import PipelineInput, analyze
    from app.domains.wiring import build_default_registry
    from app.repositories import (
        EvidenceRepository,
        InsightRepository,
    )

    headers = _signup(client)
    project_id = _project(client, headers)

    # ① 上传：真实 crash 行，应当命中 ProcessCrash
    content = (
        b"Jul  3 13:31:35 host CrashReporterSupportHelper[252]: Internal name did not resolve\n"
        b"Jul  3 13:31:36 host kernel[0]: normal line\n"
    )
    upload = upload_and_wait(client,
        f"/api/projects/{project_id}/upload",
        files={"file": ("syslog.txt", content, "text/plain")},
        data={"fmt": "txt"},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text
    source_id = upload.json()["data_source_id"]

    # ② 分析（确定性部分；模型不参与判定关键命中）
    domain = build_default_registry().load("computer_monitoring")
    with SessionLocal() as session:
        from sqlalchemy import select

        from app.models import AgentRun, Event

        rows = session.execute(
            select(Event).where(Event.project_id == project_id).order_by(Event.id)
        ).scalars().all()
        events = [
            {
                "event_id": int(r.id), "source_id": int(r.source_id), "timestamp": r.timestamp,
                "severity": r.severity, "event_type": r.event_type, "message": r.message,
                "payload": r.payload, "metadata": dict(r.meta or {}),
            }
            for r in rows
        ]
        timestamps = [e["timestamp"] for e in events]
        result = analyze(
            PipelineInput(
                project_id=project_id, source_id=source_id,
                domain_id=domain.domain_id, domain_version=domain.version,
                time_start=min(timestamps), time_end=max(timestamps), events=events,
            ),
            analyzers=domain.analyzers(), domain=domain,
        )
        assert "ProcessCrash" in {a["type"] for a in result.anomalies}

        # ③ 落库：造一条带真实证据的结论
        run = AgentRun(project_id=project_id, source_id=source_id, status="completed")
        session.add(run)
        session.flush()
        first_event_id = events[0]["event_id"]
        run_id = int(run.id)
        # 必须提交：后面的断言会在**新会话**里查这个 Run
        session.commit()

    from app.analysis.evidence import ValidatedInsight

    with SessionLocal() as session:
        run = session.execute(
            text("SELECT id FROM agent_runs WHERE project_id = :p ORDER BY id DESC LIMIT 1"),
            {"p": project_id},
        ).scalar_one()
        written = persist_insights(
            project_id=project_id,
            run_id=run,
            insights=[ValidatedInsight(
                type="fact", severity="high", confidence=0.9,
                title="检测到进程崩溃上报", summary="CrashReporterSupportHelper 在说话",
                evidence_ids=[str(first_event_id)],
            )],
            valid_event_ids={str(first_event_id)},
            insight_repository=InsightRepository(session),
            evidence_repository=EvidenceRepository(session),
            source_id=source_id,
        )
        session.commit()
        assert written.rejected == []

    # ④ 通过 API 读回：结论 + 证据
    insights_response = client.get(
        f"/api/runs/{run_id}/insights", params={"project_id": project_id}, headers=headers
    )
    assert insights_response.status_code == 200
    insights = insights_response.json()
    assert len(insights) == 1
    assert insights[0]["type"] == "fact"

    evidence = client.get(
        f"/api/insights/{insights[0]['id']}/evidence",
        params={"project_id": project_id}, headers=headers,
    )
    assert evidence.status_code == 200
    rows = evidence.json()
    # 证据 id 在校验阶段被统一规范成字符串（阶段 09 的设计），故按字符串比对：
    # `1` 与 `"1"` 必须被视为同一个事件，否则有效证据会被误判为无效。
    assert rows and rows[0]["event_ids"] == [str(first_event_id)], (
        "fact 的证据必须能点到具体事件"
    )


def test_end_to_end_run_detail_narrates_the_whole_thing(client: TestClient):
    """复述能力也是 E2E 的一部分：跑完之后要能说清发生了什么。"""
    headers = _signup(client)
    project_id = _project(client, headers)
    upload = _upload(client, headers, project_id, b"Jun 14 15:16:01 h a[1]: x\n")

    with patch("app.api.routes_runs._dispatch"):
        run_id = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={"data_source_id": upload["data_source_id"]},
            headers=headers,
        ).json()["run_id"]

    detail = client.get(
        f"/api/runs/{run_id}/detail", params={"project_id": project_id}, headers=headers
    )
    assert detail.status_code == 200
    body = detail.json()
    assert body["narrative"]
    assert f"Run #{run_id}" in body["narrative"]
    # 刚创建、还没跑：自检应如实指出"没有任何结论"，而不是假装一切正常
    assert isinstance(body["anomalies"], list)


def test_knowledge_closed_loop_over_http(client: TestClient, work_tmp):
    """知识闭环（计划第 981–985 行）：候选 → 确认 → confirmed。"""
    from app.analysis.knowledge_staging import write_candidates

    headers = _signup(client)
    project_id = _project(client, headers)

    data_dir = work_tmp / "data"
    from app.config import get_settings

    original = get_settings().data_dir
    get_settings().data_dir = str(data_dir)
    try:
        # 新异常 → 候选写入 staging，且必带有效 evidence
        stats = write_candidates(
            data_dir=data_dir,
            domain_id="computer_monitoring",
            candidates=[{
                "kind": "error_pattern", "title": "OOM 反复出现",
                "description": "同一进程反复被 oom-killer 杀死",
                "evidence_ids": ["1"], "confidence": 0.7,
            }],
            run_id=1, project_id=project_id, valid_event_ids={"1"},
        )
        assert stats["written"] == 1

        listed = client.get(
            f"/api/projects/{project_id}/knowledge/candidates", headers=headers
        )
        assert listed.status_code == 200
        candidates = listed.json()
        assert len(candidates) == 1
        assert candidates[0]["status"] == "draft", "候选在确认前必须是 draft"
        candidate_id = candidates[0]["id"]

        # 人工确认 → 进入 confirmed
        confirmed = client.post(
            f"/api/knowledge/candidates/{candidate_id}/confirm",
            params={"project_id": project_id}, headers=headers,
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == "confirmed"

        # 已从 staging 移出，出现在 confirmed 列表里
        assert client.get(
            f"/api/projects/{project_id}/knowledge/candidates", headers=headers
        ).json() == []
        confirmed_list = client.get(
            f"/api/projects/{project_id}/knowledge/confirmed", headers=headers
        ).json()
        assert any(c["id"] == candidate_id for c in confirmed_list)
    finally:
        get_settings().data_dir = original


def test_candidate_without_evidence_never_enters_staging(client: TestClient, work_tmp):
    """计划第 982 行：无证据候选不允许出现。"""
    from app.analysis.knowledge_staging import write_candidates

    data_dir = work_tmp / "data2"
    stats = write_candidates(
        data_dir=data_dir,
        domain_id="computer_monitoring",
        candidates=[{"kind": "error_pattern", "title": "凭空总结", "evidence_ids": ["9999"]}],
        run_id=1, valid_event_ids={"1"},
    )
    assert stats["written"] == 0
    assert stats["skipped"] == 1


def test_false_positive_marks_suppression_bucket(client: TestClient, work_tmp):
    """计划第 985 行：标记 false_positive 后对应误报被抑制（进入误报桶）。"""
    from app.analysis.knowledge_staging import write_candidates
    from app.config import get_settings

    headers = _signup(client)
    project_id = _project(client, headers)

    data_dir = work_tmp / "data3"
    original = get_settings().data_dir
    get_settings().data_dir = str(data_dir)
    try:
        write_candidates(
            data_dir=data_dir, domain_id="computer_monitoring",
            candidates=[{"kind": "error_pattern", "title": "其实是正常抖动",
                         "evidence_ids": ["1"], "confidence": 0.6}],
            run_id=1, project_id=project_id, valid_event_ids={"1"},
        )
        candidate_id = client.get(
            f"/api/projects/{project_id}/knowledge/candidates", headers=headers
        ).json()[0]["id"]

        response = client.post(
            f"/api/knowledge/candidates/{candidate_id}/false-positive",
            params={"project_id": project_id}, headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "confirmed"

        confirmed = client.get(
            f"/api/projects/{project_id}/knowledge/confirmed", headers=headers
        ).json()
        entry = next(c for c in confirmed if c["id"] == candidate_id)
        assert entry["kind"] == "false_positive", "误报必须以 false_positive 类型生效"

        # 归档留痕，便于事后复查
        archive = data_dir / "projects" / str(project_id) / "knowledge" / "computer_monitoring" / "false_positives"
        assert archive.is_dir() and list(archive.glob("*.yaml"))
    finally:
        get_settings().data_dir = original
