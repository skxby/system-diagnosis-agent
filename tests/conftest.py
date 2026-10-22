"""测试默认使用独立临时 PostgreSQL 数据库，结束后删除，不污染开发证据。"""
from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path

import pytest


def pytest_configure(config):
    os.environ["ALLOW_REGISTRATION"] = "true"  # 多账号隔离测试显式开启注册
    os.environ["SECRET_KEY"] = secrets.token_urlsafe(48)
    args = [str(arg).replace("\\", "/") for arg in config.args]
    only_unit = all("tests/unit" in arg for arg in args)
    if only_unit or config.option.collectonly or "not integration" in str(config.option.markexpr):
        return
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from app.config import get_settings
    settings = get_settings()
    original_url = make_url(settings.database_url)
    admin = create_engine(original_url.set(database="postgres"),
                          isolation_level="AUTOCOMMIT", connect_args={"connect_timeout": 3})
    database = "logagent_test_" + secrets.token_hex(6)
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
    except Exception as exc:
        admin.dispose()
        raise pytest.UsageError(f"测试 PostgreSQL 不可用：{type(exc).__name__}；先启动 postgres/redis") from exc
    config._audit_test_database = (admin, database)
    config._audit_data_dir = tempfile.TemporaryDirectory(prefix="logagent-tests-")
    os.environ["DATA_DIR"] = config._audit_data_dir.name
    os.environ["UPLOAD_QUEUE"] = "test-uploads-" + secrets.token_hex(8)
    os.environ["DATABASE_URL"] = original_url.set(database=database).render_as_string(hide_password=False)
    get_settings.cache_clear()
    from alembic.config import Config
    from alembic import command
    try:
        command.upgrade(Config(str(Path(__file__).resolve().parents[1] / "alembic.ini")), "head")
    except BaseException:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        admin.dispose()
        del config._audit_test_database
        raise


def pytest_sessionfinish(session, exitstatus):
    resource = getattr(session.config, "_audit_test_database", None)
    if resource is not None:
        from sqlalchemy import text
        from app.db import engine
        engine.dispose()
        admin, database = resource
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        admin.dispose()
        session.config._audit_data_dir.cleanup()


@pytest.fixture(scope="session", autouse=True)
def real_upload_worker(request):
    if not hasattr(request.config, "_audit_test_database"):
        yield
        return
    import subprocess
    import sys
    import time
    from app.config import get_settings
    from app.worker_supervisor import healthy, terminate_tree
    settings = get_settings()
    log_path = Path(settings.data_dir) / "upload-worker.log"
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with log_path.open("w", encoding="utf-8") as output:
        process = subprocess.Popen([sys.executable, "-m", "app.worker_supervisor", "--role", "uploads",
            "--queues", settings.upload_queue], stdout=output, stderr=subprocess.STDOUT, **flags)
        try:
            deadline = time.monotonic() + 40
            while not healthy("uploads"):
                if process.poll() is not None or time.monotonic() > deadline:
                    pytest.fail("真实上传 Worker 未就绪：" + log_path.read_text(encoding="utf-8")[-2500:])
                time.sleep(.2)
            yield
        finally:
            terminate_tree(process.pid)
            process.wait(timeout=10)
