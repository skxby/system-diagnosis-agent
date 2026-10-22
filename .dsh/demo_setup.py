"""准备一个**可以立刻点开看**的演示环境，并打印出链接。

交付物是一个本地 Web 应用（不是桌面程序）：想"体验一下"就得先有账号、项目、
已上传的日志、一次跑完的分析 —— 手工点一遍要几分钟，环境重建还要再来一次。
这个脚本把这条路径固定下来，跑完直接把可点的 URL 打出来。

    账号：demo@evidencelog.local / evidencelog-demo-2026   （仅本机演示用）
    项目：演示项目：真实系统日志
    数据：logs/ 下的真实语料（默认 Mac_2k.log，loghub 公开数据集，2000 行）

用法：
    python .dsh/demo_setup.py                          # 默认 L2 档，产生一次真实模型调用
    python .dsh/demo_setup.py --log logs/Linux_2k.log  # 换语料
    python .dsh/demo_setup.py --no-run                 # 只准备账号/项目/上传，零费用

费用：一次分析 = 一次真实模型调用（约 ¥0.01–0.03，见 Run 的成本栏）；
      `--no-run` 与页面上的其它只读操作都不产生费用。
环境变量：`EVIDENCE_BASE`（默认 http://127.0.0.1:8000，即容器栈）。
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
from upload_polling import wait_upload
import os
import pathlib
import secrets
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = os.environ.get("EVIDENCE_BASE", "http://127.0.0.1:8000")

DEMO_EMAIL = "demo@evidencelog.local"
DEMO_PASSWORD = "evidencelog-demo-2026"
PROJECT_NAME = "演示项目：真实系统日志"

TERMINAL = {"completed", "partial_success", "failed", "timeout", "cancelled"}

OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
)


def call(
    method: str, path: str, body=None, token: str | None = None, timeout: int = 300
) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with OPENER.open(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, (json.loads(raw) if raw else {})
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:300].decode("utf-8", "replace")}


def upload(project_id: int, path: pathlib.Path, fmt: str, token: str) -> tuple[int, dict]:
    """multipart 上传（与页面上的上传框走同一个端点）。"""
    boundary = "----demo" + secrets.token_hex(8)
    body = bytearray()
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{path.name}"\r\n'
    ).encode()
    body += b"Content-Type: text/plain\r\n\r\n"
    body += path.read_bytes()
    body += b"\r\n"
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="fmt"\r\n\r\n{fmt}\r\n'
    ).encode()
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{BASE}/api/projects/{project_id}/upload", data=bytes(body), method="POST"
    )
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with OPENER.open(req, timeout=600) as response:
            return wait_upload(BASE, project_id, token, response.status, json.loads(response.read() or b"{}"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:300].decode("utf-8", "replace")}


def token_for_demo() -> str:
    """已有账号就登录，没有就注册 —— 反复跑不会因为"邮箱已存在"失败。"""
    _, reg = call(
        "POST", "/api/register", {"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
    )
    token = reg.get("access_token") or reg.get("token")
    if token:
        print(f"① 已注册演示账号 {DEMO_EMAIL}")
        return str(token)
    status, login = call(
        "POST", "/api/login", {"email": DEMO_EMAIL, "password": DEMO_PASSWORD}
    )
    if not (login.get("access_token") or login.get("token")):
        raise SystemExit(f"演示账号登录失败（{status}）：{login}")
    print(f"① 演示账号已存在，直接登录 {DEMO_EMAIL}")
    return str(login.get("access_token") or login.get("token"))


def project_id_for(token: str) -> tuple[int, bool]:
    _, projects = call("GET", "/api/projects", token=token)
    for project in projects or []:
        if project.get("name") == PROJECT_NAME:
            return int(project["id"]), False
    status, created = call(
        "POST", "/api/projects", {"name": PROJECT_NAME, "budget_total": 10.0}, token
    )
    if status not in (200, 201) or "id" not in created:
        raise SystemExit(f"建项目失败（{status}）：{created}")
    return int(created["id"]), True


def poll(project_id: int, run_id: int, token: str, *, timeout: int = 900) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        _, run = call("GET", f"/api/runs/{run_id}?project_id={project_id}", token=token)
        status = str(run.get("status") or "")
        if status != last:
            print(f"   状态：{status or '（读不到）'}", flush=True)
            last = status
        if status in TERMINAL:
            return run
        time.sleep(3)
    return {"status": "（等待超时，可刷新页面看）"}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", default="logs/Mac_2k.log")
    parser.add_argument("--fmt", default="txt", choices=["txt", "jsonl"])
    parser.add_argument("--tier", default="L2", choices=["L1", "L2", "L3"])
    parser.add_argument("--no-run", action="store_true", help="不发起分析（零费用）")
    args = parser.parse_args(argv)

    source = ROOT / args.log
    if not source.exists():
        raise SystemExit(f"找不到语料 {source}")
    print(f"目标栈：{BASE}")

    token = token_for_demo()
    pid, created = project_id_for(token)
    print(f"② 项目 {'新建' if created else '复用'}：{PROJECT_NAME}（id={pid}）")

    status, up = upload(pid, source, args.fmt, token)
    if status != 200:
        raise SystemExit(f"上传失败（{status}）：{up}")
    parse = up.get("parse") or {}
    mask = up.get("mask") or {}
    print(
        f"③ 已上传 {source.name}：入库 {up.get('events_persisted')} 条事件、"
        f"解析 {parse.get('parsed')} 行、坏行 {parse.get('bad_lines')}、"
        f"脱敏替换 {mask.get('total') if isinstance(mask, dict) else mask} 处，"
        f"data_source_id={up.get('data_source_id')}"
    )

    run_id = None
    if not args.no_run:
        status, created_run = call(
            "POST",
            f"/api/projects/{pid}/analysis-runs",
            {"data_source_id": up.get("data_source_id"), "start_tier": args.tier},
            token,
        )
        run_id = created_run.get("run_id")
        if status != 202 or not run_id:
            raise SystemExit(f"发起分析失败（{status}）：{created_run}")
        print(f"④ 已发起分析 run_id={run_id}（{args.tier} 档，真实模型调用）")
        run = poll(pid, run_id, token)
        print(
            f"   收尾状态 {run.get('status')}｜成本 ¥{run.get('cost_actual')}｜"
            f"token {int(run.get('tokens_input') or 0)}+{int(run.get('tokens_output') or 0)}"
        )

    print("\n=== 直接点开这些链接 ===")
    print(f"登录页        {BASE}/login      （{DEMO_EMAIL} / {DEMO_PASSWORD}）")
    print(f"项目页        {BASE}/projects/{pid}")
    if run_id:
        print(f"Run 详情      {BASE}/runs/{run_id}/detail?project_id={pid}")
        print(f"报告页        {BASE}/report/{run_id}?project_id={pid}")
    print(f"知识库        {BASE}/knowledge/{pid}")
    print(f"接口文档      {BASE}/docs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
