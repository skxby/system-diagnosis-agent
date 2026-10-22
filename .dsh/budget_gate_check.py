"""真机核验成本闸门（几乎零成本：被拒的路径不花钱，"放行"的用空数据源走 L0 也不花钱）。

跑在**真实 HTTP + 真实数据库**上，逐条核验计划第 639–652 行那三道闸门：

  ① 预算 < 一次 L2/L3 估计 → 创建被拒 402
  ② 预算 0.00001（小于金额精度）→ 422，而不是被四舍五入成 0（=不设上限）
  ③ 不填预算（0）→ 视为未设上限，正常放行
  ④ **月度预算**：本月已花满 → 拒绝创建（计划第 644 行）
  ⑤ **跨月边界**：上个月花满 → 本月照样放行（月度预算是"本月"，不是"历史累计"）
  ⑥ **真实 Run 有没有把 started_at 写进去** —— 月度预算的取数以它为准，
     它为空则"本月已花"恒为 0，④那道闸门对真实用量**永远不触发**
  ⑦ **峰谷价**：同一时刻下「Pre-check 估算口径」与「实际计费口径」是否一致

④⑤ 用**手工插入的历史 Run** 造用量（用户说的"要造历史 Run"），
因为真机不可能等到下个月；插入的行带真实列结构与真实 started_at。
⑥⑦ 直接读真库、真配置、真函数，不做任何模拟。

用法：python .dsh/budget_gate_check.py            # 全部
      python .dsh/budget_gate_check.py --no-http  # 只跑 ⑥⑦（不需要 Web）
"""

from __future__ import annotations

import http.cookiejar
import json
from upload_polling import wait_upload
import os
import pathlib
import secrets
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = os.environ.get("EVIDENCE_BASE", "http://127.0.0.1:8000")
EVIDENCE = ROOT / ".dsh" / "runtime_evidence.json"
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


def call(method: str, path: str, body=None, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with OPENER.open(req, timeout=90) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:200].decode("utf-8", "replace")}


def engine():
    from sqlalchemy import create_engine

    from app.config import get_settings

    return create_engine(str(get_settings().database_url))


def insert_historical_run(eng, project_id: int, *, cost: float, started_at: datetime) -> int:
    """造一条历史 Run（真实列结构）——用来模拟"这个月/上个月已经花了这么多"。"""
    from sqlalchemy import text

    with eng.begin() as conn:
        return int(
            conn.execute(
                text(
                    """
                    insert into agent_runs
                      (project_id, status, source_id, input, idempotency_key, phase_history,
                       started_at, finished_at, tokens_input, tokens_output, cost_actual,
                       run_metadata, cancel_requested)
                    values
                      (:pid, 'completed', null, '{}'::jsonb, :key, '[]'::jsonb,
                       :started, :started, 0, 0, :cost, cast(:meta as jsonb), false)
                    returning id
                    """
                ),
                {
                    "pid": project_id,
                    "key": f"historical-{secrets.token_hex(6)}",
                    "started": started_at,
                    "cost": cost,
                    "meta": json.dumps({"note": "真机核验造的历史 Run"}, ensure_ascii=False),
                },
            ).scalar_one()
        )


def new_project(token: str) -> int:
    _, project = call("POST", "/api/projects", {"name": f"budget-{secrets.token_hex(3)}"}, token)
    return project["id"]


def new_source(token: str, project_id: int) -> int:
    _, source = call("POST", f"/api/projects/{project_id}/data-sources", {"format": "txt"}, token)
    return source["id"]


