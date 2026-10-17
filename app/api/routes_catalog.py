"""项目内分组与事故的只读浏览入口。"""
from typing import Annotated

from fastapi import APIRouter, Query
from sqlalchemy import select, func

from app.api.deps import ProjectScopeDep, SessionDep
from app.models.event import Event
from app.models.event_group import EventGroup
from app.models.incident import Incident

router = APIRouter()


@router.get("/api/projects/{project_id}/groups", tags=["groups"])
def list_groups(scope: ProjectScopeDep, session: SessionDep,
                limit: Annotated[int, Query(ge=1, le=100)] = 50,
                before_id: Annotated[int | None, Query(ge=1)] = None) -> list[dict]:
    # 成员计数从 Event.group_id 派生，不把历史缓存计数当成当前归属。
    query = select(EventGroup, func.count(Event.id)).outerjoin(Event,
        (Event.group_id == EventGroup.id) & (Event.project_id == scope.project_id)
    ).where(EventGroup.project_id == scope.project_id).group_by(EventGroup.id)
    if before_id is not None:
        query = query.where(EventGroup.id < before_id)
    return [{"id": row.id, "source_id": row.source_id, "template": row.template,
             "event_count": count, "time_start": row.time_start, "time_end": row.time_end}
            for row, count in session.execute(query.order_by(EventGroup.id.desc()).limit(limit))]


@router.get("/api/projects/{project_id}/groups/{group_id}/events", tags=["groups"])
def group_events(group_id: int, scope: ProjectScopeDep, session: SessionDep,
                 limit: Annotated[int, Query(ge=1, le=100)] = 100,
                 before_id: Annotated[int | None, Query(ge=1)] = None) -> list[dict]:
    query = select(Event).where(Event.project_id == scope.project_id, Event.group_id == group_id)
    if before_id is not None:
        query = query.where(Event.id < before_id)
    return [{"id": row.id, "timestamp": row.timestamp, "severity": row.severity,
             "message": row.message} for row in session.scalars(query.order_by(Event.id.desc()).limit(limit))]


@router.get("/api/projects/{project_id}/incidents", tags=["incidents"])
def list_incidents(scope: ProjectScopeDep, session: SessionDep,
                   limit: Annotated[int, Query(ge=1, le=100)] = 50,
                   before_id: Annotated[int | None, Query(ge=1)] = None) -> list[dict]:
    query = select(Incident).where(Incident.project_id == scope.project_id)
    if before_id is not None:
        query = query.where(Incident.id < before_id)
    return [{"id": row.id, "title": row.title, "severity": row.severity,
             "group_ids": row.group_ids, "source_run_id": row.source_run_id,
             "time_start": row.time_start, "time_end": row.time_end}
            for row in session.scalars(query.order_by(Incident.id.desc()).limit(limit))]
