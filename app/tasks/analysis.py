"""分析 Run 的 Celery 任务（阶段 08 的 Worker 落地）。

职责边界：本任务负责**从库里取出 Run、跑链路、写回状态**；
可靠性机制（状态机、重试、降级、心跳、取消）全部复用阶段 08 的 RunExecutor，
不在这里重新实现一遍。
"""

from __future__ import annotations

from typing import Any

from app.analysis.heartbeat import CancelledError
from app.analysis.pipeline import PipelineInput, analyze
from app.analysis.runner import RunExecutor
from app.celery_app import celery_app
from app.db import SessionLocal
from app.utils.observability import bind_run_context, get_logger

logger = get_logger(__name__)


@celery_app.task(name="app.tasks.analysis.execute_run", bind=True)
def execute_run_task(
    self: Any,
    *,
    run_id: int,
    project_id: int,
    start_tier: str = "L2",
    trace_id: str | None = None,
) -> dict[str, Any]:
    """执行一次分析 Run。

    Windows 上 worker 需加 `-P solo`（见 AGENTS.md 技术栈）。

    `trace_id` 由发起请求的那次 HTTP 调用传进来（见 `_dispatch`）：
    contextvars **不跨进程**，不显式传的话 worker 会另起一个 trace_id，
    "同一条 trace 串起请求与执行"就只剩前半截 —— 拿着 Run 详情里的
    trace_id 去 grep，worker 那半边一条都捞不到。传了就沿用同一个。
    """
    # 任务也在同一个 trace 下：这样"HTTP 请求 → 入队 → Worker 执行"
    # 的日志能被同一个 trace_id 串起来（计划第 928 行）
    with bind_run_context(trace_id=trace_id, run_id=run_id, project_id=project_id):
        return _execute(
            session_factory=SessionLocal,
            run_id=run_id,
            project_id=project_id,
            start_tier=start_tier,
        )


