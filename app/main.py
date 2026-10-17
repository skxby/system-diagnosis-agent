"""FastAPI 入口。

阶段 10 起：业务端点走 `app/api/` 下的路由模块；API 只做验证、鉴权、
创建任务、返回状态，**不承担分析逻辑**（计划第 856 行）。
"""

from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.responses import JSONResponse
from redis import Redis

from app.api.middleware import TraceIdMiddleware, RequestSafetyMiddleware
from app.api.routes_auth_projects import router as auth_projects_router
from app.api.routes_knowledge import router as knowledge_router
from app.api.routes_runs import router as runs_router
from app.api.routes_catalog import router as catalog_router
from app.config import get_settings
from app.db import engine

settings = get_settings()


@asynccontextmanager
async def lifespan(application):
    secret = get_settings().secret_key.strip()
    if len(secret) < 32 or secret in {"change-me", "changethis"}:
        raise RuntimeError("SECRET_KEY 必须是至少 32 字符的随机密钥；请运行 scripts/init-env.ps1")
    yield

app = FastAPI(
    lifespan=lifespan,
    title="Log Intelligence Agent",
    version="0.1.0",
    description="个人全栈、单领域、只读的日志分析 Agent Runtime（V1）",
)

# 结构化日志与 trace_id（阶段 12）：必须在最前面配置，
# 否则模块级 logger 会在配置前就被创建（structlog 缓存首次使用的 logger）
from app.utils.observability import configure_logging

configure_logging(level=settings.log_level)
app.add_middleware(TraceIdMiddleware)
app.add_middleware(RequestSafetyMiddleware)

# 原文只能存在内存：请求体上限小于 spool 阈值，解析器不会将上传写入临时盘。
from starlette.formparsers import MultiPartParser
from app.services.upload_service import MAX_UPLOAD_BYTES
MultiPartParser.spool_max_size = MAX_UPLOAD_BYTES + 128 * 1024

app.include_router(auth_projects_router)
app.include_router(runs_router)
app.include_router(knowledge_router)
app.include_router(catalog_router)

# 前端页面（Jinja2 模板 + 原生 JS，无构建工具）
from app.web import mount_static
from app.web.views import router as web_router

app.include_router(web_router)
mount_static(app)


@app.get("/healthz")
def healthz() -> dict:
    """存活检查：进程在不在。故意不碰外部依赖，避免依赖抖动引起误判重启。"""
    return {"status": "ok", "environment": settings.environment}


@app.get("/healthz/deps")
def healthz_deps() -> JSONResponse:
    """就绪检查：PostgreSQL 与 Redis 是否真的可用。"""
    checks: dict[str, dict] = {}

    try:
        with engine.connect():
            checks["postgres"] = {
                "ok": True,
            }
    except Exception:  # noqa: BLE001 — 就绪探针必须捕获一切并如实上报
        checks["postgres"] = {"ok": False}

    client = None
    try:
        client = Redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
        client.ping()
        checks["redis"] = {"ok": True}
    except Exception:  # noqa: BLE001 — 就绪探针必须捕获一切并如实上报
        checks["redis"] = {"ok": False}
    finally:
        if client is not None:
            client.close()

    healthy = all(item["ok"] for item in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
