"""运行时文件：跨进程互斥与同目录原子替换。"""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from filelock import FileLock


def file_lock(path: Path) -> FileLock:
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileLock(str(path) + ".lock", timeout=15, thread_local=False)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def project_data_dir(data_dir: str | Path, project_id: int) -> Path:
    if project_id <= 0:
        raise ValueError("project_id 必须为正整数")
    return Path(data_dir) / "projects" / str(project_id)
