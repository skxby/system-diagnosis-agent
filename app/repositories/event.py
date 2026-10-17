"""Event 仓库。

验收要点（计划第 365 行）：能插入普通事件与指标事件并按 project 查回。
"""

from __future__ import annotations

from typing import Any
from datetime import datetime

from sqlalchemy import select

from app.models import enums
from app.models.event import Event
from app.repositories.base import ProjectScopedRepository


class EventRepository(ProjectScopedRepository[Event]):
    model = Event

    def list_metric_events(self, project_id: int) -> list[Event]:
        """指标事件：`event_type="metric"`，数值在 payload 里（指标不单独建表）。"""
        return list(
            self.session.execute(
                self.scoped(project_id)
                .where(Event.event_type == enums.EVENT_TYPE_METRIC)
                .order_by(Event.timestamp)
            )
            .scalars()
            .all()
        )

    def list_log_events(self, project_id: int) -> list[Event]:
        return list(
            self.session.execute(
                self.scoped(project_id)
                .where(Event.event_type == enums.EVENT_TYPE_LOG)
                .order_by(Event.timestamp)
            )
            .scalars()
            .all()
        )

    def list_by_group(self, project_id: int, group_id: int) -> list[Event]:
        """按分组取成员事件。

        这正是「EventGroup 不存 event_ids 反向数组」后推荐的查法：
        `WHERE group_id = ?`（计划第 358 行）。
        """
        return list(
            self.session.execute(
                self.scoped(project_id)
                .where(Event.group_id == group_id)
                .order_by(Event.timestamp)
            )
            .scalars()
            .all()
        )

    def pipeline_events(
        self, project_id: int, *, limit: int = 50_000,
        source_id: int | None = None, time_start: datetime | None = None,
        time_end: datetime | None = None, filters: dict[str, Any] | None = None,
        max_event_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """装载喂给分析链路的事件字典 —— **唯一**的 ORM→链路字段映射。

        为什么必须是唯一一处：这段映射很容易漏字段，而漏掉的后果是**静默的**。
        `metadata` 里的 `proc`（进程名）就是活例子 —— 少了它，
        `ProcessCrash` 看不到 `CrashReporterSupportHelper` 这类崩溃上报进程，
        候选异常为 0 → 复杂度评为 L0 → 走"纯规则报告"分支**根本不会调用模型**，
        最后以"分析完成、零结论、零花费"收场：每一步都"成功"，真正的原因
        （字段没装载）一个字都不剩。同一条映射抄在两个地方就必然会漂移，
        所以 Worker 与本函数共用它。
        """
        query = self.scoped(project_id)
        if source_id is not None:
            query = query.where(Event.source_id == source_id)
        if time_start is not None:
            query = query.where(Event.timestamp >= time_start)
        if time_end is not None:
            query = query.where(Event.timestamp <= time_end)
        if max_event_id is not None:
            query = query.where(Event.id <= max_event_id)
        for key, value in (filters or {}).items():
            if key in {"severity", "event_type"} and value:
                query = query.where(getattr(Event, key).in_(value))
            elif key == "keyword" and value:
                query = query.where(Event.message.contains(value, autoescape=True))
        rows = list(self.session.execute(
            query.order_by(Event.timestamp, Event.id).limit(limit + 1)
        ).scalars())
        if len(rows) > limit:
            raise ValueError(f"分析范围超过 {limit} 条事件，请缩小时间窗；未静默截断")
        return [
            {
                "event_id": int(row.id),
                "source_id": int(row.source_id),
                "timestamp": row.timestamp,
                "severity": row.severity,
                "event_type": row.event_type,
                "message": row.message,
                "payload": row.payload,
                # 列名 metadata、属性名 meta；analyzer 依赖其中的 proc/pid/host
                "metadata": dict(row.meta or {}),
            }
            for row in rows
        ]

    def count_by_severity(self, project_id: int) -> dict[str, int]:
        """各 severity 分布（阶段 05 的 stats_calculator 会用到）。"""
        from sqlalchemy import func

        rows = self.session.execute(
            select(Event.severity, func.count())
            .where(Event.project_id == project_id)
            .group_by(Event.severity)
        ).all()
        return {severity: int(count) for severity, count in rows}

    def assign_group(self, project_id: int, event_ids: list[Any], group_id: int) -> int:
        """把成员事件挂到分组上（`Event.group_id` 是分组关系的**真源**）。

        为什么用一条 UPDATE 而不是逐条改对象：分组一次可能覆盖几千条事件，
        逐条走 ORM 会把内存与 SQL 次数都放大到不可接受。
        返回受影响行数 —— 调用方据此判断"分组是不是真的落上了"。
        """
        from sqlalchemy import update

        ids = [int(e) for e in event_ids]
        if not ids:
            return 0
        result = self.session.execute(
            update(Event)
            .where(Event.project_id == project_id, Event.id.in_(ids))
            .values(group_id=group_id)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    def assign_incident(self, project_id: int, group_ids: list[Any], incident_id: int) -> int:
        """按分组把成员事件挂到事故上（成员关系由 `group_id` 派生，不双写）。"""
        from sqlalchemy import update

        ids = [int(g) for g in group_ids]
        if not ids:
            return 0
        result = self.session.execute(
            update(Event)
            .where(Event.project_id == project_id, Event.group_id.in_(ids))
            .values(incident_id=incident_id)
            .execution_options(synchronize_session=False)
        )
        return int(result.rowcount or 0)

    def events_in_groups(self, project_id: int, group_ids: list[Any]) -> list[int]:
        """按分组取成员事件 id（`incident_id → group_ids → events` 的最后一步）。"""
        ids = [int(g) for g in group_ids]
        if not ids:
            return []
        rows = self.session.execute(
            select(Event.id)
            .where(Event.project_id == project_id, Event.group_id.in_(ids))
            .order_by(Event.id)
        ).scalars().all()
        return [int(r) for r in rows]

    def add_metric(
        self,
        project_id: int,
        *,
        source_id: int,
        timestamp: Any,
        metric_name: str,
        value: float,
        unit: str,
        severity: str = enums.SEVERITY_LOW,
    ) -> Event:
        """便捷构造指标事件，保证 payload 结构一致。"""
        return self.add(
            project_id,
            Event(
                project_id=project_id,
                source_id=source_id,
                timestamp=timestamp,
                event_type=enums.EVENT_TYPE_METRIC,
                severity=severity,
                message=f"{metric_name}={value}{unit}",
                payload={"metric_name": metric_name, "value": value, "unit": unit},
            ),
        )
