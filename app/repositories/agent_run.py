"""AgentRun 仓库。

验收要点（计划第 365 行）：`model_calls` 可追加记录。

`model_calls` 是 JSON 数组，追加 = 读-改-写。并发下这一步需要行级锁，
故 `append_model_call` 用 `with_for_update()` 取行——避免两次调用互相覆盖
（静默丢数据比报错更危险，红线 4）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text

from app.models import enums
from app.models.agent_run import AgentRun
from app.repositories.base import ProjectScopedRepository

logger = logging.getLogger(__name__)


class AgentRunRepository(ProjectScopedRepository[AgentRun]):
    model = AgentRun

    def lock_idempotency_key(self, key: str) -> None:
        self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                             {"key": int(key[:15], 16)})

    # ---------- 创建与幂等 ----------

    def create_queued(
        self,
        project_id: int,
        *,
        source_id: int | None = None,
        idempotency_key: str | None = None,
        run_input: dict[str, Any] | None = None,
        parent_run_id: int | None = None,
    ) -> AgentRun:
        """创建一个 `queued` 的 Run。

        计划第 731 行验收：创建 Run **立即返回** `run_id + queued`。
        所以这里只落一行 queued，不做任何分析工作。
        """
        run = AgentRun(
            project_id=project_id,
            source_id=source_id,
            idempotency_key=idempotency_key,
            input=run_input,
            parent_run_id=parent_run_id,
            status=enums.AGENT_RUN_QUEUED,
            run_metadata={"queued_at": datetime.now(timezone.utc).isoformat()},
        )
        return self.add(project_id, run)

    def find_reusable(self, project_id: int, idempotency_key: str) -> AgentRun | None:
        """找可复用的成功 Run（计划第 703 行：不重复执行、不重复计费）。

        completed 复用结果；queued/running 复用正在处理的任务，避免双击重复计费。
        partial_success 不作为完整结果复用。
        """
        return self.session.execute(
            self.scoped(project_id)
            .where(
                AgentRun.idempotency_key == idempotency_key,
                AgentRun.status.in_([enums.AGENT_RUN_COMPLETED, enums.AGENT_RUN_QUEUED,
                                     enums.AGENT_RUN_RUNNING]),
            )
            .order_by(AgentRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    def find_by_idempotency_key(
        self, project_id: int, idempotency_key: str
    ) -> AgentRun | None:
        """任意状态下按幂等键取最近一条（便于告知"已有一个在跑"）。"""
        return self.session.execute(
            self.scoped(project_id)
            .where(AgentRun.idempotency_key == idempotency_key)
            .order_by(AgentRun.id.desc())
            .limit(1)
        ).scalar_one_or_none()

    # ---------- 心跳与取消 ----------

    def touch_heartbeat(
        self, project_id: int, run_id: int, *, at: datetime | None = None
    ) -> bool:
        """更新心跳（计划第 705 行：各阶段及模型调用前后都要更新）。"""
        run = self.get(project_id, run_id)
        if run is None:
            return False
        run.last_heartbeat = at or datetime.now(timezone.utc)
        return True

    def request_cancel(self, project_id: int, run_id: int) -> bool:
        """置取消标记（计划第 709 行）。幂等：重复置位无副作用。"""
        run = self.get(project_id, run_id)
        if run is None:
            return False
        run.cancel_requested = True
        return True

    def is_cancel_requested(self, project_id: int, run_id: int) -> bool:
        """取消标记的**即时**读取（计划第 709 行）。

        为什么直接查列、而不是读 ORM 对象：
        Worker 的会话里这个 Run 早就在身份映射里了（`_execute` 开头载入过），
        而 SQLAlchemy 对**已加载**的对象默认不用库里新值覆盖它
        （要覆盖得显式 `populate_existing()`）。于是 API 进程置的位在 Worker 侧
        永远看不见 —— 真机实测过：`cancel_requested` 已落库，Run 仍跑到 completed。
        查标量列绕开身份映射，读到的就是库里的当前值。
        """
        value = self.session.execute(
            select(AgentRun.cancel_requested).where(
                AgentRun.project_id == project_id, AgentRun.id == run_id
            )
        ).scalar_one_or_none()
        return bool(value)

    def find_zombie_runs(
        self,
        *,
        timeout_seconds: int,
        now: datetime | None = None,
        project_id: int | None = None,
    ) -> list[AgentRun]:
        """找出运行心跳或新任务排队时间超时的 Run（计划第 706 行）。

        `project_id=None` 表示跨项目扫描 —— 这是**为运维/Beat 任务准备的**，
        正常业务路径应传 project_id 保持隔离。
        """
        reference_time = now or datetime.now(timezone.utc)
        deadline = reference_time - timedelta(seconds=timeout_seconds)

        statement = select(AgentRun).where(
            AgentRun.status.in_([enums.AGENT_RUN_RUNNING, enums.AGENT_RUN_QUEUED]),
            # 心跳与开始时间都早于截止时刻（心跳为空则看开始时间）
            (AgentRun.last_heartbeat < deadline)
            | (AgentRun.last_heartbeat.is_(None) & (AgentRun.started_at < deadline))
            | ((AgentRun.status == enums.AGENT_RUN_QUEUED)
               & (AgentRun.run_metadata["queued_at"].astext < deadline.isoformat())),
        )
        if project_id is not None:
            statement = statement.where(AgentRun.project_id == project_id)
        return list(
            self.session.execute(statement.order_by(AgentRun.id)).scalars().all()
        )

    def reclaim_zombies(
        self,
        *,
        timeout_seconds: int,
        now: datetime | None = None,
        project_id: int | None = None,
    ) -> list[int]:
        """把僵尸 Run 置为 `timeout`，返回被回收的 run_id 列表（计划第 706 行）。

        必须经状态机判定，不能直接赋值 status —— 否则"终态不可再改"这条
        就没有机制保障了。
        """
        from app.analysis.state_machine import IllegalTransitionError

        self.session.flush()
        reclaimed: list[int] = []
        deadline = (now or datetime.now(timezone.utc)) - timedelta(seconds=timeout_seconds)
        for candidate in self.find_zombie_runs(timeout_seconds=timeout_seconds, now=now, project_id=project_id):
            run = self.session.execute(self.scoped(candidate.project_id)
                .where(AgentRun.id == candidate.id).with_for_update()
                .execution_options(populate_existing=True)).scalar_one()
            if run.status not in {enums.AGENT_RUN_RUNNING, enums.AGENT_RUN_QUEUED}:
                continue
            heartbeat = run.last_heartbeat or run.started_at
            if heartbeat is None:
                try:
                    heartbeat = datetime.fromisoformat((run.run_metadata or {})["queued_at"])
                except (KeyError, ValueError, TypeError):
                    logger.warning("Run %s 缺少可解析的排队时间，无法自动判定超时", run.id)
                    continue
            if heartbeat >= deadline:
                continue  # 锁前看到的旧心跳已被另一个进程更新
            try:
                note = ("排队超时，Run 已被回收为 timeout" if run.status == enums.AGENT_RUN_QUEUED
                        else "Worker 心跳丢失，Run 已被回收为 timeout")
                self.apply_status(run.project_id, run.id, target=enums.AGENT_RUN_TIMEOUT,
                    reason="heartbeat_lost", error=run.error or "heartbeat lost",
                    metadata={"stop_reason": "timeout", "note": note})
            except IllegalTransitionError:
                continue
            run.finished_at = now or datetime.now(timezone.utc)
            reclaimed.append(int(run.id))
        return reclaimed

    # ---------- 成本汇总 ----------

    def cost_sum_since(self, project_id: int, since: datetime) -> float:
        """该项目自 `since` 起的累计实际花费（元）。

        用于月度预算的 Pre-check（计划第 644 行「月度预算或 Run 预算不足 → 拒绝创建」）。
        以 `started_at` 为准（冻结表里 AgentRun 没有 created_at）：Run 的钱是从
        开始执行那一刻花的；尚未开始的 Run（`started_at` 为空）花费恒为 0，被自然排除。
        `cost_actual` 是 `Numeric`，故这里显式转 float，别把 Decimal 漏进业务层。
        """
        from sqlalchemy import func

        total = self.session.execute(
            select(func.coalesce(func.sum(AgentRun.cost_actual), 0)).where(
                AgentRun.project_id == project_id,
                AgentRun.started_at.is_not(None),
                AgentRun.started_at >= since,
            )
        ).scalar_one()
        return float(total or 0.0)

    # ---------- 状态写入 ----------

    def status_of(self, project_id: int, run_id: int) -> str | None:
        """当前状态的**即时**读取（查标量列，绕开身份映射）。

        为什么不能读 ORM 对象：Worker 的会话从 `_execute` 开头就持有这个 Run，
        它的 `status` 停留在载入那一刻。真机复验撞到过后果：
        Run 已被回收成 `timeout`（终态），解冻后的 Worker 仍按内存里的 `running`
        做状态判定，于是把终态**复活**成 `completed` ——
        "不可再改"这条约束在跨进程场景下形同虚设。

        先 `flush()` 再查：本项目的 sessionmaker 是 `autoflush=False`，
        不先落盘的话，**同一会话里刚写的状态**（比如刚置的 running）查不到，
        会得出"还是 queued"这种自相矛盾的结论。
        """
        self.session.flush()
        value = self.session.execute(
            select(AgentRun.status).where(
                AgentRun.project_id == project_id, AgentRun.id == run_id
            )
        ).scalar_one_or_none()
        return str(value) if value is not None else None

    def metadata_of(self, project_id: int, run_id: int) -> dict[str, Any]:
        """`run_metadata` 的**即时**读取（查标量列，绕开身份映射）。

        同样先 flush：本项目 `autoflush=False`，不落盘就会读到自己写入之前的旧值。
        """
        self.session.flush()
        value = self.session.execute(
            select(AgentRun.run_metadata).where(
                AgentRun.project_id == project_id, AgentRun.id == run_id
            )
        ).scalar_one_or_none()
        return dict(value or {})

    def merge_run_metadata(
        self, project_id: int, run_id: int, extra: dict[str, Any]
    ) -> dict[str, Any] | None:
        """把额外信息并入 `run_metadata`：**先查库取当前值**，再合并写回。

        为什么要查库：Worker 会话里的对象可能载入于几分钟前，直接
        `{**run.run_metadata, **extra}` 会把别人（回收者 / 取消者）写进去的 key 抹掉。
        真机复验撞到过：Run 被回收成 `timeout`（写入 stop_reason 与 note），
        解冻后迟到的 Worker 用旧值合并，把原因覆盖没了 ——
        库里只剩"状态 timeout、没有为什么"，正是红线 4 要禁止的含糊。
        """
        run = self.get(project_id, run_id)
        if run is None:
            return None
        merged = {**self.metadata_of(project_id, run_id), **extra}
        run.run_metadata = merged
        return merged

    def apply_status(
        self,
        project_id: int,
        run_id: int,
        *,
        target: str,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        """经状态机写入新状态。非法转移会抛错，不静默改。"""
        from app.analysis.state_machine import StateMachine

        self.session.flush()
        run = self.session.execute(self.scoped(project_id).where(AgentRun.id == run_id)
            .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if run is None:
            return False

        # 状态判定必须用**库里的当前值**：同一会话里的对象可能是几分钟前载入的，
        # 而这段时间里 Run 可能已经被回收/取消。
        current = self.status_of(project_id, run_id)
        if current is None:
            return False
        machine = StateMachine(
            status=current,
            current_phase=run.current_phase,
            phase_history=run.phase_history,
        )
        machine.transition(target, reason=reason)

        run.status = machine.status
        run.phase_history = machine.phase_history
        # 执行起点：**只在首次进入 running 时写**。
        #
        # 它是月度预算的取数依据（`cost_sum_since` 的 SQL 带 `started_at is not null`），
        # 而此前没有任何代码写过它 —— 真机核验发现全库 177 条 Run、其中 91 条真的花过钱，
        # `started_at` 非空的是 0 条：真实花费被 SQL 全部排除，"本月已花"恒为 0，
        # 月度闸门对真实用量永远不触发（2026-09-24 真机核验报告 3.4）。
        if target == enums.AGENT_RUN_RUNNING and run.started_at is None:
            run.started_at = datetime.now(timezone.utc)
        if target in enums.AGENT_RUN_TERMINAL_STATES:
            run.finished_at = datetime.now(timezone.utc)
        if metadata is not None:
            # **合并**而不是覆盖。`run_metadata` 是累积容器：创建 Run 时写进去的
            # trace_id（计划第 928 行）与策略快照（第 356 行）必须活到终态之后。
            #
            # 覆盖式写入的后果实测过：Worker 一跑完，Run 上的 trace_id 就没了 ——
            # "事后用 trace_id 把这次分析的日志全捞出来"直接落空。更阴险的是
            # 它只在**分析真的跑完**时才发生：Run 卡在 queued 时 trace_id 反而还在，
            # 于是"越正常越丢"，看单条记录根本发现不了。
            run.run_metadata = {**(run.run_metadata or {}), **metadata}
        if error is not None:
            run.error = error
        return True

    def apply_progress(
        self,
        project_id: int,
        run_id: int,
        *,
        current_phase: str | None = None,
        phase_history: list[dict[str, Any]] | None = None,
    ) -> bool:
        """写入阶段进度（与 status 正交，计划第 354 行）。"""
        run = self.get(project_id, run_id)
        if run is None:
            return False
        if current_phase is not None:
            run.current_phase = current_phase
        if phase_history is not None:
            run.phase_history = phase_history
        return True

    def append_model_call(
        self, project_id: int, run_id: int, call: dict[str, Any]
    ) -> bool:
        """向 `model_calls` 追加一条记录。

        用行级锁串行化，防止并发追加时后写覆盖先写。
        """
        run = self.session.execute(
            self.scoped(project_id).where(AgentRun.id == run_id).with_for_update()
        ).scalar_one_or_none()
        if run is None:
            return False
        # JSONB 列的可变对象需要重新赋值才会被标记为脏
        calls = list(run.model_calls or [])
        calls.append(call)
        run.model_calls = calls
        return True

    def model_calls_of(self, project_id: int, run_id: int) -> list[dict[str, Any]]:
        run = self.get(project_id, run_id)
        return list(run.model_calls or []) if run is not None else []

    def append_tool_usage(
        self, project_id: int, run_id: int, usage: dict[str, Any]
    ) -> bool:
        """把一次工具执行记录追加到 `tool_usage`（阶段 05 验收：执行结果可记录到 Run）。

        `tool_usage` 是 JSONB，故写入前先确认可序列化：工具输出里可能混入
        `datetime` 之类的对象，直接塞进去会在 flush 时抛 TypeError
        （阶段 04 已经在这上面栽过一次）。不可序列化时**明确报错**，
        而不是让脏数据悄悄进库。
        """
        import json

        try:
            json.dumps(usage)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"tool_usage 含有不可 JSON 序列化的内容：{type(exc).__name__}: {exc}"
            ) from exc

        run = self.session.execute(
            self.scoped(project_id).where(AgentRun.id == run_id).with_for_update()
        ).scalar_one_or_none()
        if run is None:
            return False

        existing = run.tool_usage or {}
        # 约定结构：{"<tool_name>": [<每次执行的记录>, ...]}
        tool_name = str(usage.get("tool", "unknown"))
        history = list(existing.get(tool_name, []))
        history.append(usage)
        run.tool_usage = {**existing, tool_name: history}
        return True

    def tool_usage_of(self, project_id: int, run_id: int) -> dict[str, Any]:
        run = self.get(project_id, run_id)
        return dict(run.tool_usage or {}) if run is not None else {}
