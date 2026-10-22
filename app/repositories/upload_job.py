"""上传任务存入来源 metadata，不新增表或改变 AgentRun 状态机。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.datasource import DataSource


class UploadJobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def source(self, project_id: int, source_id: int, *, lock: bool = False) -> DataSource:
        query = select(DataSource).where(DataSource.project_id == project_id, DataSource.id == source_id)
        if lock:
            self.session.flush()
            # NO KEY UPDATE 与 Event 外键的 KEY SHARE 兼容，不把排队 API 挡在批量导入后面。
            query = query.with_for_update(key_share=True).execution_options(populate_existing=True)
        source = self.session.scalar(query)
        if source is None:
            raise ValueError("数据源不存在或不属于当前项目")
        return source

    def get(self, project_id: int, job_id: str) -> tuple[DataSource, dict[str, Any]]:
        rows = self.session.scalars(select(DataSource).where(DataSource.project_id == project_id))
        for source in rows:
            job = (source.meta or {}).get("upload_jobs", {}).get(job_id)
            if job is not None:
                return source, dict(job)
        raise KeyError("上传任务不存在")

    def create(self, project_id: int, source_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        source = self.source(project_id, source_id, lock=True)
        jobs = dict((source.meta or {}).get("upload_jobs") or {})
        for old in jobs.values():
            if old.get("digest") == fields["digest"] and old["status"] in {"queued", "running", "completed"}:
                return {**old, "reused": True}
        job = {**fields, "job_id": uuid4().hex, "data_source_id": source_id,
               "project_id": project_id, "status": "queued", "created_at": datetime.now(timezone.utc).isoformat()}
        jobs[job["job_id"]] = job
        source.meta = {**(source.meta or {}), "upload_jobs": jobs}
        self.session.flush()
        return job

    def update(self, project_id: int, source_id: int, job_id: str,
               fields: dict[str, Any], *, expected: set[str] | None = None,
               upload: dict[str, Any] | None = None) -> dict[str, Any]:
        source = self.source(project_id, source_id, lock=True)
        metadata = dict(source.meta or {})
        jobs = dict(metadata.get("upload_jobs") or {})
        job = dict(jobs[job_id])
        if expected is not None and job["status"] not in expected:
            return job
        job.update(fields)
        jobs[job_id] = job
        metadata["upload_jobs"] = jobs
        if upload is not None:
            metadata["uploads"] = {**(metadata.get("uploads") or {}), job["digest"]: upload}
        source.meta = metadata
        self.session.flush()
        return job

    def list(self, project_id: int) -> list[dict[str, Any]]:
        jobs = []
        for source in self.session.scalars(select(DataSource).where(DataSource.project_id == project_id)):
            jobs.extend((source.meta or {}).get("upload_jobs", {}).values())
        return sorted(jobs, key=lambda item: item["created_at"], reverse=True)[:20]


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    fields = {key: job.get(key) for key in ("job_id", "status", "data_source_id", "created_at",
              "started_at", "finished_at", "error", "reused", "created_data_source", "format", "stored_filename")}
    fields.update(job.get("result") or {})
    if job.get("reused") and job["status"] == "completed":
        fields["events_persisted"] = 0
        fields["parse"] = {**fields["parse"], "duplicate": True}
    return fields
