"""真机运行时证据采集：在**真跑起来的全栈**上走一遍，把"到底发生了什么"记下来。

这是「是否真机生效」那一列的第二层证据（第一层是 `wiring_graph.py` 的静态接线）：

  - 静态接线回答"从入口点能不能调到它"；
  - 本脚本回答"**这一次真实运行里，它到底有没有发生**" —— 以落库、落盘为准，
    不听代码自述。

四种模式（可分别跑，结果合并进同一份 `.dsh/runtime_evidence.json`）：

    python .dsh/real_path_evidence.py              # ① 两次真实分析（**会产生模型费用**）
    python .dsh/real_path_evidence.py --contract   # ② 接口契约：健康检查/鉴权/隔离/取消/重试/页面
    python .dsh/real_path_evidence.py --uploads    # ③ 只上传不分析（JSONL、负样本、指标、超限、复用）
    python .dsh/real_path_evidence.py --zombie-start / --zombie-check
                                                   # ④ 僵尸回收：起一个 Run 后杀掉 Worker 再看状态

BASE 默认 `http://127.0.0.1:8001`（带追踪的直跑栈）；对容器跑就设
`EVIDENCE_BASE=http://127.0.0.1:8000`。
"""

from __future__ import annotations

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
sys.path.insert(0, str(ROOT))

BASE = os.environ.get("EVIDENCE_BASE", "http://127.0.0.1:8001")
OUT = ROOT / ".dsh" / "runtime_evidence.json"
ZOMBIE = ROOT / ".dsh" / "zombie_test.json"
TMP = ROOT / ".pytest-tmp" / "evidence"

#: 真实日志里没有邮箱/密钥，而阶段 04 验收要证明"含邮箱/密钥的内容
#: 落盘与入库都已被替换"，故往真实语料尾部注入一条 canary 行。
CANARY_EMAIL = "canary.evidence@example.com"
CANARY_SECRET = "sk-canaryevidence0123456789abcdef"
CANARY_LINE = (
    "Sep 24 10:00:00 evidence-host notifyd[4242]: "
    f"uploader={CANARY_EMAIL} token={CANARY_SECRET}\n"
)

JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


# ============================================================
# 基础工具
# ============================================================


def call(method: str, path: str, body=None, token: str | None = None, timeout: int = 120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with OPENER.open(req, timeout=timeout) as response:
            raw = response.read()
            try:
                return response.status, (json.loads(raw) if raw else {})
            except json.JSONDecodeError:
                # /docs 之类返回 HTML，不是错误
                return response.status, {"_text_head": raw[:200].decode("utf-8", "replace")}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw[:200].decode("utf-8", "replace")}
        return exc.code, payload


def upload(project_id: int, path: pathlib.Path, fmt: str, token: str, *, source_id=None):
    boundary = "----evidence" + secrets.token_hex(8)
    body = bytearray()
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode()
    body += b"Content-Type: text/plain\r\n\r\n"
    body += path.read_bytes()
    body += b"\r\n"
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="fmt"\r\n\r\n{fmt}\r\n'.encode()
    if source_id is not None:
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="data_source_id"\r\n\r\n'
            f"{source_id}\r\n"
        ).encode()
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{BASE}/api/projects/{project_id}/upload", data=bytes(body), method="POST"
    )
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with OPENER.open(req, timeout=300) as response:
            return wait_upload(BASE, project_id, token, response.status, json.loads(response.read() or b"{}"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:300].decode("utf-8", "replace")}


def register(name: str) -> tuple[str, str]:
    email = f"evidence-{name}-{secrets.token_hex(4)}@example.com"
    _, reg = call("POST", "/api/register", {"email": email, "password": "Passw0rd!23"})
    return reg.get("access_token") or reg.get("token"), email


def new_project(token: str, name: str, budget: float = 10.0) -> int:
    _, project = call("POST", "/api/projects", {"name": name, "budget_total": budget}, token)
    return project["id"]


