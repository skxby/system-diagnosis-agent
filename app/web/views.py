"""页面路由（Jinja2 渲染）。

**服务端渲染只做"壳"**：真正的数据由页面上的原生 JS 调 `/api/*` 拿。
理由：报告页要轮询 Run 状态、展开证据、确认知识，这些都是交互行为；
把它们全做成服务端渲染反而要写更多样板，且与"最简前端"的定位不符。

鉴权：页面本身不校验（未登录时 JS 调 API 会拿到 401，页面跳转到 /login）。
真正把门的是 API 层 —— 页面不是安全边界，这一点必须诚实。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.web import get_templates
from app.web import STATIC_DIR
import hashlib

router = APIRouter()
templates = get_templates()


def _render(request: Request, template: str, **context: object) -> HTMLResponse:
    context["asset_version"] = hashlib.sha256(b"".join(
        (STATIC_DIR / name).read_bytes() for name in ("app.js", "app.css")
    )).hexdigest()[:12]
    return templates.TemplateResponse(request, template, context)


@router.get("/", include_in_schema=False)
def index(request: Request) -> RedirectResponse:
    return RedirectResponse(url="/projects")


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request) -> HTMLResponse:
    return _render(request, "login.html")


@router.get("/projects", response_class=HTMLResponse, include_in_schema=False)
def projects_page(request: Request) -> HTMLResponse:
    return _render(request, "projects.html")


@router.get(
    "/projects/{project_id}", response_class=HTMLResponse, include_in_schema=False
)
def project_detail_page(request: Request, project_id: int) -> HTMLResponse:
    return _render(request, "project_detail.html", project_id=project_id)


@router.get("/runs/{run_id}", response_class=HTMLResponse, include_in_schema=False)
def run_status_page(request: Request, run_id: int) -> HTMLResponse:
    # project_id 是必须的：Run 的 API（/api/runs/{id}）按 Project 作用域鉴权。
    # 没有它页面拿不到数据，所以这里把它作为模板变量透传下去。
    return _render(
        request, "run_status.html", run_id=run_id,
        project_id=request.query_params.get("project_id", ""),
    )


@router.get(
    "/runs/{run_id}/detail", response_class=HTMLResponse, include_in_schema=False
)
def run_detail_page(request: Request, run_id: int) -> HTMLResponse:
    """Run 详情视图（计划第 938 行：V1 做一个简单的 Run 列表/详情视图即可）。"""
    return _render(
        request, "run_detail.html", run_id=run_id,
        project_id=request.query_params.get("project_id", ""),
    )


@router.get("/report/{run_id}", response_class=HTMLResponse, include_in_schema=False)
def report_page(request: Request, run_id: int) -> HTMLResponse:
    return _render(
        request, "report.html", run_id=run_id,
        project_id=request.query_params.get("project_id", ""),
    )


@router.get(
    "/knowledge/{project_id}", response_class=HTMLResponse, include_in_schema=False
)
def knowledge_page(request: Request, project_id: int) -> HTMLResponse:
    """待确认知识区（读 staging，计划第 913 行）。"""
    return _render(request, "knowledge.html", project_id=project_id)
