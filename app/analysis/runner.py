"""Run 执行器：状态流转 + 可靠性机制（计划第 679–731 行）。

把阶段 08 的机制集中在一处，供 Celery Worker（阶段 08）与 Pipeline（阶段 09）复用：

    execute_run(...) 的职责
      1. 幂等：相同键且已有 completed → 直接复用，不重复执行/计费
      2. 状态：queued → running →（completed / partial_success / failed / timeout / cancelled）
      3. 心跳：执行前后更新 last_heartbeat
      4. 取消：阶段边界检查 cancel_requested
      5. 重试：可重试错误指数退避，最多 3 次
      6. 降级：重试耗尽后 L3→L2→L1，全败则纯规则 L0 报告 + partial_success
      7. 失败不静默：任一阶段失败且重试无果 → failed 并写明阶段与原因

**真正的分析逻辑由调用方注入**（阶段 09 的 pipeline）。本模块只保证
"怎么跑、跑失败了怎么办"，不掺业务。
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.analysis.heartbeat import (
    DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
    CancelledError,
    check_cancelled,
    validate_heartbeat_timeout,
)
from app.analysis.idempotency import (
    ErrorKind,
    NonRetryableError,
    classify_error,
    make_idempotency_key,
    PIPELINE_VERSION,
)
from app.analysis.retry import RetryPolicy, call_with_fallback
from app.analysis.state_machine import IllegalTransitionError, StateMachine
from app.models import enums
from app.policy import STOP_MODEL_UNAVAILABLE
from app.policy.policy import (
    STOP_BUDGET_EXCEEDED,
    STOP_CALL_LIMIT,
    STOP_RUNTIME_LIMIT,
    STOP_TOKEN_LIMIT,
)

#: 策略到顶导致的停止 —— 这些应当落到 `partial_success`（返回已完成部分），
#: 而不是 `failed`（计划第 647–648 行）。与 `model_unavailable` 的区别是：
#: 前者"到点收工、已有结论"，后者"压根没拿到结论"。
POLICY_STOP_REASONS: frozenset[str] = frozenset(
    {STOP_BUDGET_EXCEEDED, STOP_TOKEN_LIMIT, STOP_RUNTIME_LIMIT, STOP_CALL_LIMIT}
)


@dataclass
class RunRequest:
    """一次分析请求。幂等键由它算出。"""

    project_id: int
    source_id: int
    time_start: datetime
    time_end: datetime
    domain_id: str
    domain_version: str
    filters: dict[str, Any] = field(default_factory=dict)
    start_tier: str = "L2"
    run_input: dict[str, Any] | None = None

    def idempotency_key(self) -> str:
        return make_idempotency_key(
            project_id=self.project_id,
            source_id=self.source_id,
            time_start=self.time_start,
            time_end=self.time_end,
            filters=self.filters,
            domain_id=self.domain_id,
            domain_version=self.domain_version,
            pipeline_version=f"{PIPELINE_VERSION}:source:{self.run_input['source_revision']}"
                if self.run_input and "source_revision" in self.run_input else PIPELINE_VERSION,
        )


@dataclass
class RunOutcome:
    """执行结果，可直接用于 API 响应与 Run 落库。"""

    run_id: int
    status: str
    reused: bool = False
    #: 纯规则 L0 报告（模型全不可用时）
    used_rules_only: bool = False
    stop_reason: str | None = None
    error: str | None = None
    value: Any = None
    attempts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # 这里曾有 `insight_count` / `evidence_count` 两个字段，注释写着
    # "调用方落库后回填"。**从来没有任何代码给它们赋过值，也没有任何地方读它们** ——
    # 产出台数实际落在 `AgentRun.run_metadata`（`insight_count` / `evidence_count`），
    # Run 详情就是从那读的。留着这两个字段等于摆一个"看起来该填但没人填"的坑，
    # 下一个读代码的人会以为它有值。故删除；要查产出台数请看 run_metadata。

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "run_id": self.run_id,
            "status": self.status,
            "reused": self.reused,
            "used_rules_only": self.used_rules_only,
        }
        if self.stop_reason:
            payload["stop_reason"] = self.stop_reason
        if self.error:
            payload["error"] = self.error
        return payload


def create_run(
    run_repository: Any, request: RunRequest, *, parent_run_id: int | None = None
) -> tuple[int, str, bool]:
    """创建 Run 并**立即返回** `(run_id, status, reused)`（计划第 731 行验收）。

    相同幂等键且已有 `completed` → 返回那个 Run 并标 `reused=True`，
    **不重复执行、不重复计费**（计划第 703 行）。

    `parent_run_id`：重试产生的新 Run 用它串起谱系（计划第 353、692 行
    「failed 不可原地复活，只能新建 Run」）。这个参数一直在 model 与
    `create_queued` 里备着，却**从来没有人传过** —— 谱系字段一直是空的，
    正是"接口留好了、但没人接上"的那一类。
    """
    key = request.idempotency_key()
    if hasattr(run_repository, "lock_idempotency_key"):
        run_repository.lock_idempotency_key(key)

    reusable = run_repository.find_reusable(request.project_id, key)
    if reusable is not None:
        return int(reusable.id), reusable.status, True

    run = run_repository.create_queued(
        request.project_id,
        source_id=request.source_id,
        idempotency_key=key,
        run_input={**(request.run_input or {}), "domain_id": request.domain_id,
                   "domain_version": request.domain_version,
                   "time_range": {"start": request.time_start.isoformat(),
                                  "end": request.time_end.isoformat()},
                   "filters": request.filters, "start_tier": request.start_tier},
        parent_run_id=parent_run_id,
    )
    # 必须 flush 才能拿到自增 id —— 计划第 731 行要求"立即返回 run_id"，
    # 不 flush 的话 `run.id` 还是 None，返回值就是个空壳。
    session = getattr(run_repository, "session", None)
    if session is not None:
        session.flush()
    return int(run.id), run.status, False


def _last_error_from(outcome: Any) -> str | None:
    """从降级结果里取出最后一次尝试的错误原文（含异常类型名）。

    结构是嵌套的：`attempts[i]["retry"]["last_error"]` 才是形如
    `InputFormatError: 日志格式不认识` 的那一层；只读外层只会拿到 error_kind
    （如 `input`），丢掉类型名后排障时看不出是哪个异常。
    """
    if outcome is None:
        return None
    payload = outcome.as_dict()

    for attempt in reversed(payload.get("attempts") or []):
        retry = attempt.get("retry") or {}
        if retry.get("last_error"):
            return str(retry["last_error"])
        if attempt.get("error"):
            return str(attempt["error"])

    if payload.get("last_error"):
        return str(payload["last_error"])
    return None


class RunExecutor:
    """执行一个已创建的 Run，负责全部可靠性机制。"""

    def __init__(
        self,
        *,
        run_repository: Any,
        heartbeat_timeout_seconds: int = DEFAULT_HEARTBEAT_TIMEOUT_SECONDS,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
        now: Callable[[], datetime] | None = None,
        checkpoint: Callable[[], None] | None = None,
    ) -> None:
        validate_heartbeat_timeout(heartbeat_timeout_seconds)
        self.runs = run_repository
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self.retry_policy = retry_policy or RetryPolicy()
        self.sleep = sleep
        self.rng = rng
        self._now = now or (lambda: datetime.now(timezone.utc))
        #: 状态/心跳写完之后调用（生产传 `session.commit`）。
        #:
        #: 为什么必须有这个缝：不提交的话，从第一次 `apply_status(running)` 开始，
        #: 这条 `agent_runs` 行就被 Worker 的写锁占到任务结束 —— 别的连接（API 的取消、
        #: beat 的僵尸回收）只能干等。真机实测：分析进行到一半调取消，接口返回 200、
        #: `cancel_requested` 也确实落库了，但 Run 是**跑完之后**才看到这个标记的，
        #: 于是"取消"从未生效过（2026-09-24 真机核验报告 3.7）。
        #: 默认 None = 保持"整个任务一个事务"的旧行为，测试与脚本可以不出声地沿用。
        self._checkpoint = checkpoint

    def _commit_checkpoint(self) -> None:
        if self._checkpoint is not None:
            self._checkpoint()

    # ---------- 心跳 / 取消 ----------

    def heartbeat(self, project_id: int, run_id: int) -> None:
        self.runs.touch_heartbeat(project_id, run_id, at=self._now())
        # 心跳也要立刻落地并把行锁放掉：否则下一次心跳之前，取消/回收依然被挡在门外。
        self._commit_checkpoint()

    def check_cancel(self, project_id: int, run_id: int) -> None:
        """阶段边界与每次调用前的检查点（计划第 709 行）。"""
        check_cancelled(cancel_requested=self.runs.is_cancel_requested(project_id, run_id))

    # ---------- 主流程 ----------

    def execute(
        self,
        *,
        project_id: int,
        run_id: int,
        attempt_tier: Callable[[str], Any],
        start_tier: str,
        rules_only_fallback: Callable[[], Any] | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> RunOutcome:
        """执行 Run。任何路径都必须以明确状态收尾，不留 running 悬挂。"""
        # ① queued → running
        try:
            self.runs.apply_status(project_id, run_id, target=enums.AGENT_RUN_RUNNING)
            # 立刻落地：让别的连接（取消、僵尸回收）能拿到这一行，
            # 也让 started_at 从"分析真正开始的时刻"起算（月度预算的取数依据）。
            self._commit_checkpoint()
        except IllegalTransitionError as exc:
            # 状态已被别处改过（例如已被取消/回收）：如实返回，不强行覆盖
            current = self._current_status(project_id, run_id)
            return RunOutcome(
                run_id=run_id,
                status=current,
                error=f"无法进入 running：{exc}",
            )

        self.heartbeat(project_id, run_id)
        machine = StateMachine(status=enums.AGENT_RUN_RUNNING)

        try:
            self.check_cancel(project_id, run_id)

            outcome = call_with_fallback(
                lambda tier: attempt_tier(tier),
                start_tier=start_tier,
                rules_only_fallback=rules_only_fallback,
                retry_policy=self.retry_policy,
                sleep=self.sleep,
                rng=self.rng,
            )

            self.heartbeat(project_id, run_id)
            self.check_cancel(project_id, run_id)

            if outcome.ok and not outcome.used_rules_only:
                target = enums.AGENT_RUN_COMPLETED
                stop_reason = None
                error_message = None
            elif outcome.ok and outcome.used_rules_only:
                # 模型全不可用 → 纯规则报告：**必须**标 partial_success 并写明原因
                target = enums.AGENT_RUN_PARTIAL_SUCCESS
                stop_reason = STOP_MODEL_UNAVAILABLE
                error_message = "全部模型等级不可用，已输出纯规则报告"
            else:
                # 没有 ok 的等级尝试。这里要分两种情况 —— 混在一起会把
                # "钱花完了但已有结论"报成"跑挂了"。
                if outcome.stop_reason in POLICY_STOP_REASONS:
                    # 计划第 647–648 行：策略到顶 → **partial_success**，
                    # 返回已完成部分。理由：这不是故障，是"到点收工"；
                    # 报成 failed 会让已经有结论的 Run 看起来一无所有。
                    target = enums.AGENT_RUN_PARTIAL_SUCCESS
                    stop_reason = outcome.stop_reason
                    error_message = _last_error_from(outcome) or "已达策略上限，结果不完整"
                else:
                    # 全部等级失败且没有规则兜底
                    target = enums.AGENT_RUN_FAILED
                    stop_reason = outcome.stop_reason or STOP_MODEL_UNAVAILABLE
                    # 从尝试历史里取出真正的错误原文。
                    # 只落库不带回的话，调用方拿到的失败结果是"没有原因的失败"——
                    # 页面只能显示"失败了"，排障得去翻库，正是红线 4 要避免的含糊。
                    error_message = _last_error_from(outcome)

            metadata = self._metadata(
                machine, stop_reason, outcome, extra=extra_metadata
            )
            self.runs.apply_status(
                project_id,
                run_id,
                target=target,
                reason=stop_reason,
                metadata=metadata,
                error=error_message,
            )

            return RunOutcome(
                run_id=run_id,
                status=target,
                used_rules_only=outcome.used_rules_only,
                stop_reason=stop_reason,
                error=error_message,
                value=outcome.value,
                attempts=outcome.as_dict()["attempts"],
                metadata=metadata,
            )

        except CancelledError:
            machine.transition(enums.AGENT_RUN_CANCELLED, reason="cancel_requested")
            metadata = self._metadata(machine, "cancelled", None)
            self.runs.apply_status(
                project_id,
                run_id,
                target=enums.AGENT_RUN_CANCELLED,
                reason="cancel_requested",
                metadata=metadata,
            )
            return RunOutcome(
                run_id=run_id, status=enums.AGENT_RUN_CANCELLED, stop_reason="cancelled"
            )

        except NonRetryableError as exc:
            # 计划第 728 行：失败不静默 —— 置 failed 并写明原因
            return self._fail(project_id, run_id, machine, exc)

        except Exception as exc:  # noqa: BLE001 - 任何未预期异常也必须明确收尾
            return self._fail(project_id, run_id, machine, exc)

    # ---------- 内部 ----------

    def _fail(
        self, project_id: int, run_id: int, machine: StateMachine, exc: BaseException
    ) -> RunOutcome:
        kind = classify_error(exc)
        message = f"{type(exc).__name__}: {exc}"
        try:
            machine.transition(enums.AGENT_RUN_FAILED, reason=kind)
            self.runs.apply_status(
                project_id,
                run_id,
                target=enums.AGENT_RUN_FAILED,
                reason=kind,
                error=message,
                metadata=self._metadata(machine, kind, None),
            )
            status = enums.AGENT_RUN_FAILED
        except IllegalTransitionError:
            # 已经处于终态：不要强行覆盖，如实返回当前状态
            status = self._current_status(project_id, run_id)
        return RunOutcome(
            run_id=run_id, status=status, stop_reason=kind, error=message
        )

    def _metadata(
        self,
        machine: StateMachine,
        stop_reason: str | None,
        outcome: Any,
        *,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "completed_phases": machine.completed_phases(),
            "skipped_phases": machine.skipped_phases(),
        }
        if stop_reason:
            metadata["stop_reason"] = stop_reason
            metadata["note"] = (
                "分析因预算/时限/模型不可用中断，结果可能不完整"
                if stop_reason != ErrorKind.INPUT
                else "分析因输入问题未完成"
            )
        if outcome is not None:
            metadata["attempts"] = outcome.as_dict()["attempts"]
        if extra:
            # 调用方补充的信息（如落库后的结论条数）——不覆盖已有键，
            # 已有的键来自状态机与中断原因，优先级更高。
            for key, value in extra.items():
                metadata.setdefault(key, value)
        return metadata

    def _current_status(self, project_id: int, run_id: int) -> str:
        """当前状态：查库取，不读会话里那个可能已过期的对象。"""
        fresh = getattr(self.runs, "status_of", None)
        if callable(fresh):
            return fresh(project_id, run_id) or "unknown"
        run = self.runs.get(project_id, run_id)
        return str(run.status) if run is not None else "unknown"