def _execute(
    *, session_factory, run_id: int, project_id: int, start_tier: str
) -> dict[str, Any]:
    session = session_factory()
    try:
        from app.domains.registry import get_domain_registry
        from app.repositories.agent_run import AgentRunRepository
        from app.repositories.event import EventRepository

        runs = AgentRunRepository(session)
        run = runs.get(project_id, run_id)
        if run is None:
            logger.warning(
                "Run %s 不存在（project %s），任务不做事", run_id, project_id
            )
            return {"run_id": run_id, "status": "missing"}
        current_status = runs.status_of(project_id, run_id)
        if current_status != "queued":
            return {"run_id": run_id, "status": current_status, "reused": True}

        domain = get_domain_registry().load(
            (run.input or {}).get("domain_id") or _settings().default_domain_id,
            (run.input or {}).get("domain_version"))

        # 载入本次事件（受 project 边界约束）。
        # 必须经 EventRepository.pipeline_events —— 那里是 ORM→链路字段映射的
        # 唯一真源。早先这里手写了一份"看起来等价"的映射，唯独漏了 metadata
        # （进程名在其中），于是 analyzer 全部判定"无异常"、复杂度落到 L0、
        # 模型一次都没被调用，Run 却显示 completed。
        snapshot = run.input or {}
        window = snapshot.get("time_range") or {}
        from datetime import datetime
        time_start = datetime.fromisoformat(window["start"])
        time_end = datetime.fromisoformat(window["end"])
        events = EventRepository(session).pipeline_events(
            project_id, source_id=int(run.source_id), time_start=time_start,
            time_end=time_end, filters=snapshot.get("filters") or {},
            max_event_id=snapshot.get("source_revision"),
        )
        # 事件条数是排障的第一现场：0 条事件会让整条链路"成功地产出零结论"，
        # 而这个数字是唯一能一眼看出来的证据
        logger.info("run_events_loaded", event_count=len(events))

        # 历史相似事故（计划第 787 行）与 confirmed 知识（阶段 03 的加载器）。
        #
        # 这两项此前**在 app/ 里没有任何赋值处** —— `PipelineInput` 里留着字段与默认空表，
        # 真实路径永远传空。后果：`incidents` 表是空的，"历史事故记忆库"没有内容可注入；
        # 人工确认过的知识也从不参与分析，"确认后下次能命中"因此不成立。
        from app.repositories.incident import IncidentRepository

        historical_incidents = load_historical_incidents(
            IncidentRepository(session), project_id
        )
        confirmed_knowledge = load_confirmed_knowledge(domain, project_id=project_id)
        logger.info(
            "run_context_sources",
            historical_incidents=len(historical_incidents),
            confirmed_knowledge=len(confirmed_knowledge),
        )

        executor = RunExecutor(run_repository=runs, checkpoint=session.commit)
        captured: dict[str, Any] = {}

        def record_phase(name: str) -> None:
            executor.check_cancel(project_id, run_id)
            if runs.status_of(project_id, run_id) != "running":
                raise CancelledError("Run 已终止，停止推进阶段")
            session.refresh(run)
            history = list(run.phase_history or [])
            history.append({"phase": name, "status": "running", "at": _utcnow().isoformat()})
            if len(history) > 1 and history[-2].get("status") == "running":
                history[-2] = {**history[-2], "status": "done"}
            runs.apply_progress(project_id, run_id, current_phase=name, phase_history=history)
            executor.heartbeat(project_id, run_id)


        # 一次 Run 一个成本控制器：三道检查点（Pre 已在创建端点做过、
        # Mid 在每次调用前、Post 在下面累加预算）共用同一份用量账。
        from app.models.project import Project
        from app.policy.wiring import build_cost_controller

        controller = build_cost_controller(project=session.get(Project, project_id))
        if controller is not None:
            # 不 start() 的话 elapsed_seconds 恒为 0，时长上限形同虚设
            controller.start()

        def attempt_tier(tier: str) -> Any:
            """每个等级尝试一次完整链路。"""
            try:
                return _attempt_tier_inner(tier)
            except Exception as exc:
                import traceback

                logger.error(
                    "attempt_tier_failed",
                    tier=tier,
                    error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc()[-2000:],
                )
                raise

        def _attempt_tier_inner(tier: str) -> Any:
            executor.heartbeat(project_id, run_id)
            executor.check_cancel(project_id, run_id)

            from app.gateways.router import build_router

            class LazyRouter:
                """L0 不需要模型配置；仅当链路实际调用模型时装配网关。"""
                def __init__(self):
                    self.router = None

                def generate(self, **kwargs):
                    if self.router is None:
                        self.router = build_router(_settings(), run_repository=runs)
                        if controller is not None:
                            from app.policy.wiring import PolicyGuardedRouter
                            self.router = PolicyGuardedRouter(self.router, controller)
                    return self.router.generate(**kwargs)

            router = LazyRouter()

            result = analyze(
                PipelineInput(
                    project_id=project_id,
                    source_id=int(run.source_id or 0),
                    domain_id=domain.domain_id,
                    domain_version=domain.version,
                    time_start=time_start,
                    time_end=time_end,
                    events=events,
                    historical_incidents=historical_incidents,
                    confirmed_knowledge=confirmed_knowledge,
                ),
                analyzers=domain.analyzers(),
                domain=domain,
                router=router,
                record=True,
                project_id_for_recording=project_id,
                run_id=run_id,
                on_phase=record_phase,
                model_tier_cap=tier,
            )
            captured["result"] = result
            # 工具执行记录落进 Run（阶段 05 验收）：不记的话，
            # "工具跑了没有、跑了多久、处理多少条"在事后完全无从查起。
            _record_tool_usage(runs, project_id, run_id, result)
            # 关键节点留痕：**这才是 trace_id 能派上用场的地方**。
            # 计划第 928 行要求"同一条 trace 串起请求与执行"，可早先整条成功路径
            # 一条日志都不打 —— 于是拿 trace_id 去 grep 什么都捞不到，
            # "有 trace" 只是形式上的。等级判定、候选异常、模型调用与花费
            # 是排障时最先要看的三件事，全部记上。
            logger.info(
                "pipeline_done",
                tier=result.tier,
                anomaly_count=len(result.anomalies),
                anomaly_types=sorted({str(a.get("type")) for a in result.anomalies}),
                context_tokens=result.context_tokens,
                model_attempts=len(result.model_attempts),
            )
            # 落库必须在**这里**、也就是"状态机写终态之前"完成。
            #
            # 放在 execute() 之后是不行的：那时 Run 已经是 completed/failed 这样的
            # 终态，而状态机明令终态不可再改。于是落库一旦失败，就只能眼睁睁看着
            # 一个"已完成但 0 条结论"的 Run 留在库里 —— 恰好是红线 4 要禁止的含糊。
            # 放进 attempt_tier 里，落库失败就变成一次**等级尝试失败**，
            # 由降级链与状态机如实收尾成 failed，并把真实原因写进 error。
            record_phase("persistence")
            captured["persisted"] = _persist_result(
                session, project_id=project_id, run_id=run_id, run=run, result=result
            )
            logger.info("insights_persisted", **captured["persisted"])
            # 候选知识写 staging（计划第 751 行）：不写的话「待确认知识」区永远是空的，
            # 8.1 知识闭环只剩"人工确认"这一半 —— 没有候选可确认。
            _stage_knowledge_candidates(domain, result, run_id=run_id, project_id=project_id)

            # 模型调用失败换来的纯规则报告：这一级算**失败**，交给降级链继续
            # 尝试其它等级，全都不行时再由 `rules_only()` 产出最终报告 ——
            # 那样 Run 才会被如实收成 `partial_success` + `model_unavailable`
            # （计划第 724 行）。不抛的话它会以 `completed` 收场，
            # 用户看到的是一份"没有异常"的干净报告（真机核验 2026-09-24）。
            if getattr(result, "model_unavailable", False):
                from app.gateways.base import ModelUnavailableError, ModelRequestError

                error_type = ModelUnavailableError if result.model_failure_retryable else ModelRequestError
                failure = error_type(
                    "模型调用失败，本级只产出纯规则报告；交由降级链继续尝试其它等级"
                )
                failure.tier_used = result.tier
                raise failure

            if result.stop_reason:
                # 策略到顶：把**已经拿到的结论落库之后**再上抛，
                # 让 executor 按计划第 647–648 行把状态收成 partial_success
                # 并带上真正的原因（预算 / token / 时长 / 调用次数）。
                #
                # 顺序很重要：先落库再抛。反过来的话这份"已完成部分"会随异常
                # 一起被丢掉，等于把 partial_success 做成了 failed。
                from app.policy.cost_controller import StopExecution

                raise StopExecution(
                    result.stop_reason,
                    "；".join(result.notes[-2:]) or "已达策略上限",
                )
            return result

        def rules_only() -> Any:
            """模型全不可用时的纯规则兜底（计划第 724 行）。"""
            result = analyze(
                PipelineInput(
                    project_id=project_id,
                    source_id=int(run.source_id or 0),
                    domain_id=domain.domain_id,
                    domain_version=domain.version,
                    time_start=time_start,
                    time_end=time_end,
                    events=events,
                    historical_incidents=historical_incidents,
                    confirmed_knowledge=confirmed_knowledge,
                ),
                analyzers=domain.analyzers(),
                domain=domain,
                router=None,  # 不调模型
            )
            captured["result"] = result
            captured["persisted"] = _persist_result(session, project_id=project_id,
                run_id=run_id, run=run, result=result)
            return result

        try:
            outcome = executor.execute(
                project_id=project_id,
                run_id=run_id,
                attempt_tier=attempt_tier,
                start_tier=start_tier,
                rules_only_fallback=rules_only,
            )
        except CancelledError:
            logger.info("run_cancelled", run_id=run_id)
            return {"run_id": run_id, "status": "cancelled"}

        # 落库已在 attempt_tier 内完成（见那里的说明）。这里只取结果：
        # 失败时 captured 里没有它，Run 已被降级链收尾成 failed。
        result = captured.get("result")
        if result is None and not outcome.attempts:
            return {"run_id": run_id, "status": outcome.status, "reused": True}
        persisted = captured.get("persisted") or {
            "insight_count": 0,
            "evidence_count": 0,
        }

        # 回填成本与 token（计划第 650–652 行的 Post-check）。
        #
        # 走 `controller.post_check()` 而不是在这里把 `model_attempts` 再累加一遍：
        # 用量本来就在控制器里逐次记账（`PolicyGuardedRouter` 每次调用后
        # `record_call`），这里再算一次等于**同一件事两份实现** ——
        # 一份算错时另一份不会报错，只会给出两个不一样的数字。
        # 控制器可能是 None（型号没配齐，走纯规则），那时没有花费可回填。
        if result is not None and controller is not None:
            usage = controller.post_check()
            run.tokens_input = int(usage.get("tokens_input") or 0)
            run.tokens_output = int(usage.get("tokens_output") or 0)
            run.cost_actual = float(usage.get("cost") or 0.0)

        # 把产出条数与降级事实回填进 Run 元数据 —— 否则 Run 详情只会显示
        # "0 条结论、0 次模型调用"，而**真实发生的事**（结构化输出失败、
        # 降级到纯规则）全都看不见，读者会以为分析正常但没发现异常。
        metadata_extra: dict[str, Any] = {
            "insight_count": int(persisted.get("insight_count", 0)),
            "evidence_count": int(persisted.get("evidence_count", 0)),
        }
        if result is not None:
            # 证据被拦下的条数与链路的自述：不记的话，"为什么这次结论这么少"
            # 只能靠猜（真机上这是排障第一个要问的问题）。
            metadata_extra["evidence_rejections"] = int(result.evidence_rejections or 0)
            metadata_extra["notes"] = list(result.notes or [])
        # 降级链的尝试明细：哪个等级失败、失败原因是什么。
        # 不记的话，Run 详情只能显示"完成"，看不出中间降过级、为什么降。
        metadata_extra["attempts"] = list(outcome.attempts or [])
        if result is not None:
            metadata_extra["used_rules_only"] = bool(result.used_rules_only)
            metadata_extra["context_tokens"] = int(result.context_tokens)
            metadata_extra["model_attempts"] = result.model_attempts
            import json
            metadata_extra["rules_only_report"] = json.loads(json.dumps(
                result.rules_only_report, default=lambda value: value.isoformat()))
            metadata_extra["tier"] = result.tier
            metadata_extra["complexity"] = result.complexity.as_dict()
            metadata_extra["event_count"] = len(events)
            metadata_extra["analysis_scope"] = {"source_id": run.source_id, **window,
                                                 "filters": snapshot.get("filters") or {}}
        # 合并必须基于**库里的当前值**：这段时间里 Run 可能已被回收/取消，
        # 那些原因（stop_reason / note）不能被这份旧快照抹掉。
        runs.merge_run_metadata(project_id, run_id, metadata_extra)

        # 终态与花费：这次分析到底干了什么的一句话总结。放在成本回填之后 ——
        # 放前面的话 tokens/cost 还是 0，日志会替库里"作证"说这次没花钱。
        # 与 run_events_loaded / pipeline_done 同属一条 trace，一条 grep 全出来。
        logger.info(
            "run_finished",
            status=outcome.status,
            used_rules_only=outcome.used_rules_only,
            stop_reason=outcome.stop_reason,
            insight_count=int(persisted.get("insight_count", 0)),
            context_tokens=int(result.context_tokens) if result is not None else 0,
            tokens_input=int(run.tokens_input or 0),
            tokens_output=int(run.tokens_output or 0),
            cost=float(run.cost_actual or 0),
        )

        # ---- Post-check（计划第 650–652 行）：把实际花费累加回 Project.budget_used ----
        #
        # 这一步以前完全没有：`budget_used` 永远是 0，于是项目预算**永远花不完**，
        # "预算不足就拒绝创建"那道闸门即使接上也永远不会触发。
        # 必须放在同一个事务里提交：Run 记了多少钱、项目就扣多少钱，
        # 两者不能一个成功一个失败。
        spent = float(run.cost_actual or 0)
        if spent > 0:
            from app.repositories.project import ProjectRepository

            budget_used = ProjectRepository(session).add_budget_used(project_id, spent)
            logger.info("project_budget_accumulated", spent=spent, budget_used=budget_used)

        # 提交！这一步绝不能只 flush。
        #
        # 这个任务**自己管会话**（不像 HTTP 路由那样由 get_db 依赖负责提交），
        # 只 flush 的话 `session.close()` 会把整段执行结果回滚掉：状态机算出的
        # completed/partial_success/failed、回填的 token 与成本、落库的
        # Insight 与 Evidence，**全部消失**，Run 永远停在 queued。
        # 更阴险的是返回值仍然是对的 —— "任务报告成功"与"库里没变"能同时成立。
        session.commit()
        return {
            "run_id": run_id,
            "status": outcome.status,
            "used_rules_only": outcome.used_rules_only,
            "insights": persisted,
        }
    except Exception as exc:
        # 这一层兜底是"真起全栈跑一遍"逼出来的：
        # 落库时的约束冲突（CHECK 不认模型给的 type）从任务里逃逸出去，
        # `session.close()` 把整个事务回滚 —— Run 永远停在 queued，
        # 页面上一切正常，只有 Worker 日志里才有真相。
        # 任何逃逸异常都必须先在 Run 上留下痕迹，再抛出（红线 4）。
        _record_crash(session, project_id=project_id, run_id=run_id, exc=exc)
        raise
    finally:
        session.close()


