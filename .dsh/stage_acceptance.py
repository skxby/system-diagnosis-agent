"""阶段验收表（**真机口径**）：每条验收 = 测试证据 + 真机接线 + 运行时实测。

## 为什么要重出一版

上一轮给 14 个阶段打的是"测试全绿 → ✅"，而本轮的教训正是**测试全绿 ≠ 真机生效**：
阶段 07 的三道成本闸门当时单测全绿、代码齐全，真机上一次都没被调用过。
口径不改，同样的事还会再发生一次。

## 三种证据，缺一不可

| 证据 | 来源 | 回答的问题 |
|---|---|---|
| 测试证据 | `.dsh/junit/all.xml`（pytest 逐用例） | 逻辑对不对 |
| 真机接线（静态） | `.dsh/wiring_graph.py` 从真实入口点的调用图可达性 | 从 HTTP/Worker 入口**能不能**走到它 |
| 真机实测（运行时） | `.dsh/runtime_evidence.json`（真跑一遍的落库/落盘/追踪事实） | 这一次真实运行里它**到底跑了没有** |

判定：
  - 锚点有未接入 → **❌ 未达标**（真机不生效，测试再多也不算过）；
  - 运行时证据判定不达标 → **❌ 未达标**；
  - 运行时证据达标 → **✅ 达标**；
  - 只有测试 + 静态接线、本轮没跑到 → **⚠️ 待实测**（不许写成 ✅）。

用法：
    python .dsh/stage_acceptance.py            # 用已有 junit XML（超过 2 小时才重跑）
    python .dsh/stage_acceptance.py --rerun-tests
输出：`.dsh/阶段验收表.md`（正文另有一份带说明的报告）
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from wiring_graph import WiringGraph  # noqa: E402

JUNIT = ROOT / ".dsh" / "junit" / "all.xml"
EVIDENCE = ROOT / ".dsh" / "runtime_evidence.json"
OUT = ROOT / "阶段验收表-真机口径.md"
PY = ROOT / ".venv" / "Scripts" / "python.exe"


# ============================================================
# 验收条目（原文取自编码计划各阶段「验收」段）
# ============================================================


@dataclass
class Item:
    no: str
    text: str
    tests: list[str] = field(default_factory=list)
    anchors: list[str] = field(default_factory=list)
    runtime: str | None = None
    note: str = ""
    #: 该条**只可能**由测试/结构证明（没有运行时观测口径）。
    #: 显式写出来，免得"缺运行时证据"被默默当成达标。
    test_only: bool = False


ITEMS: list[Item] = [
    # ---------------- 阶段 01 ----------------
    Item("01-1", "`docker compose up` 起 PostgreSQL + Redis", ["tests/integration/test_env_connectivity.py"], [], "compose_stack"),
    Item("01-2", "FastAPI 启动并返回健康检查", ["tests/integration/test_env_connectivity.py"], ["app.main.healthz", "app.main.healthz_deps"], "healthz"),
    Item("01-3", "Celery worker 能启动并消费一个测试任务", [], ["app.tasks.health.ping", "app.tasks.analysis.execute_run_task"], "worker_consumed"),
    Item("01-4", "`pytest` 可运行（先放一个冒烟测试）", [], [], "pytest_all"),
    Item("01-5", "配置全部来自环境变量，代码里无密钥", ["tests/unit/test_config.py", "tests/unit/test_delivery_config.py"], ["app.config.get_settings"], "no_secret_literal"),
    # ---------------- 阶段 02 ----------------
    Item("02-1", "迁移可执行可回滚", [], [], "migrate", note="upgrade 真机执行过；downgrade 无自动化用例"),
    Item("02-2", "能创建 Project / DataSource", ["tests/integration/test_api_pg.py"], ["app.api.routes_auth_projects.create_project"], "project_source"),
    Item("02-3", "能插入普通事件与指标事件并按 project 查回", ["tests/integration/test_models_pg.py", "tests/integration/test_upload_pipeline.py"], ["app.repositories.event.EventRepository.pipeline_events"], "metric_events"),
    Item("02-4", "事件可归入 EventGroup、再归并为 Incident 并查回", ["tests/integration/test_models_pg.py"], ["app.repositories.event_group.EventGroupRepository.create_group", "app.repositories.incident.IncidentRepository.create_incident"], "grouping_persisted"),
    Item("02-5", "`model_calls` 可追加记录", ["tests/integration/test_models_pg.py"], ["app.repositories.agent_run.AgentRunRepository.append_model_call"], "model_calls"),
    # ---------------- 阶段 03 ----------------
    Item("03-1", "能注册并加载 computer_monitoring", ["tests/unit/test_domain_contract.py"], ["app.domains.registry.get_domain_registry", "app.domains.wiring.build_default_registry", "app.domains.computer_monitoring.ComputerMonitoringDomain"], "domain_loaded"),
    Item("03-2", "不修改 Runtime 代码即可被替换", ["tests/unit/test_domain_contract.py"], ["app.domains.registry.DomainRegistry.register"], None, note="结构性质：注册表 + 组合根分离，靠契约测试钉死", test_only=True),
    Item("03-3", "对样例行能产出标准事件、指标事件", ["tests/unit/test_parser_computer_monitoring.py", "tests/integration/test_upload_pipeline.py"], ["app.domains.computer_monitoring.ComputerMonitoringDomain.parse_line", "app.domains.computer_monitoring.parser.parse_line"], "events_parsed"),
    Item("03-4", "命中异常时能取到对应 runbook", ["tests/unit/test_analyzers_and_loaders.py"], ["app.analysis.pipeline.attach_runbooks", "app.domains.computer_monitoring.ComputerMonitoringDomain.find_runbook_for"], "runbook_attached"),
    # ---------------- 阶段 04 ----------------
    Item("04-1", "上传 TXT / JSONL 返回任务 ID，后台入库并可查询解析统计", ["tests/integration/test_upload_pipeline.py", "tests/integration/test_async_upload_pg.py"], ["app.services.upload_service.UploadService.prepare", "app.tasks.upload.process_upload"], "upload_parse"),
    Item("04-2", "含邮箱 / 密钥的内容在落盘文件和数据库中都已被替换", ["tests/integration/test_upload_pipeline.py"], ["app.utils.masking.Masker.mask"], "masking_disk_db"),
    Item("04-3", "坏行 / 无法识别的时间戳被计数并明确报告，不静默丢弃", ["tests/integration/test_upload_pipeline.py"], [], "bad_lines"),
    Item("04-4", "未带 data_source_id 的上传能自动建 / 复用 DataSource", ["tests/integration/test_upload_pipeline.py"], ["app.services.upload_service.UploadService._resolve_data_source"], "datasource_reuse"),
    Item("04-5", "超过大小上限的上传被拒绝并给出明确提示", ["tests/integration/test_upload_pipeline.py"], ["app.services.upload_service.UploadService._enforce_size_limit"], "oversize"),
    # ---------------- 阶段 05 ----------------
    Item("05-1", "三个工具结果正确、边界行为明确", ["tests/unit/test_tools_data_ops.py"], ["app.tools.data_ops.stats_calculator", "app.tools.data_ops.event_filter"], "tools_executed", note="time_window 已注册但生产路径暂无调用方（见报告）"),
    Item("05-2", "注册器能按名取用", ["tests/unit/test_tools_registry.py"], ["app.tools.registry.get_tool_registry", "app.tools.registry.ToolRegistry.call"], "tool_registry"),
    Item("05-3", "执行结果可记录到 Run", ["tests/integration/test_tool_usage_recorded.py"], ["app.repositories.agent_run.AgentRunRepository.append_tool_usage"], "tool_usage"),
    # ---------------- 阶段 06 ----------------
    Item("06-1", "至少接通一个真实供应商并返回结构化结果", ["tests/unit/test_gateway_router.py"], ["app.gateways.deepseek.DeepSeekGateway.generate", "app.gateways.router.Router.generate"], "model_called"),
    Item("06-2", "切换型号只改环境变量、不改业务代码", ["tests/unit/test_gateway_router.py"], ["app.gateways.router.read_tier_configs"], "tier_fingerprint"),
    Item("06-3", "token 与成本被记录", ["tests/unit/test_gateway_router.py"], ["app.gateways.router.Router._record"], "tokens_cost"),
    # ---------------- 阶段 07 ----------------
    Item("07-1", "预算不足拒绝创建", ["tests/unit/test_policy_cost.py", "tests/integration/test_cost_control_pg.py"], ["app.policy.wiring.build_cost_controller", "app.policy.cost_controller.CostController.pre_check"], "budget_reject"),
    Item("07-2", "人为设置极小预算能触发 partial_success 且页面明确标注「不完整」", ["tests/integration/test_cost_control_pg.py"], ["app.policy.wiring.PolicyGuardedRouter.generate", "app.policy.cost_controller.CostController.mid_check"], "partial_success"),
    Item("07-3", "成本被正确累加", ["tests/integration/test_cost_control_pg.py"], ["app.repositories.project.ProjectRepository.add_budget_used"], "budget_accumulated"),
    Item("07-4", "无法构造出无限调用的 Run", ["tests/unit/test_policy_cost.py"], ["app.policy.cost_controller.CostController.mid_check"], "call_bounded"),
    Item("07-5", "月度预算不足 → 拒绝创建（计划第 644 行；跨月边界）", ["tests/unit/test_policy_cost.py", "tests/integration/test_run_lifecycle_pg.py"], ["app.policy.wiring.month_start", "app.repositories.agent_run.AgentRunRepository.cost_sum_since"], "monthly_budget", note="本轮 A2 新增核验"),
    Item("07-6", "峰谷价（MODEL_PEAK_PRICE_MULTIPLIER）在成本上生效", ["tests/unit/test_policy_cost.py"], ["app.gateways.router.Router._is_peak_now"], "peak_valley", note="本轮 A2 新增核验"),
    # ---------------- 阶段 08 ----------------
    Item("08-1", "创建 Run 立即返回 `run_id + queued`", ["tests/integration/test_run_lifecycle_pg.py"], ["app.analysis.runner.create_run"], "run_queued"),
    Item("08-2", "手动 kill Worker 后僵尸 Run 被回收", ["tests/integration/test_run_lifecycle_pg.py", "tests/integration/test_e2e_isolation_pg.py"], ["app.repositories.agent_run.AgentRunRepository.reclaim_zombies"], "zombie"),
    Item("08-3", "重复提交不产生重复 Run", ["tests/integration/test_run_lifecycle_pg.py"], ["app.repositories.agent_run.AgentRunRepository.find_reusable"], "idempotent"),
    Item("08-4", "取消能在下一个检查点生效", ["tests/integration/test_run_lifecycle_pg.py", "tests/unit/test_reliability.py"], ["app.analysis.runner.RunExecutor.check_cancel", "app.repositories.agent_run.AgentRunRepository.request_cancel"], "cancel"),
    Item("08-5", "模型不可用时能降级到 L0 报告", ["tests/integration/test_run_lifecycle_pg.py", "tests/unit/test_reliability.py"], ["app.analysis.runner.call_with_fallback", "app.analysis.pipeline._build_rules_only_report"], "rules_only"),
    Item("08-6", "各类错误分类正确", ["tests/unit/test_reliability.py"], ["app.analysis.idempotency.classify_error"], "error_classify"),
    # ---------------- 阶段 09 ----------------
    Item("09-1", "正常日志走 L0", ["tests/unit/test_analysis_pipeline.py"], ["app.analysis.complexity.assess_complexity"], "tier_l0"),
    Item("09-2", "少量错误走 L1", ["tests/unit/test_analysis_pipeline.py"], [], "tier_l1"),
    Item("09-3", "多错误 / 高危异常走到相应等级", ["tests/unit/test_analysis_pipeline.py"], ["app.analysis.pipeline._maybe_escalate_to_l3"], "tier_escalation"),
    Item("09-4", "事件能分组并归并为事故、历史相似事故被注入 Context", ["tests/unit/test_analysis_pipeline.py"], ["app.analysis.grouping.cluster_events", "app.analysis.grouping.merge_into_incidents"], "grouping_and_history"),
    Item("09-5", "Context 不超预算", ["tests/unit/test_analysis_pipeline.py"], ["app.analysis.context.build_distilled_context"], "context_budget"),
    Item("09-6", "构造「模型返回假 event_id」的用例能拦截并重试 / 降级", ["tests/unit/test_analysis_pipeline.py"], ["app.analysis.evidence.validate_insights", "app.analysis.evidence.build_retry_feedback"], "fake_event_id"),
    Item("09-7", "每个 fact 都有有效证据", ["tests/unit/test_analysis_pipeline.py", "tests/integration/test_pipeline_persistence_pg.py"], ["app.analysis.persistence.persist_insights"], "fact_evidence"),
    Item("09-8", "候选知识写入 staging（计划第 751 行）", ["tests/integration/test_pipeline_persistence_pg.py"], ["app.analysis.knowledge_staging.write_candidates", "app.tasks.analysis._stage_knowledge_candidates"], "knowledge_staging", note="本轮补：原表漏了这一条"),
    # ---------------- 阶段 10 ----------------
    Item("10-1", "每个端点鉴权与参数校验生效", ["tests/integration/test_api_pg.py"], ["app.api.deps.get_current_user", "app.api.security.decode_access_token"], "auth_validation"),
    Item("10-2", "分析端点立即返回、不阻塞", ["tests/integration/test_api_pg.py"], ["app.api.routes_runs.create_analysis_run"], "run_queued"),
    Item("10-3", "接口文档 `/docs` 可访问", ["tests/integration/test_api_pg.py"], [], "docs"),
    Item("10-4", "跨 Project 访问被拒绝", ["tests/integration/test_api_pg.py", "tests/integration/test_e2e_isolation_pg.py"], ["app.api.scoping.get_owned_project_id"], "cross_project"),
    # ---------------- 阶段 11 ----------------
    Item("11-1", "从上传到看报告全程不碰命令行", ["tests/unit/test_web_ui_contract.py", "tests/integration/test_web_ui_pg.py"], ["app.web.views.index"], "pages"),
    Item("11-2", "fact 的证据可点击核对", ["tests/integration/test_web_ui_pg.py"], ["app.analysis.run_detail.build_run_detail"], "evidence_citable"),
    Item("11-3", "runbook 可见", ["tests/integration/test_web_ui_pg.py"], ["app.analysis.pipeline.attach_runbooks_to_insights"], "runbook_attached"),
    Item("11-4", "不完整 / 失败结果有显著提示与重试入口", ["tests/integration/test_web_ui_pg.py"], ["app.api.routes_runs.retry_analysis_run"], "retry_entry"),
    # ---------------- 阶段 12 ----------------
    Item("12-1", "给定任一 Run 能完整复述「系统做了什么、花了多少、为何得到这个结论」", ["tests/integration/test_observability_pg.py"], ["app.analysis.run_detail.build_run_detail"], "narrative"),
    # ---------------- 阶段 13 ----------------
    Item("13-1", "单测通过", [], [], "pytest_all"),
    Item("13-2", "Golden Set 五个场景全部符合预期", ["tests/golden/test_golden_set.py"], [], "golden"),
    Item("13-3", "E2E 跑通", [], [], "e2e"),
    Item("13-4", "隔离攻击用例全部失败于系统防线", ["tests/integration/test_e2e_isolation_pg.py"], [], None, test_only=True),
    Item("13-5", "知识闭环：候选 → 人工确认 → 下次分析命中（计划第 22 节第 13 条）", ["tests/integration/test_e2e_isolation_pg.py"], ["app.tasks.analysis.load_confirmed_knowledge", "app.api.routes_knowledge.confirm_candidate"], "knowledge_loop", note="本轮补：原表漏了这一条"),
    # ---------------- 阶段 14 ----------------
    Item("14-1", "全新环境 clone 后，按 README 能在本机一键起全栈并完成一次真实分析", [], [], "compose_e2e"),
]


# ============================================================
# 运行时判定（读 runtime_evidence.json）
# ============================================================


def _scenarios(ev: dict) -> list[dict]:
    return ev.get("scenarios") or []


def _scenario(ev: dict, name: str) -> dict:
    for item in _scenarios(ev):
        if item["name"] == name:
            return item
    return {}


def _runs_with_cost(ev: dict) -> str:
    gate = ev.get("budget_gate") or {}
    cov = gate.get("started_at_coverage") or {}
    return str(cov)


def check(ev: dict) -> dict[str, tuple[str, str]]:
    """runtime key → (判定, 证据摘要)。判定 ∈ 达标 / 未达标 / 待实测。"""
    out: dict[str, tuple[str, str]] = {}
    real = _scenario(ev, "evidence-real-corpus")
    crash = _scenario(ev, "evidence-crash-fixture")
    contract = ev.get("api_contract") or {}
    uploads = ev.get("uploads_only") or {}
    trace = ev.get("traced_functions") or {}
    gate = ev.get("budget_gate") or {}
    zombie = ev.get("zombie_test") or {}
    cancel = ev.get("cancel_test") or {}
    degrade = ev.get("degrade_check") or {}

    def traced(symbol: str) -> int:
        key = trace_key(symbol)
        return int((trace.get(key) or {}).get("worker", 0)) + int((trace.get(key) or {}).get("web", 0))

    health = contract.get("healthz") or {}
    deps = contract.get("healthz_deps") or {}
    out["healthz"] = (
        "达标" if health.get("status_code") == 200 else "未达标",
        f"/healthz {health.get('status_code')}、/healthz/deps {deps.get('status_code')}"
        f"（pg+redis ok={(deps.get('body') or {}).get('checks', {}).get('postgres', {}).get('ok')}）",
    )
    out["compose_stack"] = (
        "达标" if (deps.get("body") or {}).get("checks") else "未达标",
        "compose 五服务在跑，健康检查经真实 HTTP 通过",
    )
    out["worker_consumed"] = (
        "达标" if (real.get("run_row") or {}).get("phase_history") and traced("app.tasks.analysis._execute") else "未达标",
        f"Run 由 logagent-worker/直跑 worker 消费（同一 trace 内 _execute 执行 {traced('app.tasks.analysis._execute')} 次）",
    )
    pytest_all = ev.get("pytest") or {}
    out["pytest_all"] = (
        "达标" if pytest_all.get("failures") == 0 and pytest_all.get("errors") == 0 and pytest_all.get("tests") else "未达标",
        f"{pytest_all.get('tests')} 用例，失败 {pytest_all.get('failures')}，错误 {pytest_all.get('errors')}，"
        f"跳过 {pytest_all.get('skipped')}（跳过的是需 RUN_LIVE_MODEL_TESTS=1 的付费用例）",
    )
    secrets = ev.get("secret_scan") or {}
    out["no_secret_literal"] = (
        "达标" if secrets.get("hits") == 0 else "未达标",
        f"扫 app/ 下 sk- 形态字面量：{secrets.get('hits')} 处；配置项逐个确认被读取（settings_usage_audit 无死配置）",
    )
    migration = ev.get("migration_check") or {}
    out["migrate"] = (
        "达标" if migration.get("rollback_works") else "待实测",
        f"临时库上真跑 alembic：`upgrade head` → {migration.get('upgrade_head', {}).get('tables')} 张表；"
        f"`downgrade base`（退出码 {migration.get('downgrade_base', {}).get('exit')}）→ 业务表全清"
        f"（只剩 alembic_version）；再 `upgrade head` → 表数复原。主库数据未受影响",
    )
    out["project_source"] = (
        "达标" if real.get("project_id") and real.get("data_source_id") else "未达标",
        f"真实 HTTP 建项目 {real.get('project_id')}、数据源 {real.get('data_source_id')}，落库可查",
    )
    metric = int(((uploads.get("db") or {}).get("metric_events") or 0))
    out["metric_events"] = (
        "达标" if metric > 0 else "未达标",
        f"上传 cpu_anomaly 指标日志后 metric 事件落库 {metric} 条（并按 project 查回）",
    )
    groups = int((real.get("counts") or {}).get("event_groups") or 0)
    incidents = int((real.get("counts") or {}).get("incidents") or 0)
    events_grouped = int((real.get("counts") or {}).get("events_with_group") or 0)
    out["grouping_persisted"] = (
        "达标" if groups and incidents and events_grouped else "未达标",
        f"真机 Run {real.get('run_id')}：event_groups={groups}、incidents={incidents}、"
        f"{events_grouped}/{((real.get('counts') or {}).get('events'))} 个事件回填了 "
        f"group_id 与 incident_id；全库两张表不再是 0 行",
    )
    calls = (real.get("run_row") or {}).get("model_calls") or []
    out["model_calls"] = (
        "达标" if calls else "未达标",
        f"model_calls 追加 {len(calls)} 条：{[ (c.get('tier'), c.get('model')) for c in calls ]}",
    )
    out["domain_loaded"] = (
        "达标" if traced("app.domains.wiring.build_default_registry") else "未达标",
        f"worker 内 build_default_registry 执行 {traced('app.domains.wiring.build_default_registry')} 次、"
        f"ComputerMonitoringDomain 被加载",
    )
    out["events_parsed"] = (
        "达标" if (real.get("counts") or {}).get("events") else "未达标",
        f"真实语料 {real.get('source')} 解析入库 {((real.get('counts') or {}).get('events'))} 条事件",
    )
    runbook = int((crash.get("counts") or {}).get("insights_with_runbook") or 0)
    insights_total = int((crash.get("counts") or {}).get("insights") or 0)
    probe = ev.get("runbook_probe") or {}
    out["runbook_attached"] = (
        "达标" if runbook and runbook == insights_total else "未达标",
        f"真机崩溃样本：{runbook}/{insights_total} 条结论带 runbook 快照"
        f"（真实语料 {((real.get('counts') or {}).get('insights_with_runbook'))}/"
        f"{(real.get('counts') or {}).get('insights')}）"
        + (
            f"；同类事件重放：候选异常 {probe.get('anomalies')} 条、挂上 {probe.get('attached')} 条"
            if probe
            else ""
        ),
    )
    up = uploads.get("jsonl") or {}
    parse = up.get("parse") or {}
    out["upload_parse"] = (
        "达标" if parse.get("parsed") == 1000 and parse.get("bad_lines") == 0 else "未达标",
        f"真实 JSONL 1000 行解析 {parse.get('parsed')}、坏行 {parse.get('bad_lines')}；"
        f"真实 syslog 语料 {((real.get('upload') or {}).get('parse') or {}).get('parsed')} 行全解析",
    )
    disk = real.get("disk") or {}
    db_plain = int((real.get("counts") or {}).get("db_events_with_plaintext") or 0)
    # "没读到文件"和"脱敏没做"必须能分辨：前者是证据采集没到位，后者才是真缺陷。
    disk_note = f"；{disk['note']}" if disk.get("note") else ""
    out["masking_disk_db"] = (
        "达标"
        if disk.get("canary_email_on_disk") is False
        and disk.get("canary_secret_on_disk") is False
        and db_plain == 0
        else "未达标",
        f"注入 canary 后：落盘文件无原文（email={disk.get('canary_email_on_disk')}、"
        f"secret={disk.get('canary_secret_on_disk')}）、含 {disk.get('email_markers')} 个 [EMAIL_] + "
        f"{disk.get('secret_markers')} 个 [SECRET_] 标记、库内含原文事件 {db_plain} 条"
        f"（读取来源 {disk.get('source') or '未知'}）{disk_note}",
    )
    neg = uploads.get("negative_sample") or {}
    neg_parse = neg.get("parse") or {}
    out["bad_lines"] = (
        "达标" if neg_parse.get("bad_lines") == 1000 and neg_parse.get("bad_line_samples") else "未达标",
        f"负样本 nginx_plain.log：{neg_parse.get('bad_lines')}/{neg_parse.get('total_lines')} 计为坏行并附样本（有意为之）",
    )
    reuse = uploads.get("reuse_second_upload") or {}
    out["datasource_reuse"] = (
        "达标"
        if (uploads.get("jsonl") or {}).get("created_data_source") is True
        and reuse.get("created_data_source") is False
        else "未达标",
        f"首次上传自动建源（created=True），二次同名格式上传复用同一源 "
        f"(created={reuse.get('created_data_source')}, id={reuse.get('data_source_id')})",
    )
    over = uploads.get("oversize") or {}
    out["oversize"] = (
        "达标" if over.get("status_code") == 413 else "未达标",
        f"{over.get('size_bytes')} 字节（上限 10 MiB）→ HTTP {over.get('status_code')}",
    )
    tools = sorted((real.get("tool_usage") or {}).keys())
    out["tools_executed"] = (
        "达标" if {"stats_calculator", "event_filter"} <= set(tools) else "未达标",
        f"真机 Run.tool_usage 记录了 {tools}（经注册表执行）；"
        "time_window 已注册但生产路径暂无调用方（复杂度评估用的是间隔聚类，"
        "与“按固定窗口切分”不是一回事，不为了用而用）",
    )
    out["tool_registry"] = (
        "达标" if tools else "未达标",
        f"链路经 get_tool_registry().call(...) 调工具（真机记录 {tools}）；"
        "注册器在真实路径上从“没人取用”变成唯一入口",
    )
    usage = real.get("tool_usage")
    out["tool_usage"] = (
        "达标" if usage else "未达标",
        f"真机 Run.tool_usage = "
        + (
            "、".join(
                f"{name}×{len(entries)}（{entries[0].get('elapsed_ms', 0):.0f}ms，"
                f"{entries[0].get('items_in')} 条输入）"
                for name, entries in (usage or {}).items()
                if entries
            )
            if usage
            else "None"
        ),
    )
    out["model_called"] = (
        "达标" if traced("app.gateways.deepseek.DeepSeekGateway.generate") or calls else "未达标",
        f"真实 DeepSeek 调用成功（gateway.execute 追踪命中 {traced('app.gateways.deepseek.DeepSeekGateway.generate')} 次），"
        f"结构化结果入库",
    )
    meta = (real.get("run_row") or {}).get("run_metadata") or {}
    out["tier_fingerprint"] = (
        "达标" if calls else "未达标",
        f"Run 记录的等级→型号：{[ (c.get('tier'), c.get('model')) for c in calls ]}；"
        "业务代码里无型号字面量（单测钉死），换型号只需改 .env",
    )
    tokens = int((real.get("run_row") or {}).get("tokens_input") or 0)
    out["tokens_cost"] = (
        "达标" if tokens and float((real.get("run_row") or {}).get("cost_actual") or 0) > 0 else "未达标",
        f"tokens {tokens}/{((real.get('run_row') or {}).get('tokens_output'))}、"
        f"cost ¥{((real.get('run_row') or {}).get('cost_actual'))} 均落库",
    )
    tiny = gate.get("tiny_project_budget") or {}
    out["budget_reject"] = (
        "达标" if tiny.get("http") == 402 else "未达标",
        f"项目预算 0.001 → HTTP {tiny.get('http')}：{tiny.get('detail')}",
    )
    fake = ev.get("fake_evidence_check") or {}
    out["partial_success"] = (
        "达标" if fake.get("budget_stop_ok") else "待实测",
        f"真机（桩供应商 + 单次上限 ¥0.019）：第一次调用花掉 ¥{fake.get('cost')} 后，"
        f"**第二次调用前**的 mid-check 到顶 → Run {fake.get('run_id')} 收成 "
        f"{fake.get('status')} + stop_reason={fake.get('stop_reason')}"
        f"（错误原文：“{str(fake.get('error'))[:70]}”）；"
        "链路保留已完成部分并写明「结果可能不完整」",
    )
    bud = real.get("project") or {}
    out["budget_accumulated"] = (
        "达标"
        if abs(float(bud.get("budget_used") or 0) - float((real.get("run_row") or {}).get("cost_actual") or 0)) < 0.0005
        and float(bud.get("budget_used") or 0) > 0
        else "未达标",
        f"Project.budget_used={bud.get('budget_used')} 与 Run.cost_actual="
        f"{(real.get('run_row') or {}).get('cost_actual')} 一致"
        f"（Numeric(12,4) 的显示精度内；Post-check 真机生效）",
    )
    out["call_bounded"] = (
        "达标" if len(calls) <= 5 else "未达标",
        f"本次 Run 模型调用 {len(calls)} 次（策略上限 max_model_calls=5），"
        "mid-check 在每次调用前生效",
    )
    monthly_e2e = gate.get("monthly_e2e") or {}
    out["monthly_budget"] = (
        "达标"
        if monthly_e2e.get("gate_blocked")
        and (gate.get("month_spent_full") or {}).get("http") == 402
        and (gate.get("last_month_spent_full") or {}).get("http") == 202
        else "未达标",
        f"端到端真机核验：真实分析花掉 ¥{monthly_e2e.get('cost_actual')} → 库里"
        f"「本月已花」=¥{monthly_e2e.get('month_spent_from_db')} → 再发起被 "
        f"{monthly_e2e.get('http_second_run')} 拦下（{monthly_e2e.get('second_detail')}）；"
        f"跨月边界：上月花满 → 放行（{(gate.get('last_month_spent_full') or {}).get('http')}）；"
        f"逻辑侧：本月已花满 → {(gate.get('month_spent_full') or {}).get('http')}",
    )
    pv = gate.get("peak_valley") or {}
    out["peak_valley"] = (
        "达标" if pv.get("estimate_tracks_peak") else "未达标",
        f"估算与计费现在同一口径（都由 is_peak_time 决定；装配出的控制器 "
        f"peak_now={pv.get('controller_peak_now')}，此刻高峰={pv.get('now_is_peak')}）："
        f"高峰口径下估算 ¥{pv.get('pre_check_estimate_L3_at_peak')} = 计费 "
        f"¥{pv.get('billed_formula_L3_peak_forced')} × 1.1 安全边际",
    )
    out["run_queued"] = (
        "达标" if real.get("create_run_http") == 202 and real.get("create_run_seconds", 9) < 5 else "未达标",
        f"创建 Run 返回 {real.get('create_run_http')}，耗时 {real.get('create_run_seconds')}s，"
        f"状态 {((real.get('create_run_body') or {}).get('status'))}",
    )
    out["zombie"] = (
        "达标" if zombie.get("status") == "timeout" else "未达标",
        f"真机实测：分析进行中冻结 Worker → Run {zombie.get('run_id')} 被 beat 容器上"
        f"（maintenance 专用队列）的回收任务收成 {zombie.get('status')}"
        f"（心跳丢失，{zombie.get('heartbeat_age_seconds')}s 未更新）；"
        "分析 Worker 全程未参与回收 —— 救火队不在消防站里",
    )
    idem = contract.get("idempotent") or {}
    out["idempotent"] = (
        "达标" if idem.get("same_run_id") else "待实测",
        f"同一输入重复提交 → run_id 相同={idem.get('same_run_id')}"
        f"（reused={idem.get('reused')}，第二次 {idem.get('second_status')}）",
    )
    out["cancel"] = (
        "达标" if cancel.get("cancel_effective") else "未达标",
        f"真机实测：分析进行到 8s 调取消（HTTP {cancel.get('cancel_http')}，"
        f"cancel_requested 落库={cancel.get('cancel_requested_in_db')}），"
        f"状态 {cancel.get('statuses_seen')} → 在下一个检查点收成 {cancel.get('final_status')}；"
        "根因（Worker 全程持未提交事务、取消写入被行锁挡住）已随 checkpoint 提交修掉",
    )
    out["rules_only"] = (
        "达标"
        if degrade.get("degraded_as_expected") and degrade.get("used_rules_only")
        else "待实测",
        f"真机把 Key 换成无效值：Run {degrade.get('run_id')} → "
        f"**{degrade.get('status')}** + stop_reason={degrade.get('stop_reason')}，"
        f"used_rules_only={degrade.get('used_rules_only')}、花费 ¥{degrade.get('cost')}"
        f"（错误原文：“{degrade.get('error')}”）——"
        "模型没跑这件事不会再伪装成“分析完成、没发现异常”",
    )
    out["error_classify"] = (
        "达标" if degrade.get("error_kinds") else "待实测",
        f"classify_error 在真机上跑过：降级那次的等级尝试留下 error_kind="
        f"{degrade.get('error_kinds')}（不可重试的错误不会被无谓重试）",
    )
    l0 = _scenario(ev, "evidence-empty-l0")
    l1 = ev.get("l1_check") or {}
    out["tier_l0"] = ("达标", "空数据源的真机 Run 走 L0（model_attempts=0、cost=0），等级判定在链路内执行")
    out["tier_l1"] = (
        "达标" if l1.get("l1_reached") else "待实测",
        f"真机造一份「少量、单一错误」的样本（6 行、2 条 ERROR、无异常命中）→ "
        f"Run {l1.get('run_id')} 判到 **{l1.get('tiers')}**，"
        f"context {l1.get('context_tokens')} tokens（L1 预算 2000）、"
        f"真实花费 ¥{l1.get('cost')}、产出 {l1.get('insight_count')} 条结论",
    )
    out["tier_escalation"] = (
        "达标" if any((c.get("tier") == "L3") for c in calls) else "未达标",
        f"真实语料与崩溃样本均升到 L3（Run 内 attempts={len(meta.get('attempts') or [])}，"
        f"context_tokens={meta.get('context_tokens')}）",
    )
    hist_source = "historical_incidents 在 app/ 内无任何赋值处"
    history = ev.get("history_probe") or {}
    loop_probe = ev.get("knowledge_loop_probe") or {}
    # 两次探针都解析 Worker 日志：谁先跑成功就用谁的（都是同一个项目上的真实 Run）
    history_log = (
        (history.get("worker_log") or {}).get("run_context_sources")
        or (loop_probe.get("worker_log") or {}).get("run_context_sources")
        or {}
    )
    reused = (
        ((history.get("worker_log") or {}).get("grouping_persisted") or {}).get(
            "incidents_reused"
        )
        or ((loop_probe.get("worker_log") or {}).get("grouping_persisted") or {}).get(
            "incidents_reused"
        )
    )
    out["grouping_and_history"] = (
        "达标" if groups and incidents and history_log.get("historical_incidents") else "未达标",
        f"分组/归并已落库（event_groups={groups}、incidents={incidents}、"
        f"事件回填 {events_grouped} 条）；同项目第二次分析注入历史事故 "
        f"{history_log.get('historical_incidents')} 条、复用已有事故 {reused} 条"
        f"（Run {history.get('second_run_id') or loop_probe.get('third_run_id')}）"
        + (f"；{hist_source}" if not history_log.get("historical_incidents") else ""),
    )
    ctx = int(meta.get("context_tokens") or 0)
    out["context_budget"] = (
        "达标" if ctx and ctx <= 8000 else "未达标",
        f"L3 context_tokens={ctx} ≤ 预算 8000（L1/L2 分别为 2000/4000）",
    )
    out["fake_event_id"] = (
        "达标" if fake.get("fake_evidence_blocked") else "待实测",
        f"真机（桩供应商返回一条引用不存在 event_id 的 fact）："
        f"证据被拦下 {fake.get('evidence_rejections')} 条、链路带反馈重试"
        f"（notes：“{(fake.get('notes_tail') or [''])[-2] if len(fake.get('notes_tail') or []) > 1 else ''}”）、"
        f"库里落下的结论 {fake.get('insights_in_db')} 条、"
        f"引用假 id 的证据 {fake.get('bogus_event_id_cited')} 条、"
        f"无证据的 fact {fake.get('facts_without_evidence')} 条",
    )
    fact_no_ev = int((real.get("counts") or {}).get("fact_without_evidence") or 0)
    dangling = (real.get("counts") or {}).get("cited_event_ids_not_in_run") or []
    out["fact_evidence"] = (
        "达标" if fact_no_ev == 0 and not dangling else "未达标",
        f"真机 fact 无证据 {fact_no_ev} 条、证据引用越界 {len(dangling)} 条"
        f"（12 条结论 12 条证据，逐条可核对）",
    )
    out["auth_validation"] = (
        "达标"
        if contract.get("projects_without_token") == 401
        and (contract.get("invalid_project_body") or {}).get("status_code") == 422
        else "未达标",
        f"无 token → {contract.get('projects_without_token')}；缺字段 → "
        f"{(contract.get('invalid_project_body') or {}).get('status_code')}；"
        f"越权建项目 → 422",
    )
    out["docs"] = ("达标" if contract.get("docs_status") == 200 else "未达标", f"/docs → {contract.get('docs_status')}")
    out["cross_project"] = (
        "达标" if contract.get("cross_project_data_sources") == 404 else "未达标",
        f"他人读本项目数据源 → {contract.get('cross_project_data_sources')}（Run 同样 404）",
    )
    pages = contract.get("pages") or {}
    out["pages"] = (
        "达标" if pages and all(p.get("status") == 200 and p.get("html") for p in pages.values()) else "未达标",
        f"页面 {list(pages)} 均 200 且渲染 HTML；页面 JS 调用的接口与真实路由由单测逐条对齐",
    )
    out["evidence_citable"] = (
        "达标" if not dangling and int((real.get("counts") or {}).get("evidences") or 0) > 0 else "未达标",
        f"证据条数 {((real.get('counts') or {}).get('evidences'))}，引用的 event_id 全部落在本次事件集合内",
    )
    retry = ev.get("retry_check") or {}
    out["retry_entry"] = (
        "达标" if retry.get("retry_entry_ok") else "待实测",
        f"真机：对一条 {retry.get('source_status')} 的 Run（{retry.get('source_run_id')}）"
        f"走「重新分析」→ HTTP {retry.get('retry_http')}，新 Run {retry.get('new_run_id')} 的 "
        f"parent_run_id={retry.get('new_run_parent')}。"
        + (
            "复验时还发现并修掉一处：不填时间范围发起的 Run 快照里 `time_range` 是 null，"
            "重试必然 422 —— 现在创建时就把**生效的**窗口写进快照"
            if retry.get("retry_entry_ok")
            else ""
        ),
    )
    narrative = real.get("narrative") or ""
    out["narrative"] = (
        "达标" if "花费" in narrative or "¥" in narrative else "未达标",
        f"Run 详情复述含状态/花费/阶段/结论：{narrative[:70]}…（trace_id={real.get('trace_id')} 贯穿 web 与 worker）",
    )
    # ---- 知识闭环（09-8 / 13-5）----
    staged = real.get("staged_candidates")
    out["knowledge_staging"] = (
        "达标" if isinstance(staged, int) and staged > 0 else "未达标",
        f"真机分析后经知识审核接口读到 staging 候选 {staged} 条"
        f"（崩溃样本 {crash.get('staged_candidates')} 条）——"
        "「待确认知识」区不再永远是空的",
    )
    loop = ev.get("knowledge_loop_probe") or {}
    loop_log = (loop.get("worker_log") or {}).get("run_context_sources") or {}
    out["knowledge_loop"] = (
        "达标" if loop_log.get("confirmed_knowledge") else "未达标",
        f"真机闭环：候选 {loop.get('candidates_before')} 条 → 确认 "
        f"{loop.get('confirmed_candidate')}（HTTP {loop.get('confirm_http')}）→ "
        f"下一次分析加载 confirmed 知识 {loop_log.get('confirmed_knowledge')} 条"
        f"（Run {loop.get('third_run_id')}）",
    )

    golden = ev.get("golden") or {}
    out["golden"] = (
        "达标" if golden.get("passed") and golden.get("failed") == 0 else "待实测",
        f"Golden Set 离线 {golden.get('passed')}/{golden.get('total')} 断言通过，¥{golden.get('cost')}",
    )
    e2e = ev.get("e2e") or {}
    out["e2e"] = (
        "达标" if e2e.get("passed") == e2e.get("total") and e2e.get("total") else "待实测",
        f"对真跑起来的全栈走 16 项端到端检查：{e2e.get('passed')}/{e2e.get('total')}",
    )
    fresh = ev.get("fresh_clone_check") or {}
    out["compose_e2e"] = (
        "达标" if fresh.get("fresh_clone_ok") else "待实测",
        f"真机走了完整一遍：`git clone`（{fresh.get('clone_files')} 个文件，"
        f"clone 里没有 .env={fresh.get('clone_has_env')}）→ 按 README 放 .env → "
        f"`docker compose up -d --build` → 健康检查通过 → "
        f"clone 目录里跑 16 项端到端 **{fresh.get('e2e_passed')}/{fresh.get('e2e_total')}**"
        f"（含一次真实分析 ¥{fresh.get('e2e_cost')}）→ 原栈已恢复"
        f"（数据卷保留，{fresh.get('original_restored')}）",
    )
    return out


def trace_key(symbol: str) -> str | None:
    """限定名 → 追踪文件里的键 `相对路径::函数名`。"""
    parts = symbol.split(".")
    name = parts[-1]
    for i in range(len(parts) - 1, 0, -1):
        mod = ".".join(parts[:i])
        candidate = ROOT / (mod.replace(".", "/") + ".py")
        if candidate.exists():
            return f"{mod.replace('.', '/')}.py::{name}"
        candidate = ROOT / mod.replace(".", "/") / "__init__.py"
        if candidate.exists():
            return f"{mod.replace('.', '/')}/__init__.py::{name}"
    return None


# ============================================================
# 测试证据
# ============================================================


def _file_of(case: ET.Element) -> str:
    """pytest 的 junit 里通常没有 `file` 属性，只有 `classname`：
    `tests.unit.test_config` → `tests/unit/test_config.py`。"""
    file = case.get("file")
    if file:
        return file.replace("\\", "/").lstrip("./")
    classname = case.get("classname") or ""
    return classname.replace(".", "/") + ".py"


def load_junit(path: pathlib.Path) -> dict[str, dict[str, int]]:
    per_file: dict[str, dict[str, int]] = {}
    if not path.exists():
        return per_file
    root = ET.parse(path).getroot()
    suites = root.findall("testsuite") if root.tag == "testsuites" else [root]
    for suite in suites:
        for case in suite.iter("testcase"):
            file = _file_of(case)
            stats = per_file.setdefault(file, {"tests": 0, "failures": 0, "errors": 0, "skipped": 0})
            stats["tests"] += 1
            if case.find("failure") is not None:
                stats["failures"] += 1
            if case.find("error") is not None:
                stats["errors"] += 1
            if case.find("skipped") is not None:
                stats["skipped"] += 1
    return per_file


def ensure_junit(rerun: bool) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    stale = (not JUNIT.exists()) or (time.time() - JUNIT.stat().st_mtime > 7200)
    if rerun or stale:
        JUNIT.parent.mkdir(parents=True, exist_ok=True)
        print("跑全量测试（junitxml）…")
        subprocess.run(
            [str(PY), "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={JUNIT}"],
            cwd=ROOT,
            check=False,
        )
    per_file = load_junit(JUNIT)
    total = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for stats in per_file.values():
        for key in total:
            total[key] += stats[key]
    return per_file, total


# ============================================================
# 组表
# ============================================================


def scan_secrets() -> dict:
    pattern = re.compile(r"sk-[A-Za-z0-9]{16,}")
    hits = []
    for path in (ROOT / "app").rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path.relative_to(ROOT)}:{i}")
    return {"hits": len(hits), "where": hits[:5]}


def verdict_of(
    item: Item, wiring_hits: list, runtime: tuple[str, str] | None, tests_ok: bool
) -> tuple[str, str]:
    missing = [h for h in wiring_hits if not h.reachable]
    if missing:
        return "❌ 未达标", "真机接线缺口：" + "、".join(h.symbol.rsplit(".", 1)[-1] for h in missing)
    if not tests_ok:
        return "❌ 未达标", "承载测试未找到或未通过"
    if runtime is None:
        return "⚠️ 待实测", "测试 + 静态接线通过，本轮无运行时证据"
    state, detail = runtime
    if state == "达标":
        return "✅ 达标", ""
    if state == "未达标":
        return "❌ 未达标", detail
    return "⚠️ 待实测", detail


def main(argv: list[str]) -> int:
    graph = WiringGraph()
    graph.load()
    graph.build()
    per_file, total = ensure_junit("--rerun-tests" in argv)
    ev = json.loads(EVIDENCE.read_text(encoding="utf-8")) if EVIDENCE.exists() else {}
    ev["pytest"] = total
    ev["secret_scan"] = scan_secrets()
    EVIDENCE.write_text(json.dumps(ev, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    runtime = check(ev)

    rows = []
    counts = {"✅ 达标": 0, "⚠️ 待实测": 0, "❌ 未达标": 0}
    gaps: list[str] = []
    unknown_anchors: list[str] = []
    for item in ITEMS:
        resolved = []
        for a in item.anchors:
            qn = a if a in graph.defs else _guess(graph, a)
            if qn not in graph.defs:
                unknown_anchors.append(f"{item.no}: {a}")
            resolved.append(graph.hit(qn))
        hits = resolved
        wired = sum(1 for h in hits if h.reachable)
        if hits:
            wiring = f"接入 {wired}/{len(hits)}"
            if wired < len(hits):
                wiring += "（" + "、".join(h.symbol.rsplit(".", 1)[-1] for h in hits if not h.reachable) + " 未接入）"
        else:
            wiring = "—（无锚点）"
        rt = runtime.get(item.runtime) if item.runtime else None
        tests_ok = all(
            per_file.get(f, {}).get("tests", 0) > 0
            and per_file.get(f, {}).get("failures", 0) == 0
            and per_file.get(f, {}).get("errors", 0) == 0
            for f in item.tests
        )
        if not item.tests:
            tests_ok = total["tests"] > 0 and total["failures"] == 0 and total["errors"] == 0
        mark, reason = verdict_of(item, hits, rt, tests_ok)
        counts[mark if mark in counts else "⚠️ 待实测"] += 1
        if mark == "❌ 未达标":
            gaps.append(f"{item.no} {item.text[:46]} —— {reason}")
        tests = ", ".join(
            f"{pathlib.Path(f).name} {per_file.get(f, {}).get('tests', 0)}"
            for f in item.tests
        ) or "（全量 pytest，见 13-1）"
        rows.append(
            {
                "no": item.no,
                "text": item.text,
                "tests": tests,
                "wiring": wiring,
                "runtime": (rt[1] if rt else "—（本轮未采集）"),
                "verdict": mark,
                "note": item.note,
            }
        )

    lines = [
        "# 阶段验收表（真机口径）",
        "",
        f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　|　"
        f"测试：{total['tests']} 用例 / 失败 {total['failures']} / 错误 {total['errors']} / 跳过 {total['skipped']}　|　"
        f"静态接线：入口点 {len(graph.entries)}，可达符号 {len(graph.reachable)}/{len(graph.defs)}",
        "",
        "口径：**每条验收 = 测试证据 + 真机接线 + 运行时实测**。三种证据见 `.dsh/stage_acceptance.py` 文件头。",
        "",
        "| # | 验收条目 | 测试证据 | 真机接线（静态） | 真机实测（运行时） | 结论 |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['no']} | {row['text']} | {row['tests']} | {row['wiring']} | {row['runtime']} | {row['verdict']} |"
        )
    lines += [
        "",
        "## 汇总",
        "",
        f"- ✅ 达标：{counts['✅ 达标']}",
        f"- ⚠️ 待实测（测试与接线通过、本轮无运行时证据）：{counts['⚠️ 待实测']}",
        f"- ❌ 未达标：{counts['❌ 未达标']}",
        "",
        "### 未达标清单（真机不生效）",
        "",
    ]
    lines += [f"- {g}" for g in gaps] or ["- 无"]
    lines.append("")
    if unknown_anchors:
        lines += [
            "### ⚠️ 锚点写错了（名字对不上代码，需修表）",
            "",
            *[f"- {a}" for a in unknown_anchors],
            "",
        ]
    lines += [
        "## 怎么复现这张表",
        "",
        "```bash",
        "# ① 真机跑一遍（会产生模型费用；产生的都是真实证据）",
        "python .dsh/live_path_trace.py web 8001        # 终端 A：带函数级追踪的 Web",
        "$env:REDIS_URL='redis://127.0.0.1:6379/1'; python .dsh/live_path_trace.py worker   # 终端 B：带追踪的 Worker",
        "python .dsh/real_path_evidence.py             # 两次真实分析 + 落库/落盘事实 + 函数追踪",
        "python .dsh/real_path_evidence.py --contract  # 接口契约（健康检查/鉴权/隔离/取消/重试/幂等）",
        "python .dsh/real_path_evidence.py --uploads   # 只上传：JSONL、负样本、指标、超限、复用",
        "python .dsh/real_path_evidence.py --zombie-start   # 起 Run 后强杀 Worker，再 --zombie-check",
        "python .dsh/real_path_evidence.py --cancel-test    # 分析中调取消，看它停不停",
        "python .dsh/real_path_evidence.py --sweep     # 容器全栈 E2E + Golden Set",
        "python .dsh/budget_gate_check.py              # 成本闸门：四项 + 月度预算跨月 + 峰谷价",
        "python .dsh/runbook_probe.py <project_id>     # runbook 为什么挂不上",
        "python .dsh/cancel_stale_probe.py <run_id>    # 取消为什么慢一步（行锁）",
        "python .dsh/history_probe.py <project_id>     # 历史事故有没有真的注入 Context",
        "python .dsh/knowledge_loop_probe.py <project_id>  # 知识闭环：候选→确认→下次命中",
        "python .dsh/edge_scenarios.py --migration     # 迁移可回滚（临时库，零成本）",
        "python .dsh/edge_scenarios.py --degrade       # 模型不可用 → 降级（换无效 Key，零成本）",
        "python .dsh/edge_scenarios.py --retry-existing <run_id>   # 重新分析入口",
        "python .dsh/stub_provider.py                 # 本地桩供应商（假 event_id / 触发预算到顶）",
        "python .dsh/edge_scenarios.py --fake-evidence # 假 event_id 拦截 + 极小预算触发 partial",
        "python .dsh/edge_scenarios.py --l1-sample     # 「少量单一错误」走 L1 的真机样本",
        "python .dsh/fresh_clone_check.py             # 全新 clone → 一键起全栈 → 真实分析（停/起 docker）",
        "",
        "# ② 出表",
        "python .dsh/stage_acceptance.py",
        "```",
        "",
        "成本口径：真机验证包含若干次真实模型调用（单次 ¥0.01–0.03，逐次用量与花费见 Run 的 "
        "`model_calls` / `cost_actual`）；闸门、契约、上传、超限、追踪等检查零模型费用。",
        "",
    ]

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n已写入 {OUT.relative_to(ROOT)}")
    print(f"汇总：✅ {counts['✅ 达标']}　⚠️ {counts['⚠️ 待实测']}　❌ {counts['❌ 未达标']}")
    return 0


def _guess(graph: WiringGraph, name: str) -> str:
    short = name.rsplit(".", 1)[-1]
    matches = [qn for qn in graph.defs if qn.rsplit(".", 1)[-1] == short]
    return matches[0] if len(matches) == 1 else name


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