def engine():
    from sqlalchemy import create_engine

    from app.config import get_settings

    url = getattr(get_settings(), "database_url", None) or os.environ["DATABASE_URL"]
    return create_engine(str(url))


def rows(eng, sql: str, **params) -> list[dict]:
    from sqlalchemy import text

    with eng.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def scalar(eng, sql: str, **params):
    from sqlalchemy import text

    with eng.connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def poll(project_id: int, run_id: int, token: str, *, limit: int = 60) -> dict:
    terminal = {"completed", "partial_success", "failed", "timeout", "cancelled"}
    last: dict = {}
    for _ in range(limit):
        time.sleep(2)
        _, last = call("GET", f"/api/runs/{run_id}?project_id={project_id}", token=token)
        if last.get("status") in terminal:
            break
    return last


def data_dir() -> pathlib.Path:
    from app.config import get_settings

    raw = pathlib.Path(getattr(get_settings(), "data_dir", "./data"))
    return raw if raw.is_absolute() else (ROOT / raw)


def disk_facts(project_id: int) -> dict:
    """落盘产物：**写下去的必须是脱敏后的文本**。

    直跑栈（宿主 Web）写的是 `./data/uploads/<pid>/`，容器栈写的是 appdata 卷里的
    `/app/data/uploads/<pid>/` —— 两条路径都要能查，否则对着容器跑时
    这一条会"看起来没证据"，而不是"没脱敏"。
    """
    folder = data_dir() / "uploads" / str(project_id)
    files = sorted(p for p in folder.glob("*")) if folder.exists() else []
    out: dict = {
        "dir": str(folder.relative_to(ROOT)).replace("\\", "/"),
        "files": [p.name for p in files],
        "source": "host",
    }
    if files:
        text = files[0].read_text(encoding="utf-8", errors="replace")
        out.update(_masking_facts(text))
        return out

    container = _disk_facts_in_container(project_id)
    if container is not None:
        return container
    # 两条路都没读到：**如实说明**，不要让它看起来像"脱敏没做"
    out["note"] = "宿主机与容器内都没读到落盘文件（容器没起或 docker exec 失败）"
    return out


def _masking_facts(text: str) -> dict:
    return {
        "canary_email_on_disk": CANARY_EMAIL in text,
        "canary_secret_on_disk": CANARY_SECRET in text,
        "email_markers": text.count("[EMAIL_"),
        "secret_markers": text.count("[SECRET_"),
        "lines": text.count("\n"),
    }


