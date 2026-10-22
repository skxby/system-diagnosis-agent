"""阶段 10 验收的集成测试（真实 PostgreSQL + FastAPI TestClient）。

验收四条：
  1. 每个端点鉴权与参数校验生效
  2. 分析端点立即返回、不阻塞
  3. 接口文档 /docs 可访问
  4. 跨 Project 访问被拒绝
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from tests.upload_client import upload_and_wait
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal, engine
from app.gateways.base import ConfigurationError
from app.gateways.router import read_tier_configs
from app.main import app

pytestmark = pytest.mark.integration
UTC = timezone.utc


def _require_cost_gate_readable() -> None:
    """确认成本闸门**能构造出来** —— 这是"预算不足拒绝创建"的前提。

    `build_cost_controller` 读不到型号会返回 `None`，端点上那道 Pre-check 就
    整个缺席，创建一律 202 放行。真机核验（2026-09-24）在 CI 上就是这么栽的：
    同一份代码本地 402、CI 202，只差 CI 没配 MODEL_L1/L2/L3。

    这里**显式失败**而不是 `skip`：略过等于这条真机验收从来没跑过
    （项目红线下不允许用跳过代替通过）。
    """
    try:
        configs = read_tier_configs(get_settings())
    except ConfigurationError as exc:
        pytest.fail(
            f"型号没配齐，成本闸门无法构造，这条真机验收无从执行：{exc}；"
            "请按 .env.example 补齐 MODEL_L1/L2/L3（CI 见 .github/workflows/ci.yml）"
        )
    if all(
        config.price_input_per_1m <= 0 and config.price_output_per_1m <= 0
        for config in configs.values()
    ):
        pytest.fail(
            "单价全是 0：估算成本恒为 0，Pre-check 会一路放行（闸门在却不生效）；"
            "请按 .env.example 补 MODEL_L1/L2/L3_PRICE_*_PER_1M"
        )


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


def _email() -> str:
    return f"api-{datetime.now(UTC).timestamp()}@example.com"


def _register(client: TestClient, password: str = "strong-password") -> dict:
    """注册一个新账号并返回 Authorization 头。"""
    email = _email()
    response = client.post("/api/register", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}", "email": email}


def _auth_headers_fresh_user(client: TestClient) -> dict:
    return _register(client)


@pytest.fixture(autouse=True)
def _cleanup():
    """测试产生的数据在结束时清掉，避免污染开发库。"""
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM evidences WHERE insight_id IN (SELECT id FROM insights WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'apitest-%'))"))
        conn.execute(text("DELETE FROM insights WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'apitest-%')"))
        conn.execute(text("DELETE FROM events WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'apitest-%')"))
        conn.execute(text("DELETE FROM agent_runs WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'apitest-%')"))
        conn.execute(text("DELETE FROM data_sources WHERE project_id IN (SELECT id FROM projects WHERE name LIKE 'apitest-%')"))
        conn.execute(text("DELETE FROM projects WHERE name LIKE 'apitest-%'"))
        conn.execute(text("DELETE FROM users WHERE email LIKE 'api-%@example.com'"))


def _make_project(client: TestClient, headers: dict, name: str | None = None) -> int:
    response = client.post(
        "/api/projects",
        json={"name": name or f"apitest-{datetime.now(UTC).timestamp()}"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ============================================================
# 验收 1：鉴权生效
# ============================================================


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/projects"),
        ("post", "/api/projects"),
        ("get", "/api/runs/1"),
        ("post", "/api/runs/1/cancel"),
        ("get", "/api/insights/1"),
        ("get", "/api/insights/1/evidence"),
        ("get", "/api/me"),
        ("get", "/api/projects/1/data-sources"),
        ("get", "/api/projects/1/knowledge/candidates"),
        ("get", "/api/projects/1/knowledge/confirmed"),
    ],
)
def test_endpoints_require_authentication(client: TestClient, method: str, path: str):
    """验收：每个端点鉴权生效 —— 无令牌一律 401。"""
    response = getattr(client, method)(path)
    assert response.status_code == 401, f"{method.upper()} {path} 未鉴权却返回 {response.status_code}"


def test_invalid_token_is_rejected(client: TestClient):
    response = client.get(
        "/api/projects", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert response.status_code == 401


def test_malformed_authorization_header_is_rejected(client: TestClient):
    for header in ("Token abc", "Bearer", "Bearer ", "abc"):
        response = client.get("/api/projects", headers={"Authorization": header})
        assert response.status_code == 401, header


def test_token_for_deleted_user_is_rejected(client: TestClient):
    headers = _register(client)
    # 令牌本身合法，但用户被删掉后应当拒绝
    with SessionLocal() as session:
        session.execute(
            text("DELETE FROM users WHERE email = :e"), {"e": headers["email"]}
        )
        session.commit()
    response = client.get("/api/projects", headers=headers)
    assert response.status_code == 401


def test_valid_token_grants_access(client: TestClient):
    headers = _register(client)
    assert client.get("/api/me", headers=headers).status_code == 200
    assert client.get("/api/projects", headers=headers).status_code == 200


def test_register_rejects_duplicate_email(client: TestClient):
    headers = _register(client)
    response = client.post(
        "/api/register", json={"email": headers["email"], "password": "another-password"}
    )
    assert response.status_code == 409


def test_login_with_wrong_password_is_rejected(client: TestClient):
    headers = _register(client, password="correct-password")
    response = client.post(
        "/api/login", json={"email": headers["email"], "password": "wrong-password"}
    )
    assert response.status_code == 401
    # 不区分"用户不存在"与"口令不对"
    response2 = client.post(
        "/api/login", json={"email": "nobody@example.com", "password": "whatever123"}
    )
    assert response2.status_code == 401
    assert response.json()["detail"] == response2.json()["detail"]


def test_login_sets_httponly_cookie(client: TestClient):
    """计划第 858 行：token 存 httpOnly Cookie。"""
    headers = _register(client, password="correct-password")
    response = client.post(
        "/api/login", json={"email": headers["email"], "password": "correct-password"}
    )
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "access_token=" in set_cookie
    assert "HttpOnly" in set_cookie


# ============================================================
# 验收 1（续）：参数校验生效
# ============================================================


def test_register_rejects_short_password(client: TestClient):
    response = client.post(
        "/api/register", json={"email": _email(), "password": "short"}
    )
    assert response.status_code == 422


def test_unknown_fields_are_rejected(client: TestClient):
    """extra="forbid"：拼错字段名不该被静默忽略。"""
    response = client.post(
        "/api/register",
        json={"email": _email(), "password": "strong-password", "typo_field": 1},
    )
    assert response.status_code == 422


def test_create_project_rejects_empty_name(client: TestClient):
    headers = _register(client)
    response = client.post("/api/projects", json={"name": ""}, headers=headers)
    assert response.status_code == 422


def test_create_project_rejects_negative_budget(client: TestClient):
    headers = _register(client)
    response = client.post(
        "/api/projects", json={"name": "apitest-x", "budget_total": -5}, headers=headers
    )
    assert response.status_code == 422


def test_create_project_rejects_budget_below_storage_granularity(client: TestClient):
    """比 0.0001 更小的预算必须报错，而不是被四舍五入成 0（= 不设上限）。

    `budget_total` 列是 `Numeric(12,4)`。若放任 `0.00001` 进去，它会被存成
    0.0000，而 0 的语义是"未设预算、不拦" —— 用户以为卡得很死，
    实际等于完全不卡，方向正好是多花钱。
    """
    headers = _register(client)
    response = client.post(
        "/api/projects",
        json={"name": "apitest-too-small", "budget_total": 0.00001},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert "精度" in response.text or "0.0001" in response.text


def test_create_project_accepts_exact_granularity(client: TestClient):
    """边界值本身要能过：0.0001 是合法的最小金额。"""
    headers = _register(client)
    response = client.post(
        "/api/projects",
        json={"name": "apitest-min-budget", "budget_total": 0.0001},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["budget_total"] == pytest.approx(0.0001)


def test_data_source_rejects_unsupported_format(client: TestClient):
    headers = _register(client)
    project_id = _make_project(client, headers)
    response = client.post(
        f"/api/projects/{project_id}/data-sources",
        json={"format": "csv"},
        headers=headers,
    )
    assert response.status_code == 422


def test_run_request_rejects_naive_datetime(client: TestClient):
    """naive 时间会让时间窗口与幂等键悄悄错位，必须拒绝。"""
    headers = _register(client)
    project_id = _make_project(client, headers)
    response = client.post(
        f"/api/projects/{project_id}/analysis-runs",
        json={
            "data_source_id": 1,
            "time_range": {"start": "2026-09-23T12:00:00", "end": "2026-09-23T13:00:00"},
        },
        headers=headers,
    )
    assert response.status_code == 422


def test_run_request_rejects_inverted_time_range(client: TestClient):
    headers = _register(client)
    project_id = _make_project(client, headers)
    response = client.post(
        f"/api/projects/{project_id}/analysis-runs",
        json={
            "data_source_id": 1,
            "time_range": {
                "start": "2026-09-23T13:00:00+00:00",
                "end": "2026-09-23T12:00:00+00:00",
            },
        },
        headers=headers,
    )
    assert response.status_code == 422


def test_run_request_rejects_bad_severity_value(client: TestClient):
    headers = _register(client)
    project_id = _make_project(client, headers)
    response = client.post(
        f"/api/projects/{project_id}/analysis-runs",
        json={"data_source_id": 1, "filters": {"severity": ["critical"]}},
        headers=headers,
    )
    assert response.status_code == 422


def test_run_request_rejects_bad_tier(client: TestClient):
    headers = _register(client)
    project_id = _make_project(client, headers)
    response = client.post(
        f"/api/projects/{project_id}/analysis-runs",
        json={"data_source_id": 1, "start_tier": "L9"},
        headers=headers,
    )
    assert response.status_code == 422


# ============================================================
# 验收 3：/docs 可访问
# ============================================================


def test_docs_is_accessible(client: TestClient):
    assert client.get("/docs").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_openapi_lists_the_planned_endpoints(client: TestClient):
    """对照计划第 860–881 行的端点清单。"""
    paths = set(client.get("/openapi.json").json()["paths"])
    expected = {
        "/api/register",
        "/api/login",
        "/api/projects",
        "/api/projects/{project_id}/data-sources",
        "/api/projects/{project_id}/upload",
        "/api/projects/{project_id}/analysis-runs",
        "/api/runs/{run_id}",
        "/api/runs/{run_id}/insights",
        "/api/insights/{insight_id}",
        "/api/insights/{insight_id}/evidence",
        "/api/runs/{run_id}/cancel",
        "/api/projects/{project_id}/knowledge/candidates",
        "/api/projects/{project_id}/knowledge/confirmed",
        "/api/knowledge/candidates/{candidate_id}",
        "/api/knowledge/candidates/{candidate_id}/confirm",
        "/api/knowledge/candidates/{candidate_id}/reject",
        "/api/knowledge/candidates/{candidate_id}/false-positive",
    }
    missing = expected - paths
    assert not missing, f"缺少计划要求的端点：{sorted(missing)}"


# ============================================================
# 验收 4：跨 Project 访问被拒绝
# ============================================================


def test_other_users_project_is_not_visible(client: TestClient):
    owner = _register(client)
    intruder = _register(client)
    project_id = _make_project(client, owner)

    # 拥有者能看见
    assert client.get(f"/api/projects/{project_id}/data-sources", headers=owner).status_code == 200
    # 别人看不到（404 而非 403：不暴露该 id 是否存在）
    response = client.get(f"/api/projects/{project_id}/data-sources", headers=intruder)
    assert response.status_code == 404


def test_cross_project_data_source_is_refused_in_run_creation(client: TestClient):
    owner = _register(client)
    intruder = _register(client)
    owner_project = _make_project(client, owner)

    source = client.post(
        f"/api/projects/{owner_project}/data-sources",
        json={"format": "txt"},
        headers=owner,
    ).json()

    intruder_project = _make_project(client, intruder)
    response = client.post(
        f"/api/projects/{intruder_project}/analysis-runs",
        json={"data_source_id": source["id"]},
        headers=intruder,
    )
    # 数据源不属于该项目 → 404
    assert response.status_code == 404


def test_run_of_another_project_is_not_readable(client: TestClient):
    owner = _register(client)
    intruder = _register(client)
    owner_project = _make_project(client, owner)

    source = client.post(
        f"/api/projects/{owner_project}/data-sources",
        json={"format": "txt"},
        headers=owner,
    ).json()

    with patch("app.api.routes_runs._dispatch"):
        created = client.post(
            f"/api/projects/{owner_project}/analysis-runs",
            json={"data_source_id": source["id"]},
            headers=owner,
        )
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]

    assert client.get(f"/api/runs/{run_id}", headers=owner).status_code == 200
    assert client.get(f"/api/runs/{run_id}", headers=intruder).status_code == 404


def test_cancel_of_another_project_run_is_refused(client: TestClient):
    owner = _register(client)
    intruder = _register(client)
    owner_project = _make_project(client, owner)
    source = client.post(
        f"/api/projects/{owner_project}/data-sources",
        json={"format": "txt"},
        headers=owner,
    ).json()
    with patch("app.api.routes_runs._dispatch"):
        run_id = client.post(
            f"/api/projects/{owner_project}/analysis-runs",
            json={"data_source_id": source["id"]},
            headers=owner,
        ).json()["run_id"]

    assert client.post(f"/api/runs/{run_id}/cancel", headers=intruder).status_code == 404


# ============================================================
# 验收 2：分析端点立即返回、不阻塞
# ============================================================


def test_create_run_returns_immediately_with_run_id_and_queued(client: TestClient):
    """计划第 867 行：立即返回 `run_id + queued`。"""
    headers = _register(client)
    project_id = _make_project(client, headers)
    source = client.post(
        f"/api/projects/{project_id}/data-sources",
        json={"format": "txt"},
        headers=headers,
    ).json()

    with patch("app.api.routes_runs._dispatch") as dispatch:
        response = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={
                "data_source_id": source["id"],
                "time_range": {
                    "start": "2026-09-23T12:00:00+00:00",
                    "end": "2026-09-23T13:00:00+00:00",
                },
            },
            headers=headers,
        )

    assert response.status_code == 202
    body = response.json()
    assert body["run_id"] > 0
    assert body["status"] == "queued"
    assert body["reused"] is False
    # 关键：任务被**派发出去**而不是在这里执行
    assert dispatch.call_count == 1


def test_create_run_is_refused_when_budget_is_insufficient(client: TestClient):
    """阶段 07 验收 1（真实路径版）：预算不足**拒绝创建**。

    这条以前只在单测里成立 —— `CostController` 写得再全，真实路径里
    **没人构造过它**，所以真机上可以拿一个预算见底的项目一直发起分析。
    现在 Pre-check 接在创建端点上：402（不是 422 —— 这不是参数写错，是钱不够）。
    """
    _require_cost_gate_readable()
    headers = _register(client)
    # 预算小到连一次 L3 调用都覆盖不了（Pre-check 的估计约 ¥0.018，还叠加 10% 安全边际）。
    # 注意金额精度是 4 位小数：比 0.0001 更小的值会被数据库四舍五入成 0，
    # 而 0 的语义是"未设预算"——所以这里用 0.001 而不是 0.00001。
    project_id = _make_project(client, headers, name="apitest-tiny-budget")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE projects SET budget_total = 0.001 WHERE id = :i"),
            {"i": project_id},
        )
    source = client.post(
        f"/api/projects/{project_id}/data-sources",
        json={"format": "txt"},
        headers=headers,
    ).json()

    with patch("app.api.routes_runs._dispatch") as dispatch:
        response = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={
                "data_source_id": source["id"],
                "start_tier": "L3",
                "time_range": {
                    "start": "2026-09-23T12:00:00+00:00",
                    "end": "2026-09-23T13:00:00+00:00",
                },
            },
            headers=headers,
        )

    assert response.status_code == 402, response.text
    assert "预算" in response.json()["detail"] or "上限" in response.json()["detail"]
    # 关键：连任务都没派发出去
    assert dispatch.call_count == 0


def test_create_run_is_allowed_when_budget_is_unset(client: TestClient):
    """`budget_total = 0`（schema 默认值）视为**未设预算**，不该拦住创建。

    若把默认的 0 当成"预算为零"，所有没显式填预算的项目连一次分析都发不起来 ——
    那是把默认值当成了策略。真要有上限就填正数（或靠单次 run_max_cost 兜）。
    """
    headers = _register(client)
    project_id = _make_project(client, headers, name="apitest-no-budget")
    source = client.post(
        f"/api/projects/{project_id}/data-sources",
        json={"format": "txt"},
        headers=headers,
    ).json()

    with patch("app.api.routes_runs._dispatch"):
        response = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={
                "data_source_id": source["id"],
                "time_range": {
                    "start": "2026-09-23T12:00:00+00:00",
                    "end": "2026-09-23T13:00:00+00:00",
                },
            },
            headers=headers,
        )
    assert response.status_code == 202, response.text


def test_create_run_does_not_analyse_inline(client: TestClient):
    """端点里不能出现模型调用 —— 分析属 Worker。"""
    headers = _register(client)
    project_id = _make_project(client, headers)
    source = client.post(
        f"/api/projects/{project_id}/data-sources",
        json={"format": "txt"},
        headers=headers,
    ).json()

    with patch("app.api.routes_runs._dispatch"), patch(
        "app.gateways.router.Router.generate"
    ) as generate:
        client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={"data_source_id": source["id"]},
            headers=headers,
        )
    assert generate.call_count == 0, "创建 Run 的端点里不该调用模型"


def test_duplicate_submission_reuses_the_run(client: TestClient):
    """幂等：相同参数重复提交应复用同一个 Run，不重复执行也不重复计费。"""
    headers = _register(client)
    project_id = _make_project(client, headers)
    source = client.post(
        f"/api/projects/{project_id}/data-sources",
        json={"format": "txt"},
        headers=headers,
    ).json()
    payload = {
        "data_source_id": source["id"],
        "time_range": {
            "start": "2026-09-23T12:00:00+00:00",
            "end": "2026-09-23T13:00:00+00:00",
        },
    }

    with patch("app.api.routes_runs._dispatch"):
        first = client.post(
            f"/api/projects/{project_id}/analysis-runs", json=payload, headers=headers
        ).json()
        # 把它置为 completed，模拟上次跑成功
        with SessionLocal() as session:
            session.execute(
                text(
                    "UPDATE agent_runs SET status='running' WHERE id=:i"
                ),
                {"i": first["run_id"]},
            )
            session.execute(
                text("UPDATE agent_runs SET status='completed' WHERE id=:i"),
                {"i": first["run_id"]},
            )
            session.commit()
        second = client.post(
            f"/api/projects/{project_id}/analysis-runs", json=payload, headers=headers
        ).json()

    assert second["run_id"] == first["run_id"]
    assert second["reused"] is True


def test_run_status_endpoint_reports_progress_and_cost(client: TestClient):
    headers = _register(client)
    project_id = _make_project(client, headers)
    source = client.post(
        f"/api/projects/{project_id}/data-sources",
        json={"format": "txt"},
        headers=headers,
    ).json()
    with patch("app.api.routes_runs._dispatch"):
        run_id = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={"data_source_id": source["id"]},
            headers=headers,
        ).json()["run_id"]

    body = client.get(f"/api/runs/{run_id}", headers=headers).json()
    for field in (
        "id", "status", "current_phase", "phase_history",
        "tokens_input", "tokens_output", "cost_actual", "cancel_requested",
    ):
        assert field in body, f"RunResponse 缺少 {field}"
    assert body["status"] == "queued"


def test_cancel_endpoint_sets_the_flag_and_explains_granularity(client: TestClient):
    headers = _register(client)
    project_id = _make_project(client, headers)
    source = client.post(
        f"/api/projects/{project_id}/data-sources",
        json={"format": "txt"},
        headers=headers,
    ).json()
    with patch("app.api.routes_runs._dispatch"):
        run_id = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={"data_source_id": source["id"]},
            headers=headers,
        ).json()["run_id"]

    body = client.post(f"/api/runs/{run_id}/cancel", headers=headers).json()
    assert body["cancel_requested"] is True
    # 诚实说明粒度（计划第 710 行）
    assert "检查点" in body["note"]
    assert client.get(f"/api/runs/{run_id}", headers=headers).json()["cancel_requested"] is True


# ============================================================
# 上传端点
# ============================================================


def test_upload_persists_events_and_returns_stats(client: TestClient):
    headers = _register(client)
    project_id = _make_project(client, headers)

    content = (
        b"Jun 14 15:16:01 combo sshd(pam_unix)[19939]: authentication failure rhost=218.188.2.4\n"
        b"Jun 14 15:16:02 combo kernel: contact ops@example.com for help\n"
    )
    response = upload_and_wait(client,
        f"/api/projects/{project_id}/upload",
        files={"file": ("syslog.txt", content, "text/plain")},
        data={"fmt": "txt"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["events_persisted"] == 2
    assert body["parse"]["parsed"] == 2
    assert body["created_data_source"] is True
    # 脱敏计数要回传（阶段 11 要展示）
    assert body["mask"]["total"] >= 1


def test_upload_rejects_unsupported_format(client: TestClient):
    headers = _register(client)
    project_id = _make_project(client, headers)
    response = upload_and_wait(client,
        f"/api/projects/{project_id}/upload",
        files={"file": ("a.csv", b"a,b,c\n", "text/csv")},
        data={"fmt": "csv"},
        headers=headers,
    )
    assert response.status_code == 422


def test_upload_to_another_project_is_refused(client: TestClient):
    owner = _register(client)
    intruder = _register(client)
    project_id = _make_project(client, owner)
    response = upload_and_wait(client,
        f"/api/projects/{project_id}/upload",
        files={"file": ("a.txt", b"Jun 14 15:16:01 h a[1]: x\n", "text/plain")},
        data={"fmt": "txt"},
        headers=intruder,
    )
    assert response.status_code == 404


# ============================================================
# 知识审核端点
# ============================================================


def test_knowledge_endpoints_require_project_scope(client: TestClient):
    headers = _register(client)
    response = client.get("/api/projects/999999/knowledge/candidates", headers=headers)
    assert response.status_code == 404


def test_confirm_missing_candidate_is_404(client: TestClient):
    headers = _register(client)
    project_id = _make_project(client, headers)
    # 这些端点的路径里只有 candidate_id，故 project_id 走 query 参数
    response = client.post(
        "/api/knowledge/candidates/cand_nonexistent/confirm",
        params={"project_id": project_id},
        headers=headers,
    )
    assert response.status_code == 404


def test_confirm_without_project_id_is_422(client: TestClient):
    """缺 project_id 直接 422：不能靠"不传就跳过归属校验"绕过隔离。"""
    headers = _register(client)
    response = client.post(
        "/api/knowledge/candidates/cand_x/confirm", headers=headers
    )
    assert response.status_code == 422


def test_confirm_with_another_users_project_is_404(client: TestClient):
    """带别人的 project_id 也拿不到 —— 归属校验在依赖里完成。"""
    owner = _register(client)
    intruder = _register(client)
    project_id = _make_project(client, owner)
    response = client.post(
        "/api/knowledge/candidates/cand_x/confirm",
        params={"project_id": project_id},
        headers=intruder,
    )
    assert response.status_code == 404

# ============================================================
# 回归：派发任务前必须先提交（否则 worker 读到空数据）
# ============================================================


def test_run_creation_commits_before_dispatching(client: TestClient):
    """派发 Celery 任务前必须已提交，否则 worker 会读到一个空的世界。

    真实故障（2026-09-23 真机复现）：`POST /analysis-runs` 在请求事务提交
    **之前**就把任务放进 Redis；worker 用另一个连接读，看到 0 条事件 →
    判定 L0（无异常）→ 产出一份"已完成、零结论"的报告。
    API 侧一切正常、Run 状态是 completed，从外面完全看不出问题
    （Run 元数据里 context_tokens=0、model_attempts=[]，而库里其实有 16 条事件）。

    本测试用「在 _dispatch 被调用的那一刻，从**独立会话**查事件」来复现该竞态：
    若创建 Run 之前没有提交，事件对独立会话不可见，断言失败。
    """
    headers = _register(client)
    project_id = _make_project(client, headers)

    content = (
        b"Jun 14 15:16:01 combo kernel: Out of memory: Kill process 1234 (java)\n"
        b"Jun 14 15:16:02 combo kernel: segfault at 0 ip 00007f\n"
    )
    upload = upload_and_wait(client,
        f"/api/projects/{project_id}/upload",
        files={"file": ("syslog.txt", content, "text/plain")},
        data={"fmt": "txt"},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text
    assert upload.json()["events_persisted"] == 2
    source_id = upload.json()["data_source_id"]

    observed: dict[str, int] = {}

    def spy_dispatch(run_id: int, dispatched_project_id: int, request: object) -> None:
        # 模拟 worker：用**另一个会话**读数据。
        # 此刻若创建 Run 的事务尚未提交，这里会读到 0。
        with SessionLocal() as probe:
            observed["events"] = int(
                probe.execute(
                    text("SELECT count(*) FROM events WHERE project_id = :p"),
                    {"p": dispatched_project_id},
                ).scalar_one()
            )
            observed["run_visible"] = int(
                probe.execute(
                    text("SELECT count(*) FROM agent_runs WHERE id = :i"), {"i": run_id}
                ).scalar_one()
            )

    with patch("app.api.routes_runs._dispatch", side_effect=spy_dispatch) as dispatch:
        response = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={"data_source_id": source_id},
            headers=headers,
        )

    assert response.status_code == 202, response.text
    assert dispatch.call_count == 1
    assert observed["run_visible"] == 1, "派发时 Run 对独立会话不可见 —— 提交发生在派发之后"
    assert observed["events"] == 2, (
        f"派发时事件对独立会话不可见（读到 {observed['events']} 条）—— "
        "worker 会因此把有崩溃的日志判成 L0，产出零结论的『已完成』报告"
    )


# ============================================================
# 回归（2026-09-24 真机核验）：「重新分析」要能重建**生效的**时间窗
# ============================================================


def test_retry_rebuilds_the_effective_time_range(client: TestClient):
    """不填时间范围发起的 Run，也要能「重新分析」。

    真机核验：界面不填时间范围时，原始请求里 `time_range` 是 null，
    API 会用 `_epoch()..now` 兜底；但快照只照抄了原始请求 →
    重试解析时间窗必然失败，返回 422「原 Run 的时间范围无法解析」——
    即重试入口对**最常见**的发起方式不可用。
    """
    from unittest.mock import patch

    headers = _register(client)
    project_id = _make_project(client, headers)
    source = client.post(
        f"/api/projects/{project_id}/data-sources", json={"format": "txt"}, headers=headers
    )
    assert source.status_code == 201, source.text
    source_id = source.json()["id"]

    with patch("app.api.routes_runs._dispatch"):
        created = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={"data_source_id": source_id},  # 刻意不给 time_range
            headers=headers,
        )
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]

    # 快照里必须记下**生效的**窗口（而不是 null）
    with SessionLocal() as session:
        stored = session.execute(
            text("SELECT input -> 'time_range' FROM agent_runs WHERE id = :i"), {"i": run_id}
        ).scalar_one()
    assert stored, f"快照里没记生效时间窗：{stored!r}"

    # 让它可重试，然后再走一次真实的重试入口
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE agent_runs SET status = 'partial_success' WHERE id = :i"),
            {"i": run_id},
        )
    with patch("app.api.routes_runs._dispatch"):
        retried = client.post(f"/api/runs/{run_id}/retry", headers=headers)

    assert retried.status_code == 202, f"重试被拒：{retried.text}"
    new_run_id = retried.json()["run_id"]
    with SessionLocal() as session:
        parent = session.execute(
            text("SELECT parent_run_id FROM agent_runs WHERE id = :i"), {"i": new_run_id}
        ).scalar_one()
    assert parent == run_id, "新 Run 必须用 parent_run_id 指回原 Run"
