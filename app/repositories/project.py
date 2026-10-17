"""Project 仓库。

Project 本身即边界，故不按 project_id 过滤 —— 它按 user_id 过滤（谁的账号）。
"""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session
from decimal import Decimal

from app.models.project import Project


class ProjectRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_for_user(self, user_id: int) -> list[Project]:
        return list(
            self.session.execute(
                select(Project).where(Project.user_id == user_id).order_by(Project.id)
            ).scalars().all()
        )

    def get_for_user(self, user_id: int, project_id: int) -> Project | None:
        """按 (user_id, project_id) 取，防越权访问他人项目。"""
        return self.session.execute(
            select(Project).where(Project.id == project_id, Project.user_id == user_id)
        ).scalar_one_or_none()

    def add(self, project: Project) -> Project:
        self.session.add(project)
        return project

    def add_budget_used(self, project_id: int, amount: float) -> float:
        """把一次 Run 的实际花费累加进 `budget_used`，返回累加后的值。

        这是计划第 650–652 行 Post-check 的"累加回 Project.budget_used"那一半。
        此前没人调用它 —— 后果是 `budget_used` 永远是 0：项目预算**永远花不完**，
        于是"月度预算不足就拒绝创建"这道闸门即使接上也不会触发。

        `amount <= 0` 时也照样写回（幂等：写的是同一个值），但不做无谓的加零。
        """
        value = self.session.execute(
            update(Project).where(Project.id == project_id)
            .values(budget_used=Project.budget_used + Decimal(str(max(0.0, float(amount)))))
            .returning(Project.budget_used)
            .execution_options(synchronize_session="fetch")
        ).scalar_one_or_none()
        if value is None:
            raise ValueError(f"Project {project_id} 不存在，拒绝写预算")
        return float(value)
