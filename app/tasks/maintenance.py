"""周期维护任务（Celery beat 驱动）。

## 为什么必须有这个模块

阶段 08 的验收里有一条「手动 kill Worker 后僵尸 Run 被回收」。
`AgentRunRepository.reclaim_zombies` 早就写好了、单测也全绿，
但**没有任何真实路径调用过它**，beat 容器更是连一条 `beat_schedule` 都没有 ——
于是这条验收在真机上从来没有发生过：

    2026-09-24 真机实测：分析进行中强杀 Worker，Run 停在原状态；
    超过心跳阈值（300s）十分钟后复查，仍然没有任何东西回收它。

周期扫描是这条验收成立的**唯一**机制，所以它必须挂在 beat 上，
而不是等某个人想起来手动调一次。
"""

from __future__ import annotations

from typing import Any
from sqlalchemy.orm import Session

from app.analysis.heartbeat import DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
from app.celery_app import celery_app
from app.db import SessionLocal
from app.repositories.agent_run import AgentRunRepository
from app.utils.observability import get_logger

logger = get_logger(__name__)


@celery_app.task(name="app.tasks.maintenance.reap_zombie_runs")
def reap_zombie_runs(*, timeout_seconds: int | None = None) -> dict[str, Any]:
    """把心跳超时、仍停在 `running` 的 Run 收成 `timeout`（计划第 705–707 行）。

    **跨项目扫描**（不传 `project_id`）：这是唯一允许这么做的业务场景 ——
    回收是运维动作，按 project 切分反而会漏掉"项目被删/改"的历史 Run。
    `AgentRunRepository.find_zombie_runs` 的注释也明写它是"为运维/Beat 任务准备的"。

    返回回收清单，并且**每次都记一条日志**：没有日志的周期任务，出问题时
    没人分得清"没触发"还是"触发了但没找到僵尸"。
    """
    timeout = int(timeout_seconds or DEFAULT_HEARTBEAT_TIMEOUT_SECONDS)
    session = SessionLocal()
    try:
        runs = AgentRunRepository(session)
        reclaimed = runs.reclaim_zombies(timeout_seconds=timeout)
        # beat 任务的写入必须自己提交：没人替它管会话。
        session.commit()
        uploads_reclaimed = reclaim_uploads(session)
        session.commit()
        logger.info(
            "zombie_runs_reclaimed",
            count=len(reclaimed),
            run_ids=reclaimed,
            timeout_seconds=timeout,
        )
        return {"reclaimed": len(reclaimed), "run_ids": reclaimed, "timeout_seconds": timeout,
                "uploads_reclaimed": uploads_reclaimed}
    except Exception as exc:  # noqa: BLE001 - 周期任务失败不能让 beat 挂掉
        session.rollback()
        logger.error("zombie_reap_failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        session.close()


def reclaim_uploads(session: Session) -> int:
    from datetime import datetime, timezone
    from sqlalchemy import select
    from app.models.datasource import DataSource
    from app.repositories.upload_job import UploadJobRepository
    from app.config import get_settings
    settings = get_settings()
    now = datetime.now(timezone.utc)
    repo = UploadJobRepository(session)
    count = 0
    for source in session.scalars(select(DataSource).where(DataSource.meta.op("?")("upload_jobs"))):
        for job in list((source.meta or {}).get("upload_jobs", {}).values()):
            if job["status"] not in {"queued", "running"}:
                continue
            start = job.get("started_at") or job["created_at"]
            timeout = settings.upload_queue_timeout_seconds if job["status"] == "queued" else settings.worker_task_timeout_seconds + 60
            if (now - datetime.fromisoformat(start)).total_seconds() > timeout:
                result = repo.update(source.project_id, source.id, job["job_id"],
                    {"status": "failed", "error": "上传排队或执行超时，请重试", "finished_at": now.isoformat()},
                    expected={job["status"]})
                count += int(result["status"] == "failed")
    return count
