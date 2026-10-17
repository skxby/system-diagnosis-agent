"""阶段 08 验收的集成测试（真实 PostgreSQL）。

对应计划第 731 行验收：
  创建 Run 立即返回 run_id + queued；
  手动 kill Worker 后僵尸 Run 被回收；
  重复提交不产生重复 Run；
  取消能在下一个检查点生效；
  模型不可用时能降级到 L0 报告；
  各类错误分类正确。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.heartbeat import DEFAULT_HEARTBEAT_TIMEOUT_SECONDS, CancelledError
from app.analysis.idempotency import ErrorKind, InputFormatError
from app.analysis.retry import RetryPolicy
from app.analysis.runner import RunExecutor, RunRequest, create_run
from app.db import SessionLocal, engine
from app.gateways.base import ModelUnavailableError
from app.models import DataSource, Project, User, enums
from app.repositories import (
    AgentRunRepository,
    DataSourceRepository,
    ProjectRepository,
    UserRepository,
)

pytestmark = pytest.mark.integration
UTC = timezone.utc
T0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


@pytest.fixture()
def session():
    s = SessionLocal()
    try:
        yield s
        s.rollback()
    finally:
        s.close()


@pytest.fixture()
def ctx(session):
    users = UserRepository(session)
    user = users.add(
        User(
            email=f"stage08-{datetime.now(UTC).timestamp()}@example.com",
            password_hash="x",
        )
    )
    session.flush()
    project = ProjectRepository(session).add(
        Project(user_id=user.id, name="p08", budget_total=10, budget_used=0)
    )
    session.flush()
    source = DataSourceRepository(session).add(
        project.id,
        DataSource(
            project_id=project.id, type="file_upload", format="txt", location="a.log"
        ),
    )
    session.flush()
    runs = AgentRunRepository(session)
    return project, source, runs


def _request(project, source, **overrides) -> RunRequest:
    base = {
        "project_id": project.id,
        "source_id": source.id,
        "time_start": datetime(1970, 1, 1, tzinfo=UTC),
        "time_end": datetime(2100, 1, 1, tzinfo=UTC),
        "domain_id": "computer_monitoring",
        "domain_version": "1.0.0",
        "filters": {},
        "start_tier": "L2",
    }
    base.update(overrides)
    return RunRequest(**base)


def _executor(runs, **kwargs) -> RunExecutor:
    defaults = {"retry_policy": RetryPolicy(max_retries=0)}
    defaults.update(kwargs)
    return RunExecutor(run_repository=runs, **defaults)


# ============================================================
# 验收：创建 Run 立即返回 run_id + queued
# ============================================================


def test_create_run_returns_run_id_and_queued_immediately(session, ctx):
    project, source, runs = ctx
    run_id, status, reused = create_run(runs, _request(project, source))
    session.flush()

    assert isinstance(run_id, int) and run_id > 0
    assert status == enums.AGENT_RUN_QUEUED
    assert reused is False

    stored = runs.get(project.id, run_id)
    assert stored.status == enums.AGENT_RUN_QUEUED
    # 创建时不该有任何执行痕迹
    assert stored.started_at is None
    assert stored.model_calls is None


def test_create_run_persists_the_idempotency_key(session, ctx):
    project, source, runs = ctx
    request = _request(project, source)
    run_id, _, _ = create_run(runs, request)
    session.flush()

    assert runs.get(project.id, run_id).idempotency_key == request.idempotency_key()


# ============================================================
# 验收：重复提交不产生重复 Run
# ============================================================


def test_same_request_is_reused_without_creating_a_second_run(session, ctx):
    """计划第 703 行：相同键且已有成功 Run → 直接复用，不重复执行/计费。"""
    project, source, runs = ctx
    request = _request(project, source)

    first_id, _, _ = create_run(runs, request)
    session.flush()
    # 把它标记为已完成（模拟上次跑成功）
    runs.apply_status(project.id, first_id, target=enums.AGENT_RUN_RUNNING)
    runs.apply_status(project.id, first_id, target=enums.AGENT_RUN_COMPLETED)
    session.flush()

    second_id, status, reused = create_run(runs, _request(project, source))
    session.flush()

    assert reused is True
    assert second_id == first_id, "应复用同一个 Run"
    assert status == enums.AGENT_RUN_COMPLETED
    assert runs.count(project.id) == 1, "不应产生第二个 Run"


def test_different_filters_produce_a_different_run(session, ctx):
    """幂等键包含 filters：条件不同就是不同的分析。"""
    project, source, runs = ctx
    first_id, _, _ = create_run(
        runs, _request(project, source, filters={"severity": ["high"]})
    )
    session.flush()
    second_id, _, reused = create_run(
        runs, _request(project, source, filters={"severity": ["low"]})
    )
    session.flush()

    assert reused is False
    assert second_id != first_id
    assert runs.count(project.id) == 2


def test_partial_success_is_not_reused_as_a_completed_result(session, ctx):
    """partial_success 本身不完整，拿它当"已有结果"会把不完整当完整交付。"""
    project, source, runs = ctx
    request = _request(project, source)
    first_id, _, _ = create_run(runs, request)
    session.flush()
    runs.apply_status(project.id, first_id, target=enums.AGENT_RUN_RUNNING)
    runs.apply_status(project.id, first_id, target=enums.AGENT_RUN_PARTIAL_SUCCESS)
    session.flush()

    second_id, status, reused = create_run(runs, _request(project, source))
    session.flush()

    assert reused is False
    assert second_id != first_id
    assert status == enums.AGENT_RUN_QUEUED


# ============================================================
# 验收：手动 kill Worker 后僵尸 Run 被回收
# ============================================================


def test_zombie_run_is_reclaimed_as_timeout(session, ctx):
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_RUNNING)
    # 模拟 Worker 被杀：心跳停在很久以前
    stale = datetime.now(UTC) - timedelta(seconds=1000)
    runs.touch_heartbeat(project.id, run_id, at=stale)
    session.flush()

    reclaimed = runs.reclaim_zombies(timeout_seconds=300)
    session.flush()
    session.expire_all()

    assert run_id in reclaimed
    stored = runs.get(project.id, run_id)
    assert stored.status == enums.AGENT_RUN_TIMEOUT
    assert stored.finished_at is not None
    assert stored.run_metadata["stop_reason"] == "timeout"
    assert "心跳丢失" in stored.run_metadata["note"]


def test_terminal_status_write_keeps_earlier_run_metadata(session, ctx):
    """写终态时**不许**把 `run_metadata` 里已有的键冲掉。

    真机现象：API 创建 Run 时写进去的 trace_id，在 Worker 跑完后就不见了 ——
    报告页复述变成「trace 未记录」，计划第 928 行说的
    「事后用 trace_id 把这次分析的日志全捞出来」直接落空。

    根因：`apply_status` 用 `run.run_metadata = metadata` **覆盖**写入，
    而它带的只有 completed_phases / skipped_phases / attempts。
    最阴险的是触发条件 —— 只有"分析真的跑完"才会覆盖：Run 卡在 queued 时
    trace_id 反而还在（真机上先看到的就是这种自相矛盾的两条记录）。

    同文件里的僵尸回收走的是合并写法，两处语义不一致本身就是线索。
    """
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    # 模拟创建 Run 的那次 HTTP 请求写下的 trace_id（与 routes_runs 一致）
    created = runs.get(project.id, run_id)
    created.run_metadata = {"trace_id": "trace-abc123", "policy": {"max_calls": 20}}
    session.flush()

    _executor(runs).execute(
        project_id=project.id,
        run_id=run_id,
        attempt_tier=lambda tier: "ok",
        start_tier="L1",
    )
    session.flush()
    session.expire_all()

    stored = runs.get(project.id, run_id)
    assert stored.status == enums.AGENT_RUN_COMPLETED
    assert stored.run_metadata.get("trace_id") == "trace-abc123", (
        f"终态写入把 trace_id 冲掉了：{stored.run_metadata}"
    )
    assert stored.run_metadata.get("policy") == {"max_calls": 20}
    # 终态自己的键当然也要在
    assert "completed_phases" in stored.run_metadata
    assert "attempts" in stored.run_metadata


def test_fresh_run_is_not_reclaimed(session, ctx):
    """阈值必须大于单次最慢调用，否则正在正常工作的 Run 会被误杀。"""
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_RUNNING)
    runs.touch_heartbeat(project.id, run_id, at=datetime.now(UTC))
    session.flush()

    assert runs.reclaim_zombies(timeout_seconds=300) == []
    session.expire_all()
    assert runs.get(project.id, run_id).status == enums.AGENT_RUN_RUNNING


def test_queued_run_is_not_reclaimed_by_heartbeat(session, ctx):
    """queued 还没被 Worker 接手，没有心跳是正常的。"""
    project, source, runs = ctx
    _run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    assert runs.reclaim_zombies(timeout_seconds=300) == []


def test_terminal_run_is_never_reclaimed(session, ctx):
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_RUNNING)
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_COMPLETED)
    session.flush()

    assert runs.reclaim_zombies(timeout_seconds=1, project_id=project.id) == []
    session.expire_all()
    assert runs.get(project.id, run_id).status == enums.AGENT_RUN_COMPLETED


# ============================================================
# 验收：取消能在下一个检查点生效
# ============================================================


def test_cancel_flag_persists_and_is_visible(session, ctx):
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    assert runs.is_cancel_requested(project.id, run_id) is False
    assert runs.request_cancel(project.id, run_id) is True
    session.flush()
    assert runs.is_cancel_requested(project.id, run_id) is True

    # 幂等：重复置位无副作用
    assert runs.request_cancel(project.id, run_id) is True
    session.flush()
    assert runs.is_cancel_requested(project.id, run_id) is True


def test_cancel_takes_effect_at_the_next_checkpoint(session, ctx):
    """取消打在检查点前，执行器应在下一个检查点抛出并置 cancelled。"""
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    runs.request_cancel(project.id, run_id)
    session.flush()

    executor = _executor(runs)
    called: list[str] = []

    outcome = executor.execute(
        project_id=project.id,
        run_id=run_id,
        attempt_tier=lambda tier: called.append(tier) or "should-not-happen",
        start_tier="L2",
    )
    session.flush()
    session.expire_all()

    assert outcome.status == enums.AGENT_RUN_CANCELLED
    assert called == [], "取消后不该再发起模型调用"
    assert runs.get(project.id, run_id).status == enums.AGENT_RUN_CANCELLED


def test_check_cancel_raises_cancelled_error(session, ctx):
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    runs.request_cancel(project.id, run_id)
    session.flush()

    with pytest.raises(CancelledError):
        _executor(runs).check_cancel(project.id, run_id)


# ============================================================
# 验收：模型不可用时能降级到 L0 报告
# ============================================================


def test_model_unavailable_falls_back_to_rules_only_partial_success(session, ctx):
    """计划第 724–725 行：全等级失败 → 纯规则 L0 报告 + partial_success
    + stop_reason=model_unavailable。"""
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    def always_unavailable(tier: str) -> str:
        raise ModelUnavailableError("503 服务不可用")

    executor = RunExecutor(run_repository=runs, retry_policy=RetryPolicy(max_retries=0))
    outcome = executor.execute(
        project_id=project.id,
        run_id=run_id,
        attempt_tier=always_unavailable,
        start_tier="L3",
        rules_only_fallback=lambda: "analyzer 的确定性结果",
    )
    session.flush()
    session.expire_all()

    assert outcome.status == enums.AGENT_RUN_PARTIAL_SUCCESS
    assert outcome.used_rules_only is True
    assert outcome.stop_reason == "model_unavailable"
    assert outcome.value == "analyzer 的确定性结果"

    stored = runs.get(project.id, run_id)
    assert stored.status == enums.AGENT_RUN_PARTIAL_SUCCESS
    assert stored.run_metadata["stop_reason"] == "model_unavailable"

    # 降级链确实走过 L3 → L2 → L1
    tiers_tried = [a["tier"] for a in outcome.attempts]
    assert tiers_tried == ["L3", "L2", "L1"]


def test_successful_run_marks_completed(session, ctx):
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    outcome = _executor(runs).execute(
        project_id=project.id,
        run_id=run_id,
        attempt_tier=lambda tier: f"result-{tier}",
        start_tier="L2",
    )
    session.flush()
    session.expire_all()

    assert outcome.status == enums.AGENT_RUN_COMPLETED
    assert outcome.used_rules_only is False
    assert runs.get(project.id, run_id).status == enums.AGENT_RUN_COMPLETED


# ============================================================
# 验收：各类错误分类正确（且失败不静默）
# ============================================================


def test_input_error_fails_the_run_with_reason(session, ctx):
    """计划第 728 行：失败不静默 —— 置 failed 并写明阶段与原因。"""
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    def bad_input(tier: str) -> str:
        raise InputFormatError("日志格式不认识")

    outcome = _executor(runs).execute(
        project_id=project.id,
        run_id=run_id,
        attempt_tier=bad_input,
        start_tier="L1",
    )
    session.flush()
    session.expire_all()

    assert outcome.status == enums.AGENT_RUN_FAILED
    assert outcome.stop_reason == ErrorKind.INPUT
    assert "InputFormatError" in (outcome.error or "")

    stored = runs.get(project.id, run_id)
    assert stored.status == enums.AGENT_RUN_FAILED
    assert "InputFormatError" in (stored.error or "")


def test_unexpected_exception_still_terminates_the_run(session, ctx):
    """任何未预期异常也必须以明确状态收尾，不留 running 悬挂。"""
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    def boom(tier: str) -> str:
        raise ZeroDivisionError("unexpected")

    outcome = _executor(runs).execute(
        project_id=project.id, run_id=run_id, attempt_tier=boom, start_tier="L1"
    )
    session.flush()
    session.expire_all()

    assert outcome.status == enums.AGENT_RUN_FAILED
    assert runs.get(project.id, run_id).status == enums.AGENT_RUN_FAILED


def test_executor_never_leaves_a_run_in_running(session, ctx):
    """两种结局各跑一遍，都不该留下 running。"""
    project, source, runs = ctx

    scenarios = (
        (lambda tier: "ok", enums.AGENT_RUN_COMPLETED),
        (
            lambda tier: (_ for _ in ()).throw(InputFormatError("bad")),
            enums.AGENT_RUN_FAILED,
        ),
    )

    for index, (attempt, expected) in enumerate(scenarios):
        # 用 index 作为 filters 的一部分，保证两个场景的幂等键不同
        run_id, _, _ = create_run(
            runs, _request(project, source, filters={"scenario": index})
        )
        session.flush()
        _executor(runs).execute(
            project_id=project.id, run_id=run_id, attempt_tier=attempt, start_tier="L1"
        )
        session.flush()
        session.expire_all()
        assert runs.get(project.id, run_id).status == expected


def test_heartbeat_is_updated_during_execution(session, ctx):
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()

    seen: list[datetime | None] = []

    def probe(tier: str) -> str:
        seen.append(runs.get(project.id, run_id).last_heartbeat)
        return "ok"

    _executor(runs).execute(
        project_id=project.id, run_id=run_id, attempt_tier=probe, start_tier="L1"
    )
    session.flush()
    session.expire_all()

    stored = runs.get(project.id, run_id)
    assert stored.last_heartbeat is not None, "执行期必须写心跳，否则僵尸回收无从判断"


# ============================================================
# 回归：Worker 任务必须自己提交它改的东西
# ============================================================


def test_worker_task_commits_its_own_changes(session, ctx):
    """`_execute` 必须 commit，不能只 flush。

    这是被"真起全栈跑一遍"逼出来的 bug：任务自己管会话（不像 HTTP 路由由
    get_db 依赖负责提交），只 flush 的话 `session.close()` 会把状态机结果、
    回填的 token/成本、落库的结论与证据**全部回滚**，Run 永远停在 queued。
    而返回值仍然是对的 —— "任务报告成功"与"库里没变"能同时成立，所以
    只测函数返回值是发现不了的。

    本测试刻意从一个**独立会话**复查，模拟真实的跨进程可见性问题。
    """
    from unittest.mock import patch

    from sqlalchemy import text as _text

    from app.db import SessionLocal
    from app.tasks.analysis import _execute

    # 需要一个完整可用的 project（含 user），用现有 ctx 的项目
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.commit()

    class _FakeSettings:
        model_provider_base_url = "https://example.invalid"
        secret_key = "test-secret"
        default_timezone = "Asia/Shanghai"
        data_dir = "./data"
        model_l1 = model_l2 = model_l3 = "test-model"
        model_l1_reasoning = model_l2_reasoning = model_l3_reasoning = "off"
        model_l1_price_input_per_1m = model_l1_price_output_per_1m = 1.0
        model_l2_price_input_per_1m = model_l2_price_output_per_1m = 1.0
        model_l3_price_input_per_1m = model_l3_price_output_per_1m = 1.0

    # 让模型调用必然失败：走"降级到纯规则报告"这条路径（partial_success）
    def _boom(*args, **kwargs):
        raise RuntimeError("模型不可达（测试桩）")

    with (
        patch("app.config.get_settings", return_value=_FakeSettings()),
        patch("app.gateways.router.build_router", side_effect=_boom),
    ):
        out = _execute(
            session_factory=SessionLocal,
            run_id=run_id,
            project_id=project.id,
            start_tier="L2",
        )

    assert out["run_id"] == run_id

    # 关键断言：换一个会话读，状态必须已经落库
    fresh = SessionLocal()
    try:
        status = fresh.execute(
            _text("SELECT status FROM agent_runs WHERE id = :i"), {"i": run_id}
        ).scalar_one()
        metadata = fresh.execute(
            _text("SELECT run_metadata FROM agent_runs WHERE id = :i"), {"i": run_id}
        ).scalar_one()
    finally:
        fresh.close()

    assert status != enums.AGENT_RUN_QUEUED, (
        f"Worker 报告了 {out['status']}，但库里仍是 {status} —— 说明任务没有提交"
    )
    # 空输入走 L0，不应为未使用的模型配置而失败；仍须在独立会话看到完整产物。
    assert status == enums.AGENT_RUN_COMPLETED
    assert metadata["tier"] == "L0"
    assert metadata["rules_only_report"]["kind"] == "rules_only"


# ============================================================
# 回归：Worker 装载的事件必须让 analyzer 看得见进程名（metadata）
# ============================================================


def test_worker_loads_event_metadata_so_analyzers_fire(session, ctx, work_tmp):
    """Worker 装载的事件必须带 `metadata`，否则整条分析会**静默**退化成零结论。

    被"真起全栈跑一遍"逼出来的第二个 bug，比第一个更隐蔽：

    Worker 早先手写了一份 ORM→链路的事件映射，唯独漏了 `metadata`
    （进程名 `proc` 在里面）。后果是一条完整的连锁反应 ——
    `ProcessCrash` 看不到 `CrashReporterSupportHelper` 这类崩溃上报进程
    → 候选异常 0 → 复杂度判定为 L0 → 走"纯代码统计报告"分支，
    **模型一次都没被调用** → Run 以 `completed` 收场、0 条结论、¥0 花费。

    每一环都"成功"：任务返回值正常、状态机正常、详情页正常。
    只有把真实日志喂进真实 Worker 路径才会暴露，所以本测试走的是
    「上传真实的 crash 数据集 → `_execute` → 读库」这条完整链路。
    """
    from pathlib import Path
    from unittest.mock import patch

    from app.repositories.event import EventRepository
    from app.services.upload_service import UploadService
    from app.tasks.analysis import _execute

    project, source, runs = ctx

    # ① 用真实数据集走真实上传管道（脱敏→落盘→解析→入库）
    dataset = Path(__file__).resolve().parents[1] / "datasets" / "crash" / "system.log"
    content = dataset.read_bytes()
    uploaded = UploadService(session, uploads_dir=work_tmp / "uploads").ingest(
        project_id=project.id,
        content=content,
        filename="system.log",
        fmt="txt",
        data_source_id=source.id,
    )
    assert uploaded.events_persisted > 0, "数据集必须真的入库，否则测的是空集合"
    session.commit()

    # ② 装载器本身：`proc` 必须出现在事件字典里
    events = EventRepository(session).pipeline_events(project.id)
    procs = [(e.get("metadata") or {}).get("proc") for e in events]
    assert any(procs), (
        "事件字典里没有 metadata.proc —— analyzer 只能看到消息，"
        "崩溃上报进程（CrashReporterSupportHelper）会被整批漏掉"
    )

    real_ids = [str(e["event_id"]) for e in events]

    # ③ 驱动真实 Worker 路径。router 用桩：只验证"模型确实被调到"，
    #    不在这里花真钱（真实网关由 test_gateway_live.py 覆盖）。
    class _StubResult:
        tokens_input = 123
        tokens_output = 45
        cost = 0.0001

        def __init__(self) -> None:
            self.parsed = {
                "insights": [
                    {
                        "type": "fact",
                        "severity": "high",
                        "confidence": 0.9,
                        "title": "崩溃上报进程异常",
                        "summary": "CrashReporterSupportHelper 反复上报，疑有进程崩溃。",
                        "evidence_ids": [real_ids[0]],
                        "reasoning": "进程名命中 crash 模式",
                        "limitations": "单文件统计，无跨文件基线",
                    }
                ],
                "knowledge_candidates": [],
            }

    calls: list[dict] = []

    class _StubRouter:
        def generate(self, **kwargs):
            calls.append(kwargs)
            return _StubResult()

    class _FakeSettings:
        model_provider_base_url = "https://example.invalid"
        secret_key = "test-secret"
        default_timezone = "Asia/Shanghai"
        data_dir = "./data"
        model_l1 = model_l2 = model_l3 = "test-model"
        model_l1_reasoning = model_l2_reasoning = model_l3_reasoning = "off"
        model_l1_price_input_per_1m = model_l1_price_output_per_1m = 1.0
        model_l2_price_input_per_1m = model_l2_price_output_per_1m = 1.0
        model_l3_price_input_per_1m = model_l3_price_output_per_1m = 1.0

    run_id, _, _ = create_run(runs, _request(project, source))
    session.commit()

    with (
        patch("app.config.get_settings", return_value=_FakeSettings()),
        patch("app.gateways.router.build_router", return_value=_StubRouter()),
    ):
        out = _execute(
            session_factory=SessionLocal,
            run_id=run_id,
            project_id=project.id,
            start_tier="L2",
        )

    assert calls, (
        "模型一次都没被调用 —— 说明复杂度落到了 L0（纯规则报告），"
        "而这正是漏装 metadata 的症状"
    )
    assert out["status"] == enums.AGENT_RUN_COMPLETED
    assert out["insights"]["insight_count"] >= 1, (
        f"模型产出了结论却没落库：{out['insights']}"
    )

    # ④ 从独立会话复查落库结果：tier 不是 L0，且成本/token 已回填
    fresh = SessionLocal()
    try:
        from sqlalchemy import text as _text

        payload = fresh.execute(
            _text(
                "SELECT run_metadata, tokens_input, cost_actual FROM agent_runs WHERE id = :i"
            ),
            {"i": run_id},
        ).one()
    finally:
        fresh.close()

    metadata, tokens_input, cost = payload
    assert metadata.get("used_rules_only") is False
    assert metadata.get("model_attempts"), "模型调用必须留痕，否则详情页显示 0 次调用"
    assert tokens_input == 123 and float(cost) > 0


def test_persistence_failure_ends_the_run_as_failed_not_queued(session, ctx, work_tmp):
    """结论写不进库时，Run 必须以 `failed` + 真实原因收尾。

    第三个"真起全栈跑一遍"逼出来的 bug，也是最难查的一个：

    落库原先在 `executor.execute()` **之后**做。可那时 Run 已经是终态
    （completed）—— 状态机明令终态不可再改 —— 一旦落库抛异常逃出任务，
    `session.close()` 会把整个事务回滚：**Run 永远停在 queued**。
    页面上一切正常，Celery 只在自己日志里记了一笔，报告页看到的只是
    "排队中"。这是红线 4 最典型的违反：失败发生了，但没人看得见。

    修法有两处，本测试同时覆盖：
      ① 落库挪进 `attempt_tier`（终态之前），失败即一次"等级尝试失败"；
      ② 逃逸异常由外层兜底写进 Run，绝不留下无声的 queued。
    """
    from pathlib import Path
    from unittest.mock import patch

    from sqlalchemy import text as _text
    from sqlalchemy.exc import IntegrityError

    from app.services.upload_service import UploadService
    from app.tasks.analysis import _execute

    project, source, runs = ctx

    dataset = Path(__file__).resolve().parents[1] / "datasets" / "crash" / "system.log"
    uploaded = UploadService(session, uploads_dir=work_tmp / "uploads").ingest(
        project_id=project.id,
        content=dataset.read_bytes(),
        filename="system.log",
        fmt="txt",
        data_source_id=source.id,
    )
    assert uploaded.events_persisted > 0
    session.commit()

    from app.repositories.event import EventRepository

    real_ids = [
        str(e["event_id"]) for e in EventRepository(session).pipeline_events(project.id)
    ]

    class _StubResult:
        tokens_input = 10
        tokens_output = 20
        cost = 0.0005

        def __init__(self) -> None:
            self.parsed = {
                "insights": [
                    {
                        "type": "inference",
                        "severity": "medium",
                        "confidence": 0.6,
                        "title": "疑似崩溃",
                        "summary": "崩溃上报进程反复出现。",
                        "evidence_ids": [real_ids[0]],
                        "reasoning": "进程名命中 crash",
                    }
                ],
                "knowledge_candidates": [],
            }

    calls: list[str] = []

    class _StubRouter:
        def generate(self, **kwargs):
            calls.append(kwargs.get("tier"))
            return _StubResult()

    class _FakeSettings:
        model_provider_base_url = "https://example.invalid"
        secret_key = "test-secret"
        default_timezone = "Asia/Shanghai"
        data_dir = "./data"
        model_l1 = model_l2 = model_l3 = "test-model"
        model_l1_reasoning = model_l2_reasoning = model_l3_reasoning = "off"
        model_l1_price_input_per_1m = model_l1_price_output_per_1m = 1.0
        model_l2_price_input_per_1m = model_l2_price_output_per_1m = 1.0
        model_l3_price_input_per_1m = model_l3_price_output_per_1m = 1.0

    run_id, _, _ = create_run(runs, _request(project, source))
    session.commit()

    # 模拟落库被数据库拒绝（真机上就是 CHECK 约束不认某个值）
    def _reject(*args, **kwargs):
        raise IntegrityError("INSERT INTO insights", {}, Exception("CHECK 约束拒绝"))

    with (
        patch("app.config.get_settings", return_value=_FakeSettings()),
        patch("app.gateways.router.build_router", return_value=_StubRouter()),
        patch("app.analysis.persistence.persist_insights", side_effect=_reject),
    ):
        out = _execute(
            session_factory=SessionLocal,
            run_id=run_id,
            project_id=project.id,
            start_tier="L2",
        )

    assert out["status"] == enums.AGENT_RUN_FAILED, (
        f"落库失败必须如实置 failed，实际 {out['status']}"
    )
    # 关键：落库失败发生在钱花完之后，只许调一次模型，不许再降级重试烧钱。
    # （这里记的是链路算出的复杂度等级，不是降级链的起始等级。）
    assert len(calls) == 1, f"落库失败不该继续降级调模型，实际调用 {calls}"

    fresh = SessionLocal()
    try:
        status, error, metadata = fresh.execute(
            _text("SELECT status, error, run_metadata FROM agent_runs WHERE id = :i"),
            {"i": run_id},
        ).one()
    finally:
        fresh.close()

    assert status == enums.AGENT_RUN_FAILED, "Run 不许留在 queued/running"
    assert error and "落库失败" in error, f"失败原因必须落库，实际 error={error!r}"
    assert metadata.get("attempts"), "降级链的尝试明细必须留痕"

    # 失败归失败，已经花掉的 token 与成本仍要如实记录（钱是真花了）
    fresh = SessionLocal()
    try:
        tokens_input, cost = fresh.execute(
            _text("SELECT tokens_input, cost_actual FROM agent_runs WHERE id = :i"),
            {"i": run_id},
        ).one()
    finally:
        fresh.close()
    assert tokens_input == 10 and float(cost) > 0


# ============================================================
# 回归：月度预算的取数（cost_sum_since）
# ============================================================


def test_cost_sum_since_only_counts_this_projects_runs_after_the_cutoff(session, ctx):
    """月度预算的取数必须**按项目、按时间**过滤，且不把 Decimal 漏出去。

    取错了方向会很糟：把别的项目的花费算进来 → 无辜项目被拒；
    把本月之外的花费算进来 → 月度闸门永远不触发。
    """
    from datetime import datetime, timezone

    from app.models.agent_run import AgentRun
    from app.repositories.agent_run import AgentRunRepository

    project, source, _runs = ctx
    other_project = ProjectRepository(session).add(
        Project(user_id=project.user_id, name="p08-other", budget_total=10, budget_used=0)
    )
    session.flush()

    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = [
        # 本项目、cutoff 之后：应计入
        (project.id, datetime(2026, 9, 10, tzinfo=timezone.utc), 0.25),
        # 本项目、cutoff 之前：不应计入
        (project.id, datetime(2026, 8, 31, tzinfo=timezone.utc), 9.99),
        # 别的项目、cutoff 之后：不应计入
        (other_project.id, datetime(2026, 9, 11, tzinfo=timezone.utc), 5.0),
        # 本项目、尚未开始：花费恒为 0，也不该被算进"本月已花"
        (project.id, None, 3.0),
    ]
    for pid, started, cost in rows:
        session.add(
            AgentRun(
                project_id=pid,
                source_id=source.id,
                status=enums.AGENT_RUN_COMPLETED,
                started_at=started,
                cost_actual=cost,
            )
        )
    session.flush()

    total = AgentRunRepository(session).cost_sum_since(project.id, cutoff)
    assert isinstance(total, float), "Numeric 的 Decimal 不该漏进业务层"
    assert total == pytest.approx(0.25), f"只应计入本项目 cutoff 之后的那一笔，实际 {total}"


# ============================================================
# 回归：Post-check 必须把花费累加回 Project.budget_used
# ============================================================


def test_worker_accumulates_project_budget_used(session, ctx):
    """Post-check（计划第 650–652 行）：Run 的真实花费必须累加回项目。

    此前 `Project.budget_used` **永远是 0** —— 没有任何代码写它。
    后果是项目预算永远花不完，"预算不足就拒绝创建"那道闸门即使接上
    也永远不触发。这也解释了为什么这条验收以前只在单测里成立。
    """
    from unittest.mock import patch

    from sqlalchemy import text as _text

    from app.db import SessionLocal
    from app.tasks.analysis import _execute

    project, source, runs = ctx

    # 必须有一条 error 级事件：0 事件会判 L0（纯规则、不调模型），
    # 那样就没有花费可累加，测的就不是 Post-check 了。
    from app.models.event import Event
    from app.repositories.event import EventRepository

    EventRepository(session).add(
        project.id,
        Event(
            project_id=project.id,
            source_id=source.id,
            timestamp=T0,
            severity="high",
            event_type="log",
            message="kernel: Out of memory: Kill process 1234",
        ),
    )
    run_id, _, _ = create_run(runs, _request(project, source))
    session.commit()

    class _FakeSettings:
        model_provider_base_url = "https://example.invalid"
        secret_key = "test-secret"
        default_timezone = "Asia/Shanghai"
        data_dir = "./data"
        model_l1 = model_l2 = model_l3 = "test-model"
        model_l1_reasoning = model_l2_reasoning = model_l3_reasoning = "off"
        model_l1_price_input_per_1m = model_l1_price_output_per_1m = 1.0
        model_l2_price_input_per_1m = model_l2_price_output_per_1m = 1.0
        model_l3_price_input_per_1m = model_l3_price_output_per_1m = 1.0
        model_cache_hit_input_price_per_1m = 0.0

    class _StubResult:
        tokens_input = 1000
        tokens_output = 500
        cost = 0.25

        def __init__(self) -> None:
            self.parsed = {"insights": [], "knowledge_candidates": []}

    class _StubRouter:
        def generate(self, **kwargs):
            return _StubResult()

    with patch("app.config.get_settings", return_value=_FakeSettings()), patch(
        "app.gateways.router.build_router", return_value=_StubRouter()
    ):
        _execute(
            session_factory=SessionLocal,
            run_id=run_id,
            project_id=project.id,
            start_tier="L2",
        )

    fresh = SessionLocal()
    try:
        used = fresh.execute(
            _text("SELECT budget_used FROM projects WHERE id = :i"), {"i": project.id}
        ).scalar_one()
        cost = fresh.execute(
            _text("SELECT cost_actual FROM agent_runs WHERE id = :i"), {"i": run_id}
        ).scalar_one()
    finally:
        fresh.close()

    assert float(used) == float(cost) > 0, (
        f"项目已用预算应等于这次 Run 的实际花费：used={used} cost={cost}"
    )


# ============================================================
# 回归：Mid-check 到顶 → partial_success（而不是 failed / completed）
# ============================================================


def test_mid_check_stops_at_policy_limit_and_keeps_partial_results(session, ctx):
    """策略到顶必须收成 `partial_success` + 真实原因，且**已花的钱照记**。

    计划第 646–648 行：Mid-check 在每次模型调用前检查，到顶就"停止昂贵步骤，
    状态置 partial_success，返回已完成部分"。此前 `CostController` 在真实路径里
    根本没被构造，`Router.generate` 前面没有任何检查 —— 这些上限只存在于配置里。

    这条同时钉住两个容易做错的点：
      - 到顶**不是** failed（否则已经有结论的 Run 看起来一无所有）；
      - 抛异常之前要先把已拿到的结论落库，否则"返回已完成部分"变成"全丢"。
    """
    from unittest.mock import patch

    from sqlalchemy import text as _text

    from app.policy.store import get_policy_store, reset_policy_store
    from app.tasks.analysis import _execute

    project, source, runs = ctx

    # 一条 error 级事件 → 复杂度非 L0 → 会真的走模型这一侧
    from app.models.event import Event
    from app.repositories.event import EventRepository

    EventRepository(session).add(
        project.id,
        Event(
            project_id=project.id,
            source_id=source.id,
            timestamp=T0,
            severity="high",
            event_type="log",
            message="kernel: Out of memory: Kill process 1234",
        ),
    )
    run_id, _, _ = create_run(runs, _request(project, source, start_tier="L3"))
    session.commit()

    class _FakeSettings:
        model_provider_base_url = "https://example.invalid"
        secret_key = "test-secret"
        default_timezone = "Asia/Shanghai"
        data_dir = "./data"
        model_l1 = model_l2 = model_l3 = "test-model"
        model_l1_reasoning = model_l2_reasoning = model_l3_reasoning = "off"
        model_l1_price_input_per_1m = model_l1_price_output_per_1m = 1.0
        model_l2_price_input_per_1m = model_l2_price_output_per_1m = 1.0
        model_l3_price_input_per_1m = model_l3_price_output_per_1m = 1.0
        model_cache_hit_input_price_per_1m = 0.0

    calls: list[str] = []

    class _StubResult:
        tokens_input = 1000
        tokens_output = 500
        cost = 0.25  # 远超下面设定的 0.0001 上限

        def __init__(self) -> None:
            # 故意给一个**不存在**的 event_id：证据校验会要求重试，
            # 于是链路会发起第二轮调用 —— 而那一轮会先撞上 Mid-check。
            self.parsed = {
                "insights": [
                    {
                        "type": "inference",
                        "severity": "medium",
                        "confidence": 0.5,
                        "title": "疑似 OOM",
                        "summary": "OOM 迹象",
                        "evidence_ids": ["999999"],
                        "reasoning": "关键词命中",
                    }
                ],
                "knowledge_candidates": [],
            }

    class _StubRouter:
        def generate(self, **kwargs):
            calls.append(kwargs.get("tier"))
            return _StubResult()

    # 单次 Run 上限压到极低：第一次调用之后就必然到顶
    reset_policy_store()
    get_policy_store().set_override(project.id, run_max_cost=0.0001)
    try:
        with (
            patch("app.config.get_settings", return_value=_FakeSettings()),
            patch("app.gateways.router.build_router", return_value=_StubRouter()),
        ):
            out = _execute(
                session_factory=SessionLocal,
                run_id=run_id,
                project_id=project.id,
                start_tier="L3",
            )
    finally:
        reset_policy_store()  # 覆盖是进程内的，别漏给别的测试

    assert out["status"] == enums.AGENT_RUN_PARTIAL_SUCCESS, (
        f"策略到顶应收成 partial_success（不是 failed/completed），实际 {out['status']}"
    )
    assert len(calls) == 1, f"到顶后不该再调模型，实际调了 {calls}"

    fresh = SessionLocal()
    try:
        status, metadata, cost = fresh.execute(
            _text("SELECT status, run_metadata, cost_actual FROM agent_runs WHERE id = :i"),
            {"i": run_id},
        ).one()
    finally:
        fresh.close()

    assert status == enums.AGENT_RUN_PARTIAL_SUCCESS
    assert metadata.get("stop_reason") == "budget_exceeded", (
        f"中断原因必须是真正的原因（budget_exceeded），实际 {metadata.get('stop_reason')}"
    )
    # 钱是真花了：到顶也照记
    assert float(cost) > 0


# ============================================================
# 回归：失败 / 不完整必须能「重新分析」（计划第 912 行、验收第 919 行）
# ============================================================


def test_retry_creates_a_new_run_with_lineage(session, ctx):
    """「重新分析」要新建 Run 并用 parent_run_id 串起谱系。

    计划第 692 行写的是「failed 不可原地复活，只能新建 Run」，
    第 353 行说「重试产生新 Run，用 parent_run_id 串起谱系」——
    字段和 `create_queued(parent_run_id=...)` 参数早就备好了，
    但**从来没有人传过**：谱系一直是空的，重试入口也只有一句
    "回项目页自己再点一次"（还得把时间窗重填一遍，填不一样就不是同一次分析了）。
    """
    from unittest.mock import patch

    from app.api.routes_runs import retry_analysis_run
    from app.api.scoping import RunScope

    project, source, runs = ctx
    original_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    # 让它进入一个"允许重试"的终态
    runs.apply_status(project.id, original_id, target=enums.AGENT_RUN_RUNNING)
    runs.apply_status(
        project.id, original_id, target=enums.AGENT_RUN_FAILED, error="模型不可达"
    )
    session.flush()

    original = runs.get(project.id, original_id)
    original.input = {
        "data_source_id": int(source.id),
        "time_range": {"start": T0.isoformat(), "end": (T0 + timedelta(hours=1)).isoformat()},
        "start_tier": "L2",
    }
    session.flush()

    with patch("app.api.routes_runs._dispatch"):  # 不真的入队（测试里没有 broker）
        response = retry_analysis_run(
            scope=RunScope(run=original, project_id=project.id), session=session
        )

    assert response.reused is False, "重试必须是新 Run，不能把原 Run 复用回去"
    assert response.run_id != original_id
    assert response.status == enums.AGENT_RUN_QUEUED

    session.expire_all()
    fresh_run = runs.get(project.id, response.run_id)
    assert int(fresh_run.parent_run_id) == original_id, (
        f"新 Run 的 parent_run_id 应指向原 Run {original_id}，实际 {fresh_run.parent_run_id}"
    )
    # 输入必须原样带过去：换了时间窗就不是同一次分析了
    assert fresh_run.input["time_range"]["start"] == T0.isoformat()
    assert int(fresh_run.source_id) == int(source.id)


def test_retry_refuses_states_that_would_waste_money(session, ctx):
    """completed / running / queued 一律拒绝重试，并说明为什么。

    `completed` 再跑一遍就是重复计费；`running`/`queued` 还在跑，
    重试等于同一件事花钱做两次。拒绝时必须**说清原因**（红线 4）。
    """
    from fastapi import HTTPException

    from app.api.routes_runs import retry_analysis_run
    from app.api.scoping import RunScope

    project, source, runs = ctx

    for status_target, label in (
        (None, "queued"),
        (enums.AGENT_RUN_RUNNING, "running"),
    ):
        run_id, _, _ = create_run(
            runs, _request(project, source, filters={"case": label})
        )
        session.flush()
        if status_target:
            runs.apply_status(project.id, run_id, target=status_target)
        session.flush()
        run = runs.get(project.id, run_id)
        run.input = {
            "time_range": {"start": T0.isoformat(), "end": (T0 + timedelta(hours=1)).isoformat()}
        }
        session.flush()

        with pytest.raises(HTTPException) as excinfo:
            retry_analysis_run(
                scope=RunScope(run=run, project_id=project.id), session=session
            )
        assert excinfo.value.status_code == 422
        assert label in str(excinfo.value.detail) or "不允许" in str(excinfo.value.detail)


def test_retry_refuses_when_original_input_was_not_recorded(session, ctx):
    """没有原始输入时**明确拒绝**，而不是"用当前时间凑一个"。

    凑出来的时间窗和原来那次不是一回事，跑完还会以"重试成功"的面目出现 ——
    这正是红线 4 要禁止的含糊。
    """
    from fastapi import HTTPException

    from app.api.routes_runs import retry_analysis_run
    from app.api.scoping import RunScope

    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_RUNNING)
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_FAILED, error="boom")
    session.flush()

    run = runs.get(project.id, run_id)
    run.input = None
    session.flush()

    with pytest.raises(HTTPException) as excinfo:
        retry_analysis_run(scope=RunScope(run=run, project_id=project.id), session=session)
    assert excinfo.value.status_code == 422
    assert "原始输入" in str(excinfo.value.detail)


# ============================================================
# 回归（2026-09-24 真机核验）：started_at / 取消 / 僵尸回收
#
# 这三条的共同形态与阶段 07 一模一样：代码齐全、单测全绿，
# **真机上一次都没生效**。所以每条测试都从真实入口走一遍，
# 而不是只测那个函数本身。
# ============================================================


def test_running_transition_stamps_started_at_once(session, ctx):
    """置 `running` 时必须写 `started_at`，且后续流转不得改写。

    `started_at` 是月度预算的**唯一**取数依据（`cost_sum_since` 的 SQL 带
    `started_at is not null`）。真机核验发现：全库 177 条 Run、其中 91 条真的
    花过钱，`started_at` 非空的是 0 条 —— 真实花费被 SQL 全部排除，
    "本月已花"恒为 0，月度闸门对真实用量永远不触发。
    """
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.flush()
    assert runs.get(project.id, run_id).started_at is None, "创建时还不该有执行起点"

    assert runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_RUNNING) is True
    session.flush()
    stamped = runs.get(project.id, run_id).started_at
    assert stamped is not None, "置 running 必须写 started_at，否则月度预算永远读不到花费"

    assert runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_COMPLETED) is True
    session.flush()
    assert runs.get(project.id, run_id).started_at == stamped, "后续流转不得改写执行起点"


def test_monthly_spend_counts_a_run_that_really_started(session, ctx):
    """跑过的 Run 必须被 `cost_sum_since` 计入 —— 这是月度闸门能触发的前提。"""
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_RUNNING)
    runs.get(project.id, run_id).cost_actual = 0.5
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_COMPLETED)
    session.flush()

    month_start = datetime.now(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    assert runs.cost_sum_since(project.id, month_start) == pytest.approx(0.5)


def test_cancel_flag_is_read_from_the_database_not_from_the_cached_object(session, ctx):
    """取消标记必须**每次查库**，不能依赖会话里缓存的那个对象。

    Worker 的会话早把这个 Run 载入了身份映射（`_execute` 开头读过），
    而 SQLAlchemy 不会拿库里的新值去覆盖一个**有待写改动**的对象 ——
    于是 API 进程置的位在 Worker 侧永远看不见。
    """
    from sqlalchemy import text as _text

    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    # 必须先提交：未提交的 INSERT 对别的连接不可见，那条 UPDATE 会**静默影响 0 行**
    # （第一版测试就栽在这里，误以为"读不到"是代码问题）。
    session.commit()
    runs.get(project.id, run_id)  # 载入身份映射
    runs.touch_heartbeat(project.id, run_id)  # 弄脏（尚未 flush，故不持行锁）

    with engine.begin() as conn:
        updated = conn.execute(
            _text("update agent_runs set cancel_requested = true where id = :r"),
            {"r": run_id},
        ).rowcount
    assert updated == 1, "外部连接的取消写入没有落到行上，测试前提不成立"

    assert runs.is_cancel_requested(project.id, run_id) is True


def test_worker_releases_the_row_lock_so_cancel_can_land(session, ctx):
    """取消要能生效，Worker 就不能把 `agent_runs` 那一行锁到任务结束。

    真机核验（2026-09-24）：分析进行到一半调取消，接口 200、`cancel_requested`
    也确实进了库 —— 但那是 **Worker 提交之后**才进去的，取消从未生效；
    根因经复现确认是行锁（`LockNotAvailable`）。

    这条测试把两半都钉住：
      ① 分析进行中，另一个连接能立刻写入取消标记（说明行锁已随 checkpoint 释放）；
      ② 下一个检查点读到它，Run 被收成 `cancelled`。
    """
    from sqlalchemy import text as _text

    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    session.commit()  # 让另一个连接看得见这个 Run

    executor = RunExecutor(run_repository=runs, checkpoint=session.commit)
    cancel_written: list[bool] = []

    def attempt_tier(tier: str) -> str:
        with engine.begin() as conn:
            conn.execute(_text("set local lock_timeout = '2s'"))
            conn.execute(
                _text("update agent_runs set cancel_requested = true where id = :r"),
                {"r": run_id},
            )
        cancel_written.append(True)
        return "本轮分析已结束"

    outcome = executor.execute(
        project_id=project.id,
        run_id=run_id,
        attempt_tier=attempt_tier,
        start_tier="L2",
    )

    assert cancel_written == [True], "取消写入被 Worker 的行锁挡住了"
    assert outcome.status == enums.AGENT_RUN_CANCELLED
    # 收尾提交：executor 的 checkpoint 已经把 running 提交出去了，
    # 不提交终态的话这条 Run 会以 `running` 留在库里（跨测试污染：
    # 全局扫描的 reclaim_zombies 会把它当僵尸捞出来）。测试自己造成的状态，自己收干净。
    session.commit()


def test_reap_zombie_runs_task_reclaims_a_stale_running_run(session, ctx):
    """僵尸回收必须**经 beat 那条任务入口**生效，而不只是 repository 里可用。

    以前只在 repository 层测过 `reclaim_zombies`，而真机上没有任何东西调它：
    `celery_app` 里连 `beat_schedule` 都没有，beat 容器一直空转。
    真机实测：分析中强杀 Worker，Run 停在原状态，超过心跳阈值十分钟无人回收。
    """
    from app.tasks.maintenance import reap_zombie_runs

    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_RUNNING)
    runs.touch_heartbeat(
        project.id,
        run_id,
        at=datetime.now(UTC) - timedelta(seconds=DEFAULT_HEARTBEAT_TIMEOUT_SECONDS + 60),
    )
    session.commit()  # 任务在另一个会话里读

    result = reap_zombie_runs.apply().get()
    assert run_id in result["run_ids"], "心跳超时的 running Run 必须被任务收走"

    fresh = SessionLocal()
    try:
        row = AgentRunRepository(fresh).get(project.id, run_id)
        assert row.status == enums.AGENT_RUN_TIMEOUT
        assert (row.run_metadata or {}).get("stop_reason") == "timeout"
    finally:
        fresh.close()


def test_reaped_run_cannot_be_resurrected_by_a_late_worker(session, ctx):
    """被回收成 `timeout` 的 Run 不能被"迟到的 Worker"复活成 `completed`。

    真机复验（2026-09-24）撞到的真实后果：分析中冻结 Worker 容器 → beat 上的
    维护 worker 把 Run 收成 `timeout`（终态）→ 解冻后那个 Worker 从中断处继续跑完，
    又把终态写成 `completed`。根因是状态判定读的是**会话里几分钟前载入的对象**，
    而跨进程的"终态不可再改"必须查库判断。
    """
    from app.analysis.state_machine import IllegalTransitionError

    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_RUNNING)
    session.commit()

    # Worker 侧：像 `_execute` 开头那样把对象载入会话（此后它一直是脏/旧的）
    worker_session = SessionLocal()
    worker_runs = AgentRunRepository(worker_session)
    assert worker_runs.get(project.id, run_id).status == enums.AGENT_RUN_RUNNING

    # 回收者：另一个会话把它收成 timeout
    reaper = SessionLocal()
    AgentRunRepository(reaper).apply_status(
        project.id, run_id, target=enums.AGENT_RUN_TIMEOUT, reason="heartbeat_lost"
    )
    reaper.commit()
    reaper.close()

    # 迟到的 Worker 想写 completed → 终态不可改，必须被拒
    with pytest.raises(IllegalTransitionError):
        worker_runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_COMPLETED)
    worker_session.close()

    fresh = SessionLocal()
    try:
        assert (
            AgentRunRepository(fresh).get(project.id, run_id).status
            == enums.AGENT_RUN_TIMEOUT
        ), "终态没有被改写"
    finally:
        fresh.close()


def test_metadata_merge_keeps_other_writers_keys(session, ctx):
    """Worker 收尾合并 `run_metadata` 时必须基于**库里的当前值**。

    真机复验（2026-09-24）撞到的：Run 被回收成 `timeout`（写入了 stop_reason 与
    note），解冻后迟到的 Worker 用会话里几分钟前的旧 metadata 合并，
    把回收者写的**原因抹掉了** —— 库里只剩"状态 timeout、没有为什么"。
    """
    project, source, runs = ctx
    run_id, _, _ = create_run(runs, _request(project, source))
    runs.apply_status(project.id, run_id, target=enums.AGENT_RUN_RUNNING)
    session.commit()

    worker_session = SessionLocal()
    worker_runs = AgentRunRepository(worker_session)
    worker_runs.get(project.id, run_id)  # Worker 载入（此后是旧快照）

    reaper = SessionLocal()
    AgentRunRepository(reaper).apply_status(
        project.id,
        run_id,
        target=enums.AGENT_RUN_TIMEOUT,
        reason="heartbeat_lost",
        metadata={"stop_reason": "timeout", "note": "Worker 心跳丢失，Run 已被回收"},
    )
    reaper.commit()
    reaper.close()

    merged = worker_runs.merge_run_metadata(
        project.id, run_id, {"insight_count": 3, "context_tokens": 1234}
    )
    worker_session.commit()
    worker_session.close()

    assert merged is not None
    assert merged["stop_reason"] == "timeout", "回收原因被旧快照抹掉了"
    assert merged["note"].startswith("Worker 心跳丢失")
    assert merged["insight_count"] == 3, "新信息也要并进去"
