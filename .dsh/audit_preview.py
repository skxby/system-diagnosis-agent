"""可重复的网页验收环境：临时 PG/数据目录、真实 Celery、禁止外部模型流量。

运行 python .dsh/audit_preview.py，访问 http://127.0.0.1:8002。
Ctrl+C 后停止子进程并删除本次临时数据库。不会修改开发库或 .env。
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> None:
    from app.config import get_settings
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    original = make_url(get_settings().database_url)
    database = "logagent_preview_" + secrets.token_hex(6)
    admin = create_engine(original.set(database="postgres"), isolation_level="AUTOCOMMIT")
    children = []
    with tempfile.TemporaryDirectory(prefix="logagent-audit-") as data_dir:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        try:
            os.environ.update(DATABASE_URL=original.set(database=database).render_as_string(hide_password=False),
                SECRET_KEY=secrets.token_urlsafe(48), ALLOW_REGISTRATION="true", DATA_DIR=data_dir,
                REDIS_URL="redis://127.0.0.1:6379/14", MODEL_PROVIDER_API_KEY="local-audit-disabled",
                MODEL_PROVIDER_BASE_URL="http://127.0.0.1:9", MODEL_L1="audit", MODEL_L2="audit", MODEL_L3="audit")
            get_settings.cache_clear()
            from alembic import command
            from alembic.config import Config
            command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
            flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
            for role, queue in (("analysis", "celery"), ("uploads", "uploads"), ("maintenance", "maintenance")):
                children.append(subprocess.Popen([sys.executable, "-m", "app.worker_supervisor",
                    "--role", role, "--queues", queue], cwd=ROOT, **flags))
            children.append(subprocess.Popen([sys.executable, "-m", "celery", "-A", "app.celery_app", "beat",
                "--scheduler", "app.beat_scheduler:HealthScheduler", "--max-interval", "5",
                "--schedule", str(Path(data_dir) / "celerybeat-schedule")], cwd=ROOT, **flags))
            web = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", "8002"], cwd=ROOT, **flags)
            children.append(web)
            print("Audit preview: http://127.0.0.1:8002 (isolated, no external model calls)", flush=True)
            web.wait()
        except KeyboardInterrupt:
            pass
        finally:
            for child in reversed(children):
                if child.poll() is None and os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
                elif child.poll() is None:
                    child.terminate()
                child.wait(timeout=15)
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
            admin.dispose()


if __name__ == "__main__":
    main()
