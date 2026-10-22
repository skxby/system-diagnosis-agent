"""零模型费用的 Linux 容器实测：临时 PG/空 Redis DB、四角色、异步上传与进程冻结。"""
from pathlib import Path
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import httpx
from redis import Redis
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import get_settings


def docker(*args):
    return subprocess.check_output(["docker", "--context", "desktop-linux", *args], text=True).strip()


def wait_for(call, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if result := call():
                return result
        except httpx.TransportError:
            pass
        time.sleep(.25)
    raise RuntimeError("容器真机检查超时")


def main():
    settings = get_settings()
    original = make_url(settings.database_url)
    admin = create_engine(original.set(database="postgres"), isolation_level="AUTOCOMMIT")
    database = "logagent_container_" + secrets.token_hex(6)
    name = "logagent-runtime-" + secrets.token_hex(6)
    redis_url = urlsplit(settings.redis_url)
    selected = None
    owner = secrets.token_hex(16)
    for index in range(15, 0, -1):
        candidate = Redis.from_url(redis_url._replace(path=f"/{index}").geturl(), socket_timeout=3)
        if candidate.dbsize() == 0 and candidate.set("runtime-check-owner", owner, nx=True, ex=600):
            selected, redis_db = candidate, index
            break
        candidate.close()
    if selected is None:
        raise RuntimeError("没有空闲 Redis DB，未清理任何现有数据")
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database}"'))
    try:
        os.environ["DATABASE_URL"] = original.set(database=database).render_as_string(hide_password=False)
        get_settings.cache_clear()
        from alembic import command
        from alembic.config import Config
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        with tempfile.TemporaryDirectory(prefix="logagent-container-") as temporary:
            environment = {
                "DATABASE_URL": original.set(host="logagent-postgres", port=5432, database=database).render_as_string(hide_password=False),
                "REDIS_URL": redis_url._replace(netloc=(redis_url.netloc.rsplit('@', 1)[0] + '@' if '@' in redis_url.netloc else '') + "logagent-redis:6379", path=f"/{redis_db}").geturl(),
                "DATA_DIR": "/app/data", "SECRET_KEY": secrets.token_urlsafe(48), "ALLOW_REGISTRATION": "true",
                "UPLOAD_QUEUE": "uploads", "MODEL_PROVIDER_BASE_URL": "http://127.0.0.1:9",
                "MODEL_PROVIDER_API_KEY": "local-fixture", "MODEL_L1": "fixture", "MODEL_L2": "fixture", "MODEL_L3": "fixture",
                "WORKER_PROBE_INTERVAL_SECONDS": "1", "WORKER_PROBE_TIMEOUT_SECONDS": "2"}
            env = Path(temporary) / ".env"
            env.write_text("\n".join(f"{key}={value}" for key, value in environment.items()), encoding="utf-8")
            bootstrap = """
import subprocess,sys
from app.worker_supervisor import terminate_tree
import uvicorn
children=[]
try:
 for role,queue in [('analysis','celery'),('uploads','uploads'),('maintenance','maintenance')]:
  children.append(subprocess.Popen([sys.executable,'-m','app.worker_supervisor','--role',role,'--queues',queue]))
 children.append(subprocess.Popen([sys.executable,'-m','celery','-A','app.celery_app','beat','--scheduler','app.beat_scheduler:HealthScheduler','--max-interval','5','--schedule','/app/data/celerybeat-schedule']))
 uvicorn.run('app.main:app',host='0.0.0.0',port=8000)
finally:
 for child in children:
  if child.poll() is None: terminate_tree(child.pid)
"""
            docker("run", "-d", "--name", name, "--network", "logagent_default", "--env-file", str(env),
                   "-p", "127.0.0.1::8000", "logagent-app:local", "python", "-u", "-c", bootstrap)
        port = docker("port", name, "8000/tcp").rsplit(":", 1)[1]
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            wait_for(lambda: client.get("/healthz/workers").status_code == 200)
            health = client.get("/healthz/workers").json()
            client.post("/api/register", json={"email": "container@example.com", "password": "runtime-fixture-password"}).raise_for_status()
            login = client.post("/api/login", json={"email": "container@example.com", "password": "runtime-fixture-password"})
            login.raise_for_status()
            project = client.post("/api/projects", json={"name": "container runtime"}).json()["id"]
            response = client.post(f"/api/projects/{project}/upload", data={"fmt": "txt"},
                files={"file": ("normal.log", (ROOT / "tests/datasets/normal/system.log").read_bytes())})
            assert response.status_code == 202, response.text
            job = response.json()["job_id"]
            upload = wait_for(lambda: (value if (value := client.get(f"/api/projects/{project}/uploads/{job}").json())["status"] == "completed" else None))
            assert upload["events_persisted"] == 60
            run_id = client.post(f"/api/projects/{project}/analysis-runs", json={"data_source_id": upload["data_source_id"]}).json()["run_id"]
            run = wait_for(lambda: (value if (value := client.get(f"/api/runs/{run_id}").json())["status"] == "completed" else None))
            assert run["cost_actual"] == 0 and run["run_metadata"]["tier"] == "L0"
            freeze = "import psutil;from app.worker_supervisor import read_state,state_path;s=read_state(state_path('analysis').with_suffix('.lease.json'));print(s['pid']);psutil.Process(s['pid']).suspend()"
            frozen_pid = int(docker("exec", name, "python", "-c", freeze))
            read = "import json;from app.worker_supervisor import read_state,state_path;print(json.dumps(read_state(state_path('analysis'))))"
            restarted = wait_for(lambda: (value if (value := json.loads(docker("exec", name, "python", "-c", read))).get("restarts", 0) >= 1 else None))
            wait_for(lambda: client.get("/healthz/workers").status_code == 200)
            alive = docker("exec", name, "python", "-c", f"import psutil;print(psutil.pid_exists({frozen_pid}))")
            assert alive == "False" and restarted["last_failure"] == "worker_probe_timeout"
            evidence = {"health": health, "upload_http": 202, "events": 60, "analysis": "completed",
                        "model_cost": 0, "frozen_pid_dead": True, "restarts": restarted["restarts"],
                        "reason": restarted["last_failure"], "healthy_after_restart": True}
            output = ROOT / ".dsh/junit/container-runtime-evidence.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            print(json.dumps(evidence))
    finally:
        subprocess.run(["docker", "--context", "desktop-linux", "rm", "-f", name], capture_output=True)
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        admin.dispose()
        # 只清理由本次验证占用、启动前为空且所有权仍匹配的临时 Redis DB。
        if selected.get("runtime-check-owner") == owner.encode():
            selected.flushdb()
        selected.close()


if __name__ == "__main__":
    main()