def monthly_e2e() -> None:
    """**端到端**月度预算核验：真花掉一笔钱，再让闸门真的拦住。

    与 ④⑤ 的区别：④⑤ 的"本月已花"是手工插的历史 Run 造出来的，
    证明的是**逻辑**；这里跑一次真实分析，让它自己把 `started_at` 与
    `cost_actual` 写进去，再发起第二次 —— 证明的是**取数链路**
    （Worker 写 started_at → cost_sum_since 汇总 → Pre-check 拦下）。

    需要把 `MONTHLY_BUDGET` 调小（否则真花到 10 元太贵），所以本模式要求
    跑在一个 `MONTHLY_BUDGET` 很小的栈上（见脚本头部用法）。
    """
    import time

    from sqlalchemy import text

    eng = engine()
    from app.config import get_settings

    budget = float(get_settings().monthly_budget)
    print(f"本模式的月度预算 = ¥{budget}（要小到一次真实分析就能花完）")

    email = f"monthly-e2e-{secrets.token_hex(4)}@example.com"
    _, reg = call("POST", "/api/register", {"email": email, "password": "Passw0rd!23"})
    token = reg.get("access_token") or reg.get("token")
    pid = new_project(token)

    path = ROOT / "tests" / "datasets" / "crash" / "system.log"
    boundary = "----monthly" + secrets.token_hex(8)
    body = bytearray()
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'.encode()
    body += b"Content-Type: text/plain\r\n\r\n" + path.read_bytes() + b"\r\n"
    body += f'--{boundary}\r\nContent-Disposition: form-data; name="fmt"\r\n\r\ntxt\r\n'.encode()
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{BASE}/api/projects/{pid}/upload", data=bytes(body), method="POST"
    )
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    with OPENER.open(req, timeout=180) as response:
        code, up = wait_upload(BASE, pid, token, response.status, json.loads(response.read() or b"{}"))
        if code != 200:
            raise RuntimeError(f"上传解析失败：{up}")

    status, created = call(
        "POST",
        f"/api/projects/{pid}/analysis-runs",
        {"data_source_id": up["data_source_id"], "start_tier": "L2"},
        token,
    )
    run_id = created.get("run_id")
    terminal = {"completed", "partial_success", "failed", "timeout", "cancelled"}
    for _ in range(60):
        time.sleep(2)
        _, body_json = call("GET", f"/api/runs/{run_id}?project_id={pid}", token=token)
        if body_json.get("status") in terminal:
            break

    with eng.connect() as conn:
        row = conn.execute(
            text(
                "select status, started_at, cost_actual from agent_runs where id = :r"
            ),
            {"r": run_id},
        ).one()
        month_spent = conn.execute(
            text(
                "select coalesce(sum(cost_actual), 0) from agent_runs"
                " where project_id = :p and started_at is not null"
                "   and started_at >= date_trunc('month', now() at time zone 'UTC')"
            ),
            {"p": pid},
        ).scalar_one()

    status2, second = call(
        "POST",
        f"/api/projects/{pid}/analysis-runs",
        {"data_source_id": up["data_source_id"], "start_tier": "L2"},
        token,
    )
    facts = {
        "http_first_run": status,
        "run_id": run_id,
        "run_status": row[0],
        "started_at": str(row[1]),
        "cost_actual": float(row[2] or 0),
        "month_spent_from_db": float(month_spent),
        "http_second_run": status2,
        "second_detail": str(second.get("detail"))[:200],
        "gate_blocked": status2 == 402 and "本月已花" in str(second.get("detail", "")),
    }
    payload = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.exists() else {}
    merged = dict(payload.get("budget_gate") or {})
    merged["monthly_e2e"] = facts
    payload["budget_gate"] = merged
    EVIDENCE.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    print(f"① 真实分析 Run {run_id} → {row[0]}，started_at={row[1]}，花费 ¥{float(row[2] or 0):.6f}")
    print(f"② 库里「本月已花」 = ¥{float(month_spent):.6f}")
    print(f"③ 再发起一次 → {status2} | {str(second.get('detail'))[:110]}")
    print(f"闸门真机拦下 = {facts['gate_blocked']}")


