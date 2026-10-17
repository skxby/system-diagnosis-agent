"""候选知识写入 staging（计划第 751、477–486 行）。

```text
候选必须绑定真实 event_id（子集校验，不允许凭空总结）
→ 写入 staging/candidates.yaml，报告页「待确认知识」区可见
→ 人工审核：确认 / 编辑 / 丢弃
→ 确认后移入 confirmed/，下次分析自动加载
```

**写入位置是数据卷，不是代码目录**（修订说明第 2 条，最后一句明写
「不往代码目录写运行时数据」这条不能破）：

    ${DATA_DIR}/projects/<project_id>/knowledge/<domain>/staging/candidates.yaml

同一 Run 的候选按 `id` 累积去重：重复分析同一份日志不该产生一堆重复候选。
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from app.analysis.evidence import normalize_evidence_ids
from app.utils.atomic_files import atomic_write, file_lock, project_data_dir


class KnowledgeStagingError(ValueError):
    """候选不合法 —— 拒绝写入而不是让脏候选进 staging。"""


def candidate_id(domain_id: str, candidate: dict[str, Any]) -> str:
    """给候选算一个稳定 id。

    用 title 的哈希而不是随机 id：同一份异常重复分析时能得到同一个 id，
    从而在 staging 里**去重**而不是越积越多。
    """
    key = f"{domain_id}\0{candidate.get('title', '')}"
    return f"cand_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"


def staging_path(data_dir: str | Path, domain_id: str, project_id: int | None = None) -> Path:
    """staging 文件路径（数据卷内）。"""
    base = project_data_dir(data_dir, project_id) if project_id is not None else Path(data_dir)
    return base / "knowledge" / domain_id / "staging" / "candidates.yaml"


def load_candidates(path: Path) -> list[dict[str, Any]]:
    """读取已有候选；文件不存在或为空返回空列表。"""
    if not path.is_file():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise KnowledgeStagingError(f"{path.name}: 顶层必须是列表")
    return [c for c in raw if isinstance(c, dict)]


def write_candidates(
    *, data_dir: str | Path, domain_id: str, candidates: list[dict[str, Any]],
    run_id: int | None = None, project_id: int | None = None,
    valid_event_ids: set[str] | list[str] | None = None,
) -> dict[str, Any]:
    path = staging_path(data_dir, domain_id, project_id)
    with file_lock(path):
        return _write_candidates(data_dir=data_dir, domain_id=domain_id,
                                 candidates=candidates, run_id=run_id,
                                 project_id=project_id, valid_event_ids=valid_event_ids)


def _write_candidates(
    *,
    data_dir: str | Path,
    domain_id: str,
    candidates: list[dict[str, Any]],
    run_id: int | None = None,
    project_id: int | None = None,
    valid_event_ids: set[str] | list[str] | None = None,
) -> dict[str, Any]:
    """把候选写入 staging，按 id 去重后累积。

    返回统计：`{"written": n, "skipped": m, "total": k, "path": ...}`
    """
    path = staging_path(data_dir, domain_id, project_id)
    existing = load_candidates(path)
    by_id: dict[str, dict[str, Any]] = {}
    for item in existing:
        cid = str(item.get("id") or candidate_id(domain_id, item))
        by_id[cid] = {**item, "id": cid}

    valid = {str(v) for v in valid_event_ids} if valid_event_ids is not None else None
    written = 0
    skipped = 0

    for candidate in candidates:
        evidence = normalize_evidence_ids(candidate.get("evidence_ids"))
        if valid is not None:
            evidence = [e for e in evidence if e in valid]
        # 计划第 482 行：不允许凭空总结 —— 没有有效证据的候选不写
        if not evidence:
            skipped += 1
            continue

        cid = candidate_id(domain_id, candidate)
        if cid in by_id:
            skipped += 1  # 已有同 id 候选，去重
            continue

        by_id[cid] = {
            "id": cid,
            "kind": str(candidate.get("kind", "error_pattern")),
            "title": str(candidate.get("title", "")),
            "description": str(candidate.get("description", "")),
            "match": candidate.get("match") or {},
            "message_pattern": candidate.get("message_pattern"),
            "severity_hint": candidate.get("severity_hint"),
            "evidence": {
                "run_id": str(run_id) if run_id is not None else "",
                "event_ids": evidence,
            },
            "confidence": float(candidate.get("confidence") or 0.0),
            # 计划第 493 行：候选一律 draft，人工确认前不参与自动结论
            "status": "draft",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        written += 1

    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [by_id[key] for key in sorted(by_id)]
    atomic_write(path, yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))

    return {
        "written": written,
        "skipped": skipped,
        "total": len(payload),
        "path": str(path),
    }
