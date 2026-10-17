"""trace_id 中间件（计划第 928 行：一次请求一个 trace_id，贯穿日志与任务）。

行为：
- 请求带 `X-Trace-Id` 就用它（便于从网关/前端串联），否则新生成一个；
- 把 trace_id 绑进上下文，使本次请求内所有结构化日志自动带上；
- **响应里回传 `X-Trace-Id`**，这样用户遇到问题时能把 id 报给你去 grep 日志。

刻意不把 trace_id 写进数据库：`AgentRun` 的 trace_id 由创建 Run 的那次请求
决定并随 Run 一起持久化（见 routes_runs），中间件只负责"当前作用域"。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.responses import JSONResponse
from urllib.parse import urlsplit

from app.utils.observability import TRACE_ID_HEADER, bind_run_context, new_trace_id


class RequestSafetyMiddleware:
    """在 multipart 解析前限制原始上传体；拒绝跨站浏览器写请求。"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            origin = headers.get(b"origin")
            if (headers.get(b"sec-fetch-site") == b"cross-site" or
                (origin and urlsplit(origin.decode()).netloc != headers.get(b"host", b"").decode())):
                return await JSONResponse({"detail": "拒绝跨站写请求"}, status_code=403)(scope, receive, send)
        if scope["path"].endswith("/upload"):
            from app.services.upload_service import MAX_UPLOAD_BYTES
            from starlette.exceptions import HTTPException
            limit = MAX_UPLOAD_BYTES + 64 * 1024  # 有限的 multipart 头部余量
            try:
                length = int(headers.get(b"content-length", b"0"))
            except ValueError:
                return await JSONResponse({"detail": "无效 Content-Length"}, status_code=400)(scope, receive, send)
            if length > limit:
                return await JSONResponse({"detail": "上传大小超过 10 MiB"}, status_code=413)(scope, receive, send)
            used = 0
            original_receive = receive

            async def limited_receive():
                nonlocal used
                message = await original_receive()
                used += len(message.get("body", b""))
                if used > limit:
                    raise HTTPException(413, "上传大小超过 10 MiB")
                return message
            receive = limited_receive
        await self.app(scope, receive, send)


class TraceIdMiddleware(BaseHTTPMiddleware):
    """给每个请求绑定 trace_id，并在响应头回传。"""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(TRACE_ID_HEADER)
        trace_id = incoming.strip() if incoming and incoming.strip() else new_trace_id()

        with bind_run_context(trace_id=trace_id):
            response = await call_next(request)
            response.headers[TRACE_ID_HEADER] = trace_id
            return response