def main(argv: list[str]) -> int:
    if "--monthly-e2e" in argv:
        monthly_e2e()
        return 0
    from app.config import get_settings
    from app.policy.wiring import build_cost_controller, month_start
    from app.utils.timestamps import is_peak_time

    settings = get_settings()
    eng = engine()
    facts: dict[str, object] = {
        "monthly_budget_setting": settings.monthly_budget,
        "run_max_cost_setting": settings.default_run_max_cost,
        "peak_multiplier_setting": settings.model_peak_price_multiplier,
        "timezone": settings.default_timezone,
    }
    want_http = "--no-http" not in argv

    if want_http:
        email = f"eval-budget-{secrets.token_hex(4)}@example.com"
        _, reg = call("POST", "/api/register", {"email": email, "password": "Passw0rd!23"})
        token = reg.get("access_token") or reg.get("token")
        window = {"start": "2026-09-23T12:00:00+00:00", "end": "2026-09-23T13:00:00+00:00"}

        # ① 预算太小 → 拒绝创建
        pid = new_project(token)
        call("PATCH", f"/api/projects/{pid}", {"budget_total": 0.001}, token)  # 兼容无此端点时忽略
        with eng.begin() as conn:
            from sqlalchemy import text

            conn.execute(
                text("update projects set budget_total = 0.001 where id = :p"), {"p": pid}
            )
        sid = new_source(token, pid)
        status, body = call(
            "POST",
            f"/api/projects/{pid}/analysis-runs",
            {"data_source_id": sid, "start_tier": "L3", "time_range": window},
            token,
        )
        facts["tiny_project_budget"] = {"http": status, "detail": str(body.get("detail"))[:120]}
        print(f"① 项目预算 0.001 创建 Run → {status} | {str(body.get('detail'))[:100]}")

        # ② 低于金额精度 → 422
        status, body = call("POST", "/api/projects", {"name": "budget-too-small", "budget_total": 0.00001}, token)
        facts["below_precision"] = {"http": status, "detail": str(body.get("detail"))[:120]}
        print(f"② 预算 0.00001 → {status} | {str(body.get('detail'))[:80]}")

        # ③ 不填预算 → 放行
        pid3 = new_project(token)
        sid3 = new_source(token, pid3)
        status, body = call(
            "POST",
            f"/api/projects/{pid3}/analysis-runs",
            {"data_source_id": sid3, "time_range": window},
            token,
        )
        facts["no_project_budget"] = {"http": status, "status_field": body.get("status")}
        print(f"③ 不填项目预算创建 Run → {status} | {json.dumps(body, ensure_ascii=False)[:80]}")

        # ④ 月度预算：本月已花满 → 应当拒绝
        pid4 = new_project(token)
        sid4 = new_source(token, pid4)
        now = datetime.now(timezone.utc)
        this_month = insert_historical_run(
            eng, pid4, cost=settings.monthly_budget - 0.001, started_at=now
        )
        status, body = call(
            "POST",
            f"/api/projects/{pid4}/analysis-runs",
            {"data_source_id": sid4, "start_tier": "L2", "time_range": window},
            token,
        )
        facts["month_spent_full"] = {
            "http": status,
            "detail": str(body.get("detail"))[:200],
            "seeded_run_id": this_month,
            "seeded_cost": settings.monthly_budget - 0.001,
            "month_start": str(month_start(settings=settings)),
        }
        print(
            f"④ 本月已花 ¥{settings.monthly_budget - 0.001}（月度预算 ¥{settings.monthly_budget}）"
            f" → 创建 Run {status} | {str(body.get('detail'))[:90]}"
        )

        # ⑤ 跨月边界：上个月花满 → 本月应当放行
        pid5 = new_project(token)
        sid5 = new_source(token, pid5)
        last_month = insert_historical_run(
            eng, pid5, cost=settings.monthly_budget * 3, started_at=now - timedelta(days=35)
        )
        status, body = call(
            "POST",
            f"/api/projects/{pid5}/analysis-runs",
            {"data_source_id": sid5, "time_range": window},
            token,
        )
        facts["last_month_spent_full"] = {
            "http": status,
            "status_field": body.get("status"),
            "detail": str(body.get("detail"))[:200],
            "seeded_run_id": last_month,
            "seeded_cost": settings.monthly_budget * 3,
        }
        print(
            f"⑤ 上月已花 ¥{settings.monthly_budget * 3}（跨月） → 创建 Run {status} | "
            f"{json.dumps(body, ensure_ascii=False)[:90]}"
        )

    # ⑥ 真实 Run 的 started_at：月度预算的取数依据
    from sqlalchemy import text

    with eng.connect() as conn:
        total = conn.execute(text("select count(*) from agent_runs")).scalar_one()
        with_started = conn.execute(
            text("select count(*) from agent_runs where started_at is not null")
        ).scalar_one()
        spent_runs = conn.execute(
            text("select count(*) from agent_runs where cost_actual > 0")
        ).scalar_one()
    facts["started_at_coverage"] = {
        "runs_total": total,
        "runs_with_started_at": with_started,
        "runs_with_cost": spent_runs,
    }
    print(
        f"⑥ 全库 Run {total} 条（其中 {spent_runs} 条真的花过钱），"
        f"started_at 非空的有 {with_started} 条"
    )
    if with_started == 0:
        print(
            "   ⚠️ 真实 Run 的 started_at 全为空 → cost_sum_since 的 "
            "`started_at is not null` 会把它们**全部排除**，"
            "本月已花恒为 0，④那道月度闸门对真实用量永远不触发"
        )

    # ⑦ 峰谷价：估算口径 vs 计费口径
    real_project = None
    from sqlalchemy import text as _text

    with eng.connect() as conn:
        row = conn.execute(
            _text("select id from projects order by id desc limit 1")
        ).first()
        real_project = row[0] if row else None

    controller = build_cost_controller(project=_project_row(eng, real_project), settings=settings)
    now_peak = is_peak_time(
        datetime.now(timezone.utc), default_timezone=settings.default_timezone
    )
    peak_probe = is_peak_time(
        datetime(2026, 9, 24, 2, 0, tzinfo=timezone.utc),
        default_timezone=settings.default_timezone,
    )
    if controller is not None:
        pre = controller.pre_check("L3")
        estimate = pre.estimate.estimated_cost_with_margin if pre.estimate else None
        tier_config = controller.tier_configs["L3"]
        billed_now = tier_config.estimate_cost(8000, 2000, peak=now_peak)
        billed_peak = tier_config.estimate_cost(8000, 2000, peak=True)
        # 估算侧现在也吃 peak（`estimate_run_cost(..., peak=...)`）。
        # 用真正的估算函数在高峰口径下算一遍，和计费口径比 ——
        # 两者必须只差 10% 安全边际；此前估算侧根本不知道峰谷，差的是 2 倍。
        from app.policy.cost_controller import estimate_run_cost

        estimate_peak = estimate_run_cost(
            "L3", tier_config, peak=True
        ).estimated_cost_with_margin
        facts["peak_valley"] = {
            "now_is_peak": now_peak,
            "peak_window_probe_0200utc": peak_probe,
            "controller_peak_now": bool(getattr(controller, "peak_now", None)),
            "pre_check_estimate_L3": estimate,
            "pre_check_estimate_L3_at_peak": round(estimate_peak, 6),
            "billed_formula_L3_peak_now": round(billed_now, 6),
            "billed_formula_L3_peak_forced": round(billed_peak, 6),
            "peak_multiplier": tier_config.peak_price_multiplier,
            # 估算 = 计费 × 1.1（安全边际）—— 两个口径终于统一
            "estimate_tracks_peak": abs(estimate_peak - billed_peak * 1.1) < 1e-9,
        }
        print(
            f"⑦ 此刻是否高峰={now_peak}（装配出的控制器 peak_now="
            f"{getattr(controller, 'peak_now', None)}）｜Pre-check 估算 ¥{estimate:.6f}"
            f"（闲时口径）｜同一份 token 走高峰口径：估算 ¥{estimate_peak:.6f} vs 计费 "
            f"¥{billed_peak:.6f}（倍数 {tier_config.peak_price_multiplier}，"
            f"估算=计费×1.1 安全边际：{facts['peak_valley']['estimate_tracks_peak']}）"
        )

    payload = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.exists() else {}
    merged = dict(payload.get("budget_gate") or {})
    merged.update(facts)
    payload["budget_gate"] = merged
    payload["generated_at"] = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    EVIDENCE.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"\n已并入 {EVIDENCE.relative_to(ROOT)}")
    return 0


def _project_row(eng, project_id):
    from app.models.project import Project
    from sqlalchemy.orm import Session

    if project_id is None:
        return None
    with Session(eng) as session:
        return session.get(Project, int(project_id))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
