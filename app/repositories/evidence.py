"""Evidence 仓库。

本表**没有** project_id 列（计划表结构如此）——它通过 `insight_id` 归属到
Project。因此不能直接继承 ProjectScopedRepository，改为所有方法都要求
project_id 并 JOIN insights 校验归属。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.evidence import Evidence
from app.models.insight import Insight


class EvidenceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _scoped(self, project_id: int):
        """经 insights 关联到 project —— Evidence 的隔离路径。"""
        if project_id is None:
            raise ValueError("project_id 不能为空：Project 是上下文边界")
        return (
            select(Evidence)
            .join(Insight, Evidence.insight_id == Insight.id)
            .where(Insight.project_id == project_id)
        )

    def list_for_insight(self, project_id: int, insight_id: int) -> list[Evidence]:
        return list(
            self.session.execute(
                self._scoped(project_id)
                .where(Evidence.insight_id == insight_id)
                .order_by(Evidence.id)
            ).scalars().all()
        )

    def list_for_insights(self, project_id: int, insight_ids: list[int]) -> list[Evidence]:
        return list(self.session.scalars(self._scoped(project_id)
                    .where(Evidence.insight_id.in_(insight_ids)).order_by(Evidence.id)).all())

    def add(self, project_id: int, evidence: Evidence) -> Evidence:
        """插入前校验：其 insight 必须属于同一 project。"""
        insight = self.session.get(Insight, evidence.insight_id)
        if insight is None:
            raise ValueError(f"insight_id={evidence.insight_id} 不存在")
        if insight.project_id != project_id:
            raise ValueError(
                f"insight {evidence.insight_id} 属于 project {insight.project_id}，"
                f"与传入的 project_id={project_id} 不一致：拒绝跨项目写入"
            )
        self.session.add(evidence)
        return evidence
