"""测试默认使用独立临时 PostgreSQL 数据库，结束后删除，不污染开发证据。"""
from __future__ import annotations

import os
import secrets
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
