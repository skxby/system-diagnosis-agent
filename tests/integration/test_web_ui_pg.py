"""阶段 11 验收的集成测试（真实 PostgreSQL + TestClient）。

验收四条：
  从上传到看报告全程不碰命令行；
  fact 的证据可点击核对；
  runbook 可见；
  不完整 / 失败结果有显著提示与重试入口。

"可点击核对"与"显著提示"是**页面上**的行为，无法在 Python 里点按钮，
故这里验证的是：页面能渲染出承载这些行为的元素，且它们的数据来源（API）
确实返回了所需内容。真正的视觉验证属阶段 13 的 E2E。
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
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
            "DELETE FROM evidences WHERE insight_id IN "
            "(SELECT id FROM insights WHERE project_id IN "
            "(SELECT id FROM projects WHERE name LIKE 'uic-%'))"))
        conn.execute(text(
            "DELETE FROM insights WHERE project_id IN "
            "(SELECT id FROM projects WHERE name LIKE 'uic-%')"))
        conn.execute(text(
            "DELETE FROM events WHERE project_id IN "
            "(SELECT id FROM projects WHERE name LIKE 'uic-%')"))
        conn.execute(text(
            "DELETE FROM agent_runs WHERE project_id IN "
            "(SELECT id FROM projects WHERE name LIKE 'uic-%')"))
        conn.execute(text(
            "DELETE FROM data_sources WHERE project_id IN "
            "(SELECT id FROM projects WHERE name LIKE 'uic-%')"))
        conn.execute(text("DELETE FROM projects WHERE name LIKE 'uic-%'"))
        conn.execute(text("DELETE FROM users WHERE email LIKE 'uic-%@example.com'"))


def _signup(client: TestClient) -> dict:
    email = f"uic-{datetime.now(UTC).timestamp()}@example.com"
    response = client.post("/api/register", json={"email": email, "password": "strong-password"})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _project(client: TestClient, headers: dict) -> int:
    return client.post(
        "/api/projects",
        json={"name": f"uic-{datetime.now(UTC).timestamp()}"},
        headers=headers,
    ).json()["id"]


# ============================================================
# 页面可访问（"不碰命令行"的前提）
# ============================================================


@pytest.mark.parametrize(
    "path",
    ["/login", "/projects", "/projects/1", "/runs/1", "/runs/1/detail", "/report/1", "/knowledge/1"],
)
def test_pages_render(client: TestClient, path: str):
    response = client.get(path)
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_root_redirects_to_projects(client: TestClient):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/projects"


@pytest.mark.parametrize("asset", ["/static/app.css", "/static/app.js"])
def test_static_assets_are_served(client: TestClient, asset: str):
    response = client.get(asset)
    assert response.status_code == 200


def test_login_page_contains_the_login_flow(client: TestClient):
    html = client.get("/login").text
    # 登录与注册都走 /api/*，且不保存 token（httpOnly Cookie 由服务端下发）
    assert "/api/login" in html
    assert "/api/register" in html
    assert "localStorage" not in html, "httpOnly Cookie 方案下不该把 token 存到 localStorage"


# ============================================================
# 四层语义渲染（验收：fact 的证据可点击核对）
# ============================================================


def test_report_page_defines_all_four_semantics(client: TestClient):
    """计划第 833–836、907 行：✓ 确认 / → 推测 / ? 可能 / − 未知。"""
    html = client.get("/report/1").text
    for symbol in ("✓", "→", "?", "−"):
        assert symbol in html, f"报告页缺少语义符号 {symbol}"
    for label in ("确认", "推测", "可能", "未知"):
        assert label in html
    # 证据要可展开核对
    assert "evidence" in html
    assert "/evidence" in html


def test_report_page_has_incomplete_and_failure_banners(client: TestClient):
    """计划第 910–912 行：不完整要显著标注，失败要给重试入口。

    注意断言对象：报告页是**壳**，具体提示由 JS 依据 API 数据渲染。
    所以这里验证"模板里确实有渲染这些提示的代码路径"，
    再用 API 数据确认字段齐全（见 test_report_api_supplies_banner_fields）。
    """
    html = client.get("/report/1").text
    assert "结果不完整" in html, "缺少「结果不完整」的渲染逻辑"
    assert "partial_success" in html
    assert "中断原因" in html
    assert "失败阶段" in html


def test_report_page_renders_runbook_steps(client: TestClient):
    """计划第 909 行：命中异常附对应 runbook（只读展示）。"""
    html = client.get("/report/1").text
    assert "runbook" in html.lower()
    assert "处置步骤" in html
    # 明确只读，不自动执行（与只读定位一致）
    assert "不自动执行" in html


def test_report_page_has_knowledge_review_actions(client: TestClient):
    """计划第 913–915 行：确认 / 编辑 / 丢弃 / 这不是问题。"""
    html = client.get("/report/1").text
    assert "待确认知识" in html
    assert "confirm" in html
    assert "false-positive" in html
    assert "reject" in html


def test_run_status_page_has_progress_and_cancel(client: TestClient):
    html = client.get("/runs/1").text
    assert "阶段进度" in html
    assert "/cancel" in html
    assert "3000" in html or "3 秒" in html, "状态页应轮询"


def test_run_status_page_handles_timeout_and_retry(client: TestClient):
    html = client.get("/runs/1").text
    assert "timeout" in html
    assert "心跳" in html


# ============================================================
# 端到端：上传 → 发起分析 → 看报告（全程 HTTP）
# ============================================================


def test_full_flow_over_http_without_touching_a_terminal(client: TestClient):
    """验收：「从上传到看报告全程不碰命令行」——这里把四步都用 HTTP 走一遍。"""
    headers = _signup(client)
    project_id = _project(client, headers)

    # ① 上传（页面上的"上传并解析"按钮做的就是这件事）
    upload = client.post(
        f"/api/projects/{project_id}/upload",
        files={"file": ("syslog.txt",
                        b"Jun 14 15:16:01 combo kernel: Out of memory: Kill process 1234\n",
                        "text/plain")},
        data={"fmt": "txt"},
        headers=headers,
    )
    assert upload.status_code == 200, upload.text
    source_id = upload.json()["data_source_id"]

    # ② 发起分析（立即返回）
    with patch("app.api.routes_runs._dispatch"):
        created = client.post(
            f"/api/projects/{project_id}/analysis-runs",
            json={"data_source_id": source_id},
            headers=headers,
        )
    assert created.status_code == 202, created.text
    run_id = created.json()["run_id"]

    # ③ 状态页能拿到数据
    status = client.get(
        f"/api/runs/{run_id}", params={"project_id": project_id}, headers=headers
    )
    assert status.status_code == 200
    assert status.json()["status"] == "queued"

    # ④ 报告页可打开（数据为空也应当正常渲染，不报错）
    report = client.get(f"/report/{run_id}", params={"project_id": project_id})
    assert report.status_code == 200
    assert "分析报告" in report.text

    # 页面链路完整：项目详情页的 JS 会拼出「查看 Run #id 状态」的链接。
    # 链接是运行时生成的，服务端 HTML 里只有拼接模板，故断言模板本身。
    project_html = client.get(f"/projects/{project_id}").text
    assert "/runs/${result.run_id}" in project_html, "项目详情页应有跳转 Run 状态页的链接逻辑"
    assert "analysis-runs" in project_html


def test_insight_api_returns_runbook_snapshot(client: TestClient):
    """runbook 通过 Insight.run_metadata 暴露给页面。"""
    headers = _signup(client)
    project_id = _project(client, headers)

    with SessionLocal() as session:
        from app.models import AgentRun, DataSource, Insight

        session.add(DataSource(project_id=project_id, type="file_upload", format="txt", location="a"))
        session.flush()
        source_id = session.query(DataSource).filter_by(project_id=project_id).one().id
        run = AgentRun(project_id=project_id, source_id=source_id, status="completed")
        session.add(run)
        session.flush()
        session.add(Insight(
            project_id=project_id, run_id=run.id, type="fact", severity="high",
            confidence=0.9, title="OOM 结论", summary="s",
            run_metadata={"runbooks": [{"id": "rb_oom_001", "title": "内存耗尽 / OOM 处置",
                                        "steps": ["确认内存水位", "定位占用最高的进程"],
                                        "references": []}]},
        ))
        session.commit()
        insight_id, run_id = session.query(Insight).filter_by(project_id=project_id).one().id, run.id

    response = client.get(
        f"/api/insights/{insight_id}", params={"project_id": project_id}, headers=headers
    )
    assert response.status_code == 200
    body = response.json()
    assert body["run_metadata"]["runbooks"][0]["id"] == "rb_oom_001"
    assert body["run_metadata"]["runbooks"][0]["steps"]

    listed = client.get(
        f"/api/runs/{run_id}/insights", params={"project_id": project_id}, headers=headers
    )
    assert listed.status_code == 200
    assert listed.json()[0]["run_metadata"]["runbooks"]


def test_page_does_not_leak_another_users_data(client: TestClient):
    """页面不是安全边界，但也不该主动渲染别人的数据。

    页面本身不校验登录（JS 调 API 会拿到 401 并被送回登录页），
    真正把门的是 API —— 这里确认 API 侧确实把住了。
    """
    owner = _signup(client)
    intruder = _signup(client)
    project_id = _project(client, owner)

    # 页面壳对任何人都返回 200（它不含数据）
    assert client.get(f"/projects/{project_id}").status_code == 200
    # 但数据接口对别人 404
    assert client.get(
        f"/api/projects/{project_id}/data-sources", headers=intruder
    ).status_code == 404
    assert client.get(
        f"/api/projects/{project_id}/knowledge/candidates", headers=intruder
    ).status_code == 404