def _disk_facts_in_container(project_id: int) -> dict | None:
    """宿主机看不到卷时，进容器读一次（同一份卷，web/worker 都挂着它）。

    这里刻意把子进程的 stdout **重定向到文件**，而不是用 `capture_output=True`：
    受限沙箱里"父进程用管道捕获子进程输出"会被直接挡掉（EPERM），
    而异常一旦被下面那句 `except` 吞掉，落盘脱敏这条证据就会**静默退化成
    "宿主机没读到文件"** —— 于是真机明明没问题，验收表却判"未达标"。
    （2026-09-24 实测踩到：对着容器栈跑时 04-2 变成了 ❌，而 E2E 里落盘是干净的。）
    """
    import subprocess

    script = (
        "import json,pathlib;"
        f"p=pathlib.Path('/app/data/uploads/{project_id}');"
        "fs=sorted(x for x in p.glob('*')) if p.exists() else [];"
        "t=fs[0].read_text(encoding='utf-8',errors='replace') if fs else '';"
        "print(json.dumps({'dir':str(p),'files':[x.name for x in fs],"
        f"'canary_email_on_disk':{CANARY_EMAIL!r} in t,"
        f"'canary_secret_on_disk':{CANARY_SECRET!r} in t,"
        "'email_markers':t.count('[EMAIL_'),'secret_markers':t.count('[SECRET_'),"
        "'lines':t.count(chr(10))}))"
    )
    out_file = TMP / f"disk_facts_{project_id}.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with out_file.open("w", encoding="utf-8") as handle:
            proc = subprocess.run(
                ["docker", "compose", "exec", "-T", "web", "python", "-c", script],
                cwd=ROOT,
                stdout=handle,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except Exception:  # noqa: BLE001 - 没有 docker 就用宿主结果
        return None
    if proc.returncode != 0 or not out_file.exists():
        return None
    for line in reversed(out_file.read_text(encoding="utf-8", errors="replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return {**json.loads(line), "source": "container"}
            except json.JSONDecodeError:
                continue
    return None


def merge(patch: dict) -> dict:
    payload = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    payload.update(patch)
    payload["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    payload["base"] = BASE
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return payload


# ============================================================
# ① 真实分析（会产生费用）
# ============================================================


def scenario(eng, name: str, source: pathlib.Path, fmt: str, *, canary: bool) -> dict:
    token, _ = register(name)
    pid = new_project(token, name)

    payload_path = source
    if canary:
        TMP.mkdir(parents=True, exist_ok=True)
        payload_path = TMP / f"{source.stem}_with_canary{source.suffix}"
        payload_path.write_bytes(source.read_bytes() + CANARY_LINE.encode())

    up_status, up = upload(pid, payload_path, fmt, token)
    source_id = up.get("data_source_id")

    t0 = time.time()
    run_status, created = call(
        "POST",
        f"/api/projects/{pid}/analysis-runs",
        {"data_source_id": source_id, "start_tier": "L2"},
        token,
    )
    accepted_in = round(time.time() - t0, 3)
    run_id = created.get("run_id")
    final = poll(pid, run_id, token) if run_id else {}
    _, detail = call("GET", f"/api/runs/{run_id}/detail?project_id={pid}", token=token)
    _, insights = call("GET", f"/api/runs/{run_id}/insights?project_id={pid}", token=token)
    # 候选知识是否真的进了 staging：经**知识审核接口**读（真机的文件在容器卷里，
    # 宿主机看不到；这个接口本身就是页面上「待确认知识」区的数据来源）。
    _, candidates = call(
        "GET", f"/api/projects/{pid}/knowledge/candidates", token=token, timeout=60
    )

    run = rows(eng, "select * from agent_runs where id=:rid", rid=run_id)[0] if run_id else {}
    budget = rows(eng, "select budget_total, budget_used from projects where id=:pid", pid=pid)[0]
    evidence_rows = rows(
        eng,
        "select e.insight_id, e.event_ids from evidences e join insights i on i.id=e.insight_id"
        " where i.run_id=:rid",
        rid=run_id,
    )
    valid_event_ids = {
        str(r["id"]) for r in rows(eng, "select id from events where project_id=:pid", pid=pid)
    }
    cited = {str(e) for row in evidence_rows for e in (row["event_ids"] or [])}

    return {
        "name": name,
        "source": str(source.relative_to(ROOT)).replace("\\", "/"),
        "canary_injected": canary,
        "project_id": pid,
        "data_source_id": source_id,
        "run_id": run_id,
        "create_run_http": run_status,
        "create_run_seconds": accepted_in,
        "create_run_body": created,
        "upload_http": up_status,
        "upload": up,
        "final_status": final.get("status"),
        "run_row": {
            k: (str(v) if hasattr(v, "isoformat") else v)
            for k, v in run.items()
            if k
            in {
                "status", "current_phase", "phase_history", "model_calls", "tool_usage",
                "tokens_input", "tokens_output", "cost_actual", "run_metadata", "error",
                "started_at", "finished_at", "last_heartbeat", "parent_run_id",
            }
        },
        "trace_id": (run.get("run_metadata") or {}).get("trace_id"),
        "project": {"budget_total": float(budget["budget_total"] or 0), "budget_used": float(budget["budget_used"] or 0)},
        "counts": {
            "events": scalar(eng, "select count(*) from events where project_id=:pid", pid=pid),
            "metric_events": scalar(
                eng, "select count(*) from events where project_id=:pid and event_type='metric'", pid=pid
            ),
            "event_groups": scalar(eng, "select count(*) from event_groups where project_id=:pid", pid=pid),
            "incidents": scalar(eng, "select count(*) from incidents where project_id=:pid", pid=pid),
            "events_with_group": scalar(
                eng, "select count(*) from events where project_id=:pid and group_id is not null", pid=pid
            ),
            "events_with_incident": scalar(
                eng, "select count(*) from events where project_id=:pid and incident_id is not null", pid=pid
            ),
            "insights_with_incident": scalar(
                eng, "select count(*) from insights where run_id=:rid and incident_id is not null", rid=run_id
            ),
            "insights": scalar(eng, "select count(*) from insights where run_id=:rid", rid=run_id),
            "insights_with_runbook": scalar(
                eng,
                "select count(*) from insights where run_id=:rid and run_metadata ? 'runbooks'",
                rid=run_id,
            ),
            "evidences": len(evidence_rows),
            "fact_without_evidence": scalar(
                eng,
                "select count(*) from insights i where i.run_id=:rid and i.type='fact'"
                " and not exists (select 1 from evidences e where e.insight_id=i.id)",
                rid=run_id,
            ),
            "cited_event_ids_not_in_run": sorted(cited - valid_event_ids),
            "db_events_with_plaintext": scalar(
                eng,
                "select count(*) from events where project_id=:pid and"
                " (message like '%' || :email || '%' or message like '%' || :secret || '%')",
                pid=pid,
                email=CANARY_EMAIL,
                secret=CANARY_SECRET,
            ),
        },
        "tool_usage": run.get("tool_usage"),
        "staged_candidates": (
            len(candidates) if isinstance(candidates, list) else candidates
        ),
        "insights": rows(
            eng,
            "select id, type, severity, confidence, title, run_metadata from insights"
            " where run_id=:rid order by id",
            rid=run_id,
        ),
        "narrative": (detail or {}).get("narrative", "")[:600],
        "api_insights": len(insights) if isinstance(insights, list) else insights,
        "disk": disk_facts(pid),
        "staging_file_exists": (
            data_dir() / "knowledge" / "computer_monitoring" / "staging" / "candidates.yaml"
        ).exists(),
    }


def run_scenarios() -> None:
    eng = engine()
    scenarios = [
        scenario(eng, "evidence-real-corpus", ROOT / "logs" / "Mac_2k.log", "txt", canary=True),
        scenario(
            eng,
            "evidence-crash-fixture",
            ROOT / "tests" / "datasets" / "crash" / "system.log",
            "txt",
            canary=False,
        ),
    ]
    traces = load_traces()
    anchors: dict[str, dict[str, int]] = {}
    for role, payload in traces.items():
        for key, count in (payload.get("calls") or {}).items():
            anchors.setdefault(key, {})[role] = count
    merge(
        {
            "scenarios": scenarios,
            "trace_summary": {
                role: {
                    "pid": t.get("pid"),
                    "call_count": t.get("call_count"),
                    "distinct_functions": len(t.get("calls") or {}),
                }
                for role, t in traces.items()
            },
            "traced_functions": anchors,
        }
    )
    for item in scenarios:
        c = item["counts"]
        print(
            f"[{item['name']}] run={item['run_id']} status={item['final_status']} "
            f"cost=¥{item['run_row'].get('cost_actual')} events={c['events']} "
            f"insights={c['insights']} evidences={c['evidences']} "
            f"fact无证据={c['fact_without_evidence']} 悬空={len(c['cited_event_ids_not_in_run'])} "
            f"预算={item['project']['budget_used']:.4f}/{item['project']['budget_total']}"
        )
        print(f"   落盘 {item['disk']}")
        print(f"   staging 候选文件存在={item['staging_file_exists']}")
    print(f"产出：{OUT.relative_to(ROOT)}")


def load_traces() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for role in ("web", "worker"):
        path = ROOT / ".dsh" / f"trace_{role}.json"
        out[role] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return out


# ============================================================
# ② 接口契约（零模型费用）
# ============================================================


def contract() -> None:
    eng = engine()
    facts: dict[str, object] = {}

    status, health = call("GET", "/healthz")
    facts["healthz"] = {"status_code": status, "body": health}
    status, deps = call("GET", "/healthz/deps")
    facts["healthz_deps"] = {"status_code": status, "body": deps}

    facts["docs_status"] = call("GET", "/docs")[0]
    facts["projects_without_token"] = call("GET", "/api/projects")[0]

    token, _ = register("contract")
    pid = new_project(token, "evidence-contract")
    status, body = call("POST", "/api/projects", {"budget_total": 1}, token)
    facts["invalid_project_body"] = {"status_code": status, "detail": str(body.get("detail"))[:120]}

    other_token, _ = register("contract-intruder")
    facts["cross_project_data_sources"] = call(
        "GET", f"/api/projects/{pid}/data-sources", token=other_token
    )[0]

    # 页面路由（服务端渲染的壳）
    pages = {}
    for path in ("/login", "/projects", f"/knowledge/{pid}"):
        status, raw = raw_get(path, token)
        pages[path] = {"status": status, "html": "<!DOCTYPE html>" in raw}
    facts["pages"] = pages

    # 取消 → 重试（不完整/失败结果的"重试入口"）。
    # 注意：**必须让这次 Run 有活干**。空数据源会走 L0 秒完，
    # 取消永远赶不上（第一版就是这么误判成"取消无效"的）。
    _, source = call("POST", f"/api/projects/{pid}/data-sources", {"format": "txt"}, token)
    _, up = upload(pid, ROOT / "tests" / "datasets" / "crash" / "system.log", "txt", token)
    _, created = call(
        "POST",
        f"/api/projects/{pid}/analysis-runs",
        {"data_source_id": up["data_source_id"], "start_tier": "L2"},
        token,
    )
    run_id = created.get("run_id")
    facts["cancel_http"] = call("POST", f"/api/runs/{run_id}/cancel", token=token)[0]
    time.sleep(2)
    facts["cancelled_status"] = call(
        "GET", f"/api/runs/{run_id}?project_id={pid}", token=token
    )[1].get("status")
    final = poll(pid, run_id, token, limit=20)
    facts["status_after_poll"] = final.get("status")
    facts["cancel_effective"] = final.get("status") == "cancelled"
    status, retried = call("POST", f"/api/runs/{run_id}/retry", token=token)
    facts["retry"] = {"status_code": status, "body": retried if isinstance(retried, dict) else retried}
    new_run = retried.get("run_id") if isinstance(retried, dict) else None
    if new_run:
        facts["retry_parent_run_id"] = scalar(
            eng, "select parent_run_id from agent_runs where id=:rid", rid=new_run
        )
        facts["retry_parent_matches"] = facts["retry_parent_run_id"] == run_id
        call("POST", f"/api/runs/{new_run}/cancel", token=token)

    status, raw = raw_get(f"/report/{run_id}?project_id={pid}", token)
    facts["report_page"] = {"status": status, "html": "<!DOCTYPE html>" in raw}

    # 幂等：同一输入重复提交不应产生第二个 Run（阶段 08 验收）。
    # 用空数据源（走 L0、零成本）即可验证"重复提交"这一条。
    pid2 = new_project(token, "evidence-idempotent")
    _, src2 = call("POST", f"/api/projects/{pid2}/data-sources", {"format": "txt"}, token)
    body = {"data_source_id": src2["id"], "start_tier": "L2", "time_range": {
        "start": "2026-09-01T00:00:00+00:00", "end": "2026-09-02T00:00:00+00:00"}}
    _, first = call("POST", f"/api/projects/{pid2}/analysis-runs", body, token)
    time.sleep(2)
    status2, second = call("POST", f"/api/projects/{pid2}/analysis-runs", body, token)
    facts["idempotent"] = {
        "first_run_id": first.get("run_id"),
        "second_run_id": second.get("run_id"),
        "same_run_id": first.get("run_id") == second.get("run_id"),
        "reused": second.get("reused"),
        "second_status": status2,
    }

    merge({"api_contract": facts})
    print(json.dumps(facts, ensure_ascii=False, indent=1)[:2500])


def raw_get(path: str, token: str | None = None) -> tuple[int, str]:
    req = urllib.request.Request(BASE + path, method="GET")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with OPENER.open(req, timeout=60) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


# ============================================================
# ③ 只上传不分析（零模型费用）
# ============================================================


def upload_only() -> None:
    eng = engine()
    token, _ = register("uploads")
    pid = new_project(token, "evidence-uploads")
    facts: dict[str, object] = {"project_id": pid}

    cases = [
        ("jsonl", ROOT / "logs" / "nginx_json_access.log", "jsonl"),
        ("negative_sample", ROOT / "logs" / "nginx_plain.log", "txt"),
        ("metric_events", ROOT / "tests" / "datasets" / "cpu_anomaly" / "metrics.log", "txt"),
        ("malformed", ROOT / "tests" / "datasets" / "malformed" / "mixed.log", "txt"),
    ]
    for name, path, fmt in cases:
        status, body = upload(pid, path, fmt, token)
        facts[name] = {
            "status_code": status,
            "file": str(path.relative_to(ROOT)).replace("\\", "/"),
            "events_persisted": body.get("events_persisted"),
            "parse": body.get("parse"),
            "mask": body.get("mask"),
            "created_data_source": body.get("created_data_source"),
            "data_source_id": body.get("data_source_id"),
        }

    # 未带 data_source_id 的第二次上传应当**复用**同一个源
    status, body = upload(pid, ROOT / "logs" / "Mac_2k.log", "txt", token)
    facts["reuse_second_upload"] = {
        "created_data_source": body.get("created_data_source"),
        "data_source_id": body.get("data_source_id"),
    }

    # 超限：> 10 MiB 必须被明确拒绝
    TMP.mkdir(parents=True, exist_ok=True)
    big = TMP / "oversize.log"
    big.write_bytes(b"Sep 24 10:00:00 host proc[1]: padding line for size limit\n" * 200_000)
    status, body = upload(pid, big, "txt", token)
    facts["oversize"] = {
        "status_code": status,
        "size_bytes": big.stat().st_size,
        "detail": str(body.get("detail"))[:160],
    }

    facts["db"] = {
        "events": scalar(eng, "select count(*) from events where project_id=:pid", pid=pid),
        "metric_events": scalar(
            eng, "select count(*) from events where project_id=:pid and event_type='metric'", pid=pid
        ),
        "data_sources": scalar(eng, "select count(*) from data_sources where project_id=:pid", pid=pid),
    }
    facts["stored_files"] = disk_facts(pid)
    merge({"uploads_only": facts})
    print(json.dumps(facts, ensure_ascii=False, indent=1, default=str)[:2500])


# ============================================================
# ④ 僵尸回收：起 Run → 杀 Worker → 看状态（真机实测"有没有人回收"）
# ============================================================


def zombie_start() -> None:
    token, _ = register("zombie")
    pid = new_project(token, "evidence-zombie")
    _, up = upload(pid, ROOT / "logs" / "Mac_2k.log", "txt", token)
    _, created = call(
        "POST",
        f"/api/projects/{pid}/analysis-runs",
        {"data_source_id": up["data_source_id"], "start_tier": "L3"},
        token,
    )
    ZOMBIE.write_text(
        json.dumps({"project_id": pid, "run_id": created.get("run_id"), "token": token}),
        encoding="utf-8",
    )
    print(f"已起 Run {created.get('run_id')}（project {pid}）；现在去杀 Worker，再跑 --zombie-check")


def zombie_check() -> None:
    eng = engine()
    info = json.loads(ZOMBIE.read_text(encoding="utf-8"))
    rid, pid = info["run_id"], info["project_id"]
    row = rows(
        eng,
        "select status, last_heartbeat, started_at, finished_at, error, run_metadata"
        " from agent_runs where id=:rid",
        rid=rid,
    )[0]
    now = scalar(eng, "select now()")
    age = None
    if row["last_heartbeat"] is not None:
        age = scalar(eng, "select extract(epoch from (now() - :hb))", hb=row["last_heartbeat"])
    facts = {
        "run_id": rid,
        "project_id": pid,
        "status": row["status"],
        "heartbeat_age_seconds": round(float(age), 1) if age is not None else None,
        "checked_at": str(now),
        "error": row["error"],
        "note": (
            "已由维护队列回收为 timeout（心跳丢失）—— 回收者住在 beat 容器的 "
            "maintenance 队列上，与分折 Worker 无关，故 Worker 停摆不影响回收"
            if row["status"] == "timeout"
            else f"Worker 被杀后 Run 停在 {row['status']}，等待回收；"
            "若长期停在这里，说明 beat 的维护队列或 schedule 没生效"
        ),
    }
    merge({"zombie_test": facts})
    print(json.dumps(facts, ensure_ascii=False, indent=1, default=str))


# ============================================================
# ⑤ 刷新派生事实（零模型费用：只重查库与磁盘，不重跑分析）
# ============================================================


def refresh() -> None:
    """对已采集的场景重算"派生事实"（落盘、计数、staging 是否存在）。

    为什么要这个模式：磁盘路径口径改对之后，不该为了补一个字段再花一次模型钱 ——
    重查库与磁盘得到的是同一批事实，不是"重新解释"。
    """
    eng = engine()
    payload = json.loads(OUT.read_text(encoding="utf-8"))
    for item in payload.get("scenarios", []):
        pid, rid = item["project_id"], item["run_id"]
        evidence_rows = rows(
            eng,
            "select e.event_ids from evidences e join insights i on i.id=e.insight_id"
            " where i.run_id=:rid",
            rid=rid,
        )
        valid = {
            str(r["id"]) for r in rows(eng, "select id from events where project_id=:pid", pid=pid)
        }
        cited = {str(e) for row in evidence_rows for e in (row["event_ids"] or [])}
        item["counts"].update(
            {
                "metric_events": scalar(
                    eng,
                    "select count(*) from events where project_id=:pid and event_type='metric'",
                    pid=pid,
                ),
                "cited_event_ids_not_in_run": sorted(cited - valid),
                "db_events_with_plaintext": scalar(
                    eng,
                    "select count(*) from events where project_id=:pid and"
                    " (message like '%' || :email || '%' or message like '%' || :secret || '%')",
                    pid=pid,
                    email=CANARY_EMAIL,
                    secret=CANARY_SECRET,
                ),
            }
        )
        item["disk"] = disk_facts(pid)
        item["staging_file_exists"] = (
            data_dir() / "knowledge" / "computer_monitoring" / "staging" / "candidates.yaml"
        ).exists()
    merge({"scenarios": payload.get("scenarios", [])})
    for item in payload.get("scenarios", []):
        print(f"[{item['name']}] 落盘={item['disk']} staging={item['staging_file_exists']}")
        print(f"   counts={json.dumps(item['counts'], ensure_ascii=False, default=str)[:220]}")


# ============================================================
# ⑥ 受控实验：取消（让 Run 真在跑，再取消）
# ============================================================


def cancel_test(wait_seconds: int = 20) -> None:
    """受控实验：**让 Run 真的在跑**，再取消，看它会不会停。

    为什么不能"创建后立刻取消"：那样测的是"取消一个还没被 Worker 取走的任务"，
    而验收要的是「取消在**下个检查点**生效」—— 下个检查点在模型调用返回之后。
    """
    token, _ = register("cancel")
    pid = new_project(token, "evidence-cancel")
    _, up = upload(pid, ROOT / "logs" / "Mac_2k.log", "txt", token)
    _, created = call(
        "POST",
        f"/api/projects/{pid}/analysis-runs",
        {"data_source_id": up["data_source_id"], "start_tier": "L2"},
        token,
    )
    rid = created.get("run_id")
    print(f"Run {rid} 已起（project {pid}），等 {wait_seconds}s 再取消…")
    time.sleep(wait_seconds)
    cancel_http = call("POST", f"/api/runs/{rid}/cancel", token=token)[0]
    seen: list[str] = []
    final: dict = {}
    for _ in range(40):
        time.sleep(3)
        _, body = call("GET", f"/api/runs/{rid}?project_id={pid}", token=token)
        status = body.get("status")
        if status not in seen:
            seen.append(status)
        final = body
        if status in {"completed", "partial_success", "failed", "timeout", "cancelled"}:
            break
    eng = engine()
    row = rows(
        eng, "select cancel_requested, status, cost_actual from agent_runs where id=:r", r=rid
    )[0]
    facts = {
        "run_id": rid,
        "project_id": pid,
        "cancel_http": cancel_http,
        "statuses_seen": seen,
        "final_status": final.get("status"),
        "cancel_requested_in_db": bool(row["cancel_requested"]),
        "cost_actual": float(row["cost_actual"] or 0),
        "cancel_effective": final.get("status") == "cancelled",
        "note": (
            "取消在下一个检查点生效：状态 running → cancelled"
            if final.get("status") == "cancelled"
            else "cancel_requested 已落库却仍跑到终态 → 取消没有生效"
        ),
    }
    merge({"cancel_test": facts})
    print(json.dumps(facts, ensure_ascii=False, indent=1, default=str))


# ============================================================
# ⑦ 汇总：把另外两个证据产出者的结果也记进来（可一条命令复现）
# ============================================================


def sweep() -> None:
    """跑 `final_verify.cjs`（容器全栈 16 项 E2E）与 `golden_eval.py`（Golden Set 离线）。

    它们各自已有脚本，这里只做两件事：**跑一遍**、把结论摘成结构化事实 ——
    免得验收表里的 E2E/Golden 数字靠手工抄。
    """
    import re
    import subprocess

    facts: dict[str, object] = {}
    e2e = subprocess.run(
        ["node", ".dsh/final_verify.cjs"], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False,
    )
    match = re.search(r"(\d+)/(\d+) 项通过", e2e.stdout or "")
    facts["e2e"] = {
        "passed": int(match.group(1)) if match else 0,
        "total": int(match.group(2)) if match else 0,
        "exit_code": e2e.returncode,
    }
    print(f"E2E：{facts['e2e']}")

    golden = subprocess.run(
        [str(ROOT / ".venv" / "Scripts" / "python.exe"), ".dsh/golden_eval.py"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
    )
    text = golden.stdout or ""
    passed = text.count("[PASS]")
    failed_match = re.search(r"失败断言 (\d+) 条", text)
    cost_match = re.search(r"合计成本 ¥([\d.]+)", text)
    facts["golden"] = {
        "passed": passed,
        "failed": int(failed_match.group(1)) if failed_match else -1,
        "total": passed + (int(failed_match.group(1)) if failed_match else 0),
        "cost": float(cost_match.group(1)) if cost_match else None,
    }
    print(f"Golden：{facts['golden']}")

    merge(facts)


# ============================================================


def main(argv: list[str]) -> int:
    if "--sweep" in argv:
        sweep()
        return 0
    if "--refresh" in argv:
        refresh()
        return 0
    if "--cancel-test" in argv:
        # 允许指定"等几秒再取消"：取消必须落在**分析进行中**。
        # 太晚就等于在测"取消一个已经跑完的 Run" —— 第一版就是这么误判的。
        index = argv.index("--cancel-test")
        wait = (
            int(argv[index + 1])
            if len(argv) > index + 1 and argv[index + 1].isdigit()
            else 20
        )
        cancel_test(wait)
        return 0
    if "--contract" in argv:
        contract()
        return 0
    if "--uploads" in argv:
        upload_only()
        return 0
    if "--zombie-start" in argv:
        zombie_start()
        return 0
    if "--zombie-check" in argv:
        zombie_check()
        return 0
    run_scenarios()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