def _persist_result(
    session: Any, *, project_id: int, run_id: int, run: Any, result: Any
) -> dict[str, int]:
    """把分组、事故、结论与证据落库；失败必须变成一次**明确的**失败，不许静默。

    - 证据闸门拒绝（结论本身不合格）→ `ValidationFailureError`（不可重试）
    - 数据库写不进去（约束冲突、连接中断…）→ `StorageFailureError`（不可重试）

    两者都不可重试的理由一样：模型已经调完、钱已经花了，重试只会产出
    同一份写不进去的结论，再花一次钱。降级链会因此立即收尾成 `failed`
    并把真实原因写进 Run.error，而不是继续往下白白烧钱。

    顺序：**先分组/事故，再结论** —— 结论的 `incident_id` 要靠事故的成员事件派生。
    """
    if result is None:
        return {"insight_count": 0, "evidence_count": 0}

    from sqlalchemy.exc import SQLAlchemyError

    from app.analysis.idempotency import StorageFailureError, ValidationFailureError
    from app.analysis.persistence import (
        EvidencePersistenceError,
        persist_grouping,
        persist_insights,
        assert_no_evidence_violation,
    )
    from app.repositories.event import EventRepository
    from app.repositories.event_group import EventGroupRepository
    from app.repositories.evidence import EvidenceRepository
    from app.repositories.incident import IncidentRepository
    from app.repositories.insight import InsightRepository

    written = None
    grouping = None
    try:
        # SAVEPOINT：落库失败只回滚这一小段，外层事务仍然可用 ——
        # 否则 PG 会把整个事务置为 aborted，连"把失败写进 Run"都做不到，
        # 于是又变成"任务崩了、Run 停在 queued"。
        with session.begin_nested():
            grouping = persist_grouping(
                project_id=project_id,
                run_id=run_id,
                groups=list(result.groups or []),
                incidents=list(result.incidents or []),
                group_repository=EventGroupRepository(session),
                incident_repository=IncidentRepository(session),
                event_repository=EventRepository(session),
            )
            written = persist_insights(
                project_id=project_id,
                run_id=run_id,
                insights=result.insights,
                valid_event_ids=set(result.valid_event_ids),
                insight_repository=InsightRepository(session),
                evidence_repository=EvidenceRepository(session),
                source_id=int(run.source_id) if run.source_id else None,
                incident_by_event=grouping.incident_by_event,
                timestamps_by_event=_event_timestamps(session, project_id, result.valid_event_ids),
            )
            assert_no_evidence_violation(written)
    except EvidencePersistenceError as exc:
        logger.error(
            "insights_rejected_by_evidence_gate", run_id=run_id, error=str(exc)
        )
        raise ValidationFailureError(f"证据校验失败：{exc}") from exc
    except SQLAlchemyError as exc:
        logger.error(
            "insights_persist_failed",
            run_id=run_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise StorageFailureError(f"结论落库失败：{type(exc).__name__}: {exc}") from exc

    logger.info(
        "grouping_persisted",
        run_id=run_id,
        **(grouping.as_dict() if grouping is not None else {}),
    )
    return {**written.as_dict(), **(grouping.as_dict() if grouping is not None else {})}


def _event_timestamps(session, project_id: int, event_ids: list[str]) -> dict:
    from sqlalchemy import select
    from app.models.event import Event
    return {str(event_id): timestamp for event_id, timestamp in session.execute(
        select(Event.id, Event.timestamp).where(Event.project_id == project_id,
                                              Event.id.in_([int(i) for i in event_ids])))}


def load_historical_incidents(incident_repository: Any, project_id: int, *, limit: int = 5) -> list[dict]:
    """取本项目最近的历史事故，供 Context 注入（计划第 787 行）。"""
    try:
        return incident_repository.recent_for_context(project_id, limit=limit)
    except Exception as exc:  # noqa: BLE001 - 历史事故是背景信息，取不到不该让分析失败
        logger.warning(
            "historical_incidents_unavailable", error=f"{type(exc).__name__}: {exc}"
        )
        return []


def load_confirmed_knowledge(domain: Any, *, project_id: int | None = None) -> list[Any]:
    """加载领域已确认知识（阶段 03 的加载器；候选(staging)不参与）。"""
    try:
        from app.utils.atomic_files import project_data_dir
        base = project_data_dir(_settings().data_dir, project_id) if project_id else _settings().data_dir
        knowledge = domain.knowledge(data_dir=base)
    except Exception as exc:  # noqa: BLE001 - 知识库读不到不该让分析失败
        logger.warning("knowledge_unavailable", error=f"{type(exc).__name__}: {exc}")
        return []
    return list((knowledge or {}).get("confirmed") or [])


def _record_tool_usage(runs: Any, project_id: int, run_id: int, result: Any) -> None:
    """把工具执行记录写进 `Run.tool_usage`（阶段 05 验收）。"""
    for entry in getattr(result, "tool_runs", None) or []:
        runs.append_tool_usage(project_id, run_id, entry)


def _stage_knowledge_candidates(domain: Any, result: Any, *, run_id: int, project_id: int | None = None) -> None:
    """把候选知识写入 staging（计划第 751 行）。

    失败只记日志、不让分析失败：候选是**副产品**，主结果是结论与证据；
    为了一个副产品把整次分析判失败，是把优先级搞反了。
    """
    candidates = list(getattr(result, "knowledge_candidates", None) or [])
    if not candidates:
        return
    from app.analysis.knowledge_staging import write_candidates

    try:
        stats = write_candidates(
            data_dir=_settings().data_dir,
            domain_id=domain.domain_id,
            project_id=project_id,
            candidates=candidates,
            run_id=run_id,
            valid_event_ids=set(getattr(result, "valid_event_ids", None) or []),
        )
        logger.info("knowledge_candidates_staged", **stats)
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        logger.warning(
            "knowledge_staging_failed", error=f"{type(exc).__name__}: {exc}"
        )


def _record_crash(
    session: Any, *, project_id: int, run_id: int, exc: BaseException
) -> None:
    """把逃逸异常如实写进 Run（尽量），失败只记日志。

    running 异常收为 failed；执行准备阶段的 queued 异常沿用已有 queued→timeout，
    明确记录 preparation_failed，不留下永久排队的记录。
    """
    from app.analysis.idempotency import classify_error
    from app.models import enums
    from app.repositories.agent_run import AgentRunRepository

    message = f"{type(exc).__name__}: {exc}"
    try:
        # PG 在事务出错后会拒绝后续语句，先回滚再写
        session.rollback()
        runs = AgentRunRepository(session)
        run = runs.get(project_id, run_id)
        if run is None:
            return
        # 状态要查库取：这个会话里的对象可能是分析开始那一刻载入的，
        # 而在它"崩溃"之前，Run 可能已经被回收成 timeout / 被置为 cancelled。
        current = runs.status_of(project_id, run_id)
        if current is None:
            return
        if current in enums.AGENT_RUN_TERMINAL_STATES:
            logger.error(
                "execute_run_crashed_after_terminal",
                run_id=run_id,
                status=current,
                error=message,
            )
            return
        if current != enums.AGENT_RUN_RUNNING:
            if current == enums.AGENT_RUN_QUEUED:
                runs.apply_status(project_id, run_id, target=enums.AGENT_RUN_TIMEOUT,
                    reason="preparation_failed", error=f"执行准备失败：{message}",
                    metadata={"stop_reason": "preparation_failed"})
                session.commit()
            logger.error(
                "execute_run_crashed_before_running",
                run_id=run_id,
                status=current,
                error=message,
            )
            return
        runs.apply_status(
            project_id,
            run_id,
            target=enums.AGENT_RUN_FAILED,
            reason=classify_error(exc),
            error=f"Worker 异常终止：{message}",
        )
        session.commit()
        logger.error("execute_run_crashed", run_id=run_id, error=message)
    except Exception:
        session.rollback()
        logger.exception("record_crash_failed", run_id=run_id, error=message)


def _settings() -> Any:
    from app.config import get_settings

    return get_settings()


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
