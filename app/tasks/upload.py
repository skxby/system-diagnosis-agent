"""仅消费已脱敏文件的异步解析任务。"""
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from sqlalchemy.orm import Session

from app.celery_app import celery_app
from app.db import SessionLocal
from app.repositories.upload_job import UploadJobRepository, public_job
from app.services.upload_service import UploadService, MAX_UPLOAD_BYTES
from app.utils.observability import bind_run_context, get_logger

logger = get_logger(__name__)


@celery_app.task(name="app.tasks.upload.execute_upload")
def execute_upload(*, project_id: int, source_id: int, job_id: str, trace_id: str | None = None) -> dict:
    with bind_run_context(trace_id=trace_id, project_id=project_id):
        return process_upload(project_id=project_id, source_id=source_id, job_id=job_id)


def process_upload(*, project_id: int, source_id: int, job_id: str,
                   session_factory: Callable[[], Session] = SessionLocal) -> dict:
    with session_factory() as session:
        repo = UploadJobRepository(session)
        source = repo.source(project_id, source_id, lock=True)
        job = dict((source.meta or {}).get("upload_jobs", {})[job_id])
        if job["status"] != "queued":
            return public_job(job)
        repo.update(project_id, source_id, job_id, {"status": "running", "started_at": _now()})
        session.commit()
        try:
            # 跨 Worker 串行同来源导入，但不会持有阻塞 API 排队的来源行锁。
            from sqlalchemy import text
            session.execute(text("SELECT pg_advisory_xact_lock(:source)"), {"source": source_id})
            session.refresh(source)
            cached = (source.meta or {}).get("uploads", {}).get(job["digest"])
            if cached:
                result = {**cached, "data_source_id": source_id, "format": job["format"],
                    "created_data_source": False, "events_persisted": 0,
                    "parse": {**cached["parse"], "duplicate": True}}
            else:
                root = Path(job["uploads_dir"]) / str(project_id)
                path = (root / job["stored_filename"]).resolve()
                if not path.is_relative_to(root.resolve()) or path.stat().st_size > MAX_UPLOAD_BYTES * 8:
                    raise ValueError("不合法的脱敏文件边界")
                service = UploadService(session, uploads_dir=root.parent,
                    domain_id=job["domain_id"], domain_version=job["domain_version"], default_timezone=job["timezone"])
                count, stats = service.parse_masked(project_id, source_id, path.read_text(encoding="utf-8"),
                    fmt=job["format"], encoding=job["encoding"], mask_ip=job["mask_ip"],
                    now=datetime.fromisoformat(job["reference_time"]))
                result = {"data_source_id": source_id, "created_data_source": job["created_data_source"],
                    "format": job["format"], "stored_filename": job["stored_filename"],
                    "events_persisted": count, "parse": stats, "mask": job["mask"]}
            finished = repo.update(project_id, source_id, job_id,
                {"status": "completed", "finished_at": _now(), "result": result}, expected={"running"},
                upload={key: result[key] for key in ("stored_filename", "parse", "mask")})
            if finished["status"] != "completed":
                session.rollback()  # 终态被监督器改写时不提交部分事件。
            else:
                session.commit()
            logger.info("upload_completed", job_id=job_id, events=result["events_persisted"])
            return public_job(finished)
        except Exception as exc:
            session.rollback()
            repo.update(project_id, source_id, job_id, {"status": "failed", "finished_at": _now(),
                "error": f"上传解析失败（{type(exc).__name__}），请检查文件或重试"}, expected={"running"})
            session.commit()
            logger.exception("upload_failed", job_id=job_id)
            raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
