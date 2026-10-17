"""知识审核端点（计划第 874–881 行，对应修订说明第 1 条的 6 个端点）。

这是 8.1 知识闭环的**运行时入口**：候选在 staging → 人工确认 → 移入 confirmed
→ 下次分析自动加载。修订说明第 1 条明确：不让前端直接读写 YAML，否则文件格式
会耦进 UI，且无法做 evidence 校验。

**写入位置是数据卷**（`${DATA_DIR}/projects/<project_id>/knowledge/<domain>/...`），
绝不写代码目录（修订说明第 2 条最后一句）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, status

from app.analysis.knowledge_staging import (
    KnowledgeStagingError,
    load_candidates,
    staging_path,
)
from app.api.deps import ProjectScopeDep
from app.api.schemas import (
    HTTP_422,
    EditCandidateRequest,
    KnowledgeActionResponse,
    KnowledgeCandidateResponse,
)
from app.api.scoping import get_owned_project_id
from app.config import get_settings

router = APIRouter()

from app.utils.atomic_files import atomic_write, file_lock, project_data_dir


def _domain_id() -> str:
    return get_settings().default_domain_id

#: 要求调用方以 query 参数给出 project_id，并校验归属当前用户
def _locked_project(project_id: Annotated[int, Depends(get_owned_project_id)]):
    with file_lock(_staging_file(project_id)):
        yield project_id


OwnedProjectId = Annotated[int, Depends(_locked_project)]


def _data_dir() -> Path:
    return Path(get_settings().data_dir)


def _confirmed_dir(project_id: int) -> Path:
    return project_data_dir(_data_dir(), project_id) / "knowledge" / _domain_id() / "confirmed"


def _staging_file(project_id: int) -> Path:
    return staging_path(_data_dir(), _domain_id(), project_id)


def _read_staging(project_id: int) -> list[dict[str, Any]]:
    try:
        return load_candidates(_staging_file(project_id))
    except KnowledgeStagingError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"staging 文件格式不合法：{exc}",
        ) from exc


def _write_staging(project_id: int, candidates: list[dict[str, Any]]) -> None:
    path = _staging_file(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, yaml.safe_dump(candidates, allow_unicode=True, sort_keys=False))


def _find(project_id: int, candidate_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    candidates = _read_staging(project_id)
    for item in candidates:
        if str(item.get("id")) == candidate_id:
            return candidates, item
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail=f"候选 {candidate_id} 不存在"
    )


def _assert_confirmable(item: dict[str, Any]) -> None:
    """计划第 494 行：无 evidence、无 confidence 的条目**不允许确认**。

    在 API 层再挡一次：前端可以绕过，但服务端不能。
    """
    evidence = item.get("evidence") or {}
    event_ids = evidence.get("event_ids") or []
    run_id = evidence.get("run_id")
    if not event_ids and not run_id:
        raise HTTPException(
            status_code=HTTP_422,
            detail="该候选没有 evidence（run_id / event_ids），不允许确认为 confirmed",
        )
    if float(item.get("confidence") or 0.0) <= 0.0:
        raise HTTPException(
            status_code=HTTP_422,
            detail="该候选 confidence 为 0，不允许确认为 confirmed",
        )


def _move_to_confirmed(project_id: int, item: dict[str, Any], *, kind_override: str | None = None) -> None:
    """把条目从 staging 移入 confirmed（修订说明第 2 条的两层结构）。"""
    confirmed_dir = _confirmed_dir(project_id)
    confirmed_dir.mkdir(parents=True, exist_ok=True)
    target = confirmed_dir / "runtime_confirmed.yaml"

    existing: list[dict[str, Any]] = []
    if target.is_file():
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            existing = [c for c in raw if isinstance(c, dict)]

    entry = dict(item)
    entry.pop("id", None)
    entry["id"] = str(item.get("id"))
    if kind_override:
        entry["kind"] = kind_override
    entry["status"] = "confirmed"
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()

    kept = [c for c in existing if str(c.get("id")) != entry["id"]]
    kept.append(entry)
    atomic_write(target, yaml.safe_dump(kept, allow_unicode=True, sort_keys=False))


def _remove_from_staging(project_id: int, candidate_id: str) -> None:
    candidates = _read_staging(project_id)
    kept = [c for c in candidates if str(c.get("id")) != candidate_id]
    _write_staging(project_id, kept)


def _archive_candidate(project_id: int, item: dict[str, Any], bucket: str) -> Path:
    """把被丢弃/标误报的候选挪到归档目录，便于事后复查。"""
    archive_dir = project_data_dir(_data_dir(), project_id) / "knowledge" / _domain_id() / bucket
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{item.get('id')}.yaml"
    atomic_write(path, yaml.safe_dump(item, allow_unicode=True, sort_keys=False))
    return path


# ============================================================
# 端点
# ============================================================


@router.get(
    "/api/projects/{project_id}/knowledge/candidates",
    response_model=list[KnowledgeCandidateResponse],
    tags=["knowledge"],
)
def list_candidates(scope: ProjectScopeDep) -> list[KnowledgeCandidateResponse]:
    """列出 staging 候选（阶段 11「待确认知识」区的数据来源）。"""
    return [
        KnowledgeCandidateResponse(
            id=str(c.get("id", "")),
            kind=str(c.get("kind", "error_pattern")),
            title=str(c.get("title", "")),
            description=str(c.get("description", "")),
            match=c.get("match") or {},
            evidence=c.get("evidence") or {},
            confidence=float(c.get("confidence") or 0.0),
            status=str(c.get("status", "draft")),
        )
        for c in _read_staging(scope.project_id)
    ]


@router.get(
    "/api/projects/{project_id}/knowledge/confirmed",
    response_model=list[KnowledgeCandidateResponse],
    tags=["knowledge"],
)
def list_confirmed(scope: ProjectScopeDep) -> list[KnowledgeCandidateResponse]:
    """列出已生效知识（含 seed 与运行时两部分）。"""
    out: list[KnowledgeCandidateResponse] = []
    for directory in (_seed_dir(), _confirmed_dir(scope.project_id)):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.y*ml")):
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            entries = raw if isinstance(raw, list) else [raw]
            for c in entries:
                if not isinstance(c, dict):
                    continue
                out.append(
                    KnowledgeCandidateResponse(
                        id=str(c.get("id", "")),
                        kind=str(c.get("kind", "error_pattern")),
                        title=str(c.get("title", "")),
                        description=str(c.get("description", "")),
                        match=c.get("match") or {},
                        evidence=c.get("evidence") or {},
                        confidence=float(c.get("confidence") or 0.0),
                        status=str(c.get("status", "confirmed")),
                    )
                )
    return out


@router.post(
    "/api/knowledge/candidates/{candidate_id}/confirm",
    response_model=KnowledgeActionResponse,
    tags=["knowledge"],
)
def confirm_candidate(
    candidate_id: str, project_id: OwnedProjectId
) -> KnowledgeActionResponse:
    """确认候选 → 移入 confirmed（下次分析生效）。"""
    _, item = _find(project_id, candidate_id)
    _assert_confirmable(item)
    _move_to_confirmed(project_id, item)
    _remove_from_staging(project_id, candidate_id)
    return KnowledgeActionResponse(
        candidate_id=candidate_id, action="confirm", status="confirmed"
    )


@router.patch(
    "/api/knowledge/candidates/{candidate_id}",
    response_model=KnowledgeCandidateResponse,
    tags=["knowledge"],
)
def edit_candidate(
    candidate_id: str, payload: EditCandidateRequest, project_id: OwnedProjectId
) -> KnowledgeCandidateResponse:
    """编辑候选（编辑后再确认）。只允许改白名单字段。"""
    candidates, item = _find(project_id, candidate_id)
    updates = payload.model_dump(exclude_none=True)
    if not updates:
        raise HTTPException(
            status_code=HTTP_422, detail="没有可更新的字段"
        )
    item.update(updates)
    item["updated_at"] = datetime.now(timezone.utc).isoformat()
    _write_staging(project_id, candidates)

    return KnowledgeCandidateResponse(
        id=str(item.get("id", "")),
        kind=str(item.get("kind", "error_pattern")),
        title=str(item.get("title", "")),
        description=str(item.get("description", "")),
        match=item.get("match") or {},
        evidence=item.get("evidence") or {},
        confidence=float(item.get("confidence") or 0.0),
        status=str(item.get("status", "draft")),
    )


@router.post(
    "/api/knowledge/candidates/{candidate_id}/reject",
    response_model=KnowledgeActionResponse,
    tags=["knowledge"],
)
def reject_candidate(
    candidate_id: str, project_id: OwnedProjectId
) -> KnowledgeActionResponse:
    """丢弃候选：移出 staging 并归档，便于事后复查。"""
    _, item = _find(project_id, candidate_id)
    _archive_candidate(project_id, item, "rejected")
    _remove_from_staging(project_id, candidate_id)
    return KnowledgeActionResponse(candidate_id=candidate_id, action="reject", status="rejected")


@router.post(
    "/api/knowledge/candidates/{candidate_id}/false-positive",
    response_model=KnowledgeActionResponse,
    tags=["knowledge"],
)
def mark_false_positive(
    candidate_id: str, project_id: OwnedProjectId
) -> KnowledgeActionResponse:
    """标记误报 → 进 confirmed/false_positives（下次分析会抑制同类异常）。

    计划第 486 行：false_positive 命中则抑制该误报。
    """
    _, item = _find(project_id, candidate_id)
    _archive_candidate(project_id, item, "false_positives")
    entry = dict(item)
    entry["kind"] = "false_positive"
    entry["status"] = "confirmed"
    _move_to_confirmed(project_id, entry, kind_override="false_positive")
    _remove_from_staging(project_id, candidate_id)
    return KnowledgeActionResponse(
        candidate_id=candidate_id, action="false-positive", status="confirmed"
    )


def _seed_dir() -> Path:
    """领域目录包里的 seed 知识（代码资产，只读）。"""
    from app.domains.registry import get_domain_registry
    return Path(get_domain_registry().load(_domain_id()).knowledge_dir()) / "knowledge" / "confirmed"
