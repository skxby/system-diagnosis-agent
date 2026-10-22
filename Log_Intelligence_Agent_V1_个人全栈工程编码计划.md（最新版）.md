# Log Intelligence Agent Platform
## V1 个人全栈工程编码计划

> 这份文档是给「一个人、全栈、要把东西真正做出来并跑起来」的工程施工图。
> 照着阶段顺序做：一个阶段 → 编码 → 测试 → Git Commit → 下一阶段。
> 原则：V1 能运行、可信、可观测、成本可控；不为未来版本提前写复杂代码，但保留最小的扩展缝。
>
> **本文件是「V1 版本的详细施工计划」。项目总纲、跨版本路线与新窗口接续指引，见《Log_Intelligence_Agent_总工程文档.md》。**

---

# 0. 项目定位

这是一个**产品/设备日志智能分析 Agent 平台**。

不是通用聊天 Agent，也不是日志解析脚本。它做一件事：

```
用户上传日志 → 解析 → 标准化事件 → 指标 → 规则/异常检测
            → 按复杂度选择模型等级 → 模型分析
            → Insight（结论）→ Evidence（证据）→ Report（报告）
```

核心信条：

```
让代码处理确定性，让小模型处理简单智能，让大模型处理真正困难的推理。
```

V1 只做一个领域、两种格式、默认只读。先把一条链路打穿，再谈扩展。

---

# 1. V1 范围（明确且克制）

| 项 | V1 内容 |
|---|---|
| 领域 | 仅 `computer_monitoring`（系统/服务器监控日志） |
| 格式 | TXT（syslog / dmesg 风格）、JSONL；CSV / JSON 后移 |
| 权限 | 只读：不修改设备、不执行命令、不删数据 |
| 用户 | 极简账号；自用可只建一个默认账号，不做角色系统 |
| 数据模型 | **9 张表**（见阶段 02） |
| 知识沉淀 | 每次真实异常 → 候选知识条目 → 人工确认 → 入库复用（YAML，不建知识表，见 8.1） |
| 事故记忆 | 事件分组（EventGroup）+ 事故归并（Incident），跨 Run 可检索（见阶段 02 / 09） |
| 部署 | Docker Compose 一键起，单机即可 |
| 规模假设 | 单文件 MB 级、单次分析 ≤ 约 1 万事件；更大的文件明确提示缩小范围 |

选 `computer_monitoring` 的理由：格式相对标准、异常类型可预测（CPU 峰值 / 内存增长 / 磁盘满 / 进程崩溃）、无行业合规门槛，适合验证架构与核心假设。

---

# 2. 版本路线（只看方向，不在 V1 实现）

```
V1   被动分析：用户上传，系统出带证据的报告（本文档）
V1.5 主动分析：定时任务、历史 Baseline、自动分片聚合、通知、知识命中计数与自动强化、
              MCP Server / CLI 分发、文件级 Domain 扩展
V2   单源因果：趋势分析、时间相关性、模式识别、调查计划、连接器扩展、领域 embedding
V2.5 Multi-Agent：仅当单 Agent 确实扛不住时才引入（触发指标见总工程文档）
V3   受控执行：Action、审批、权限、回滚、审计；分析与执行严格分离；UI 无代码自建 Domain
```

---

# 3. V1 总体架构

```text
Browser (极简页面)
  ↓
FastAPI（鉴权 / 验证 / 创建任务 / 返回状态）
  ↓
Celery Worker（真正的分析在这里）
  │
  ├── Domain Registry      （领域「目录包」插件：parser / analyzers / knowledge / runbooks / config）
  ├── Tool Registry        （通用数据算子：统计 / 过滤 / 时间窗口，跨领域复用）
  ├── Model Gateway        （统一 generate()，不感知供应商）
  ├── Model Router         （L0–L3 等级，型号由配置映射）
  ├── Policy Engine        （策略：调用/工具/超时上限）
  └── Cost Controller      （预算三检查点）
  │
  ↓
Data Pipeline：读取 → 脱敏 → 解析 → 标准化 → 指标事件
  ↓
Analysis：模板聚类/事件分组 → 规则/知识命中 → 异常检测（文件内）→ 事故归并
        → 复杂度评估 → Context 蒸馏（含历史相似事故）→ 模型分析（同时产出候选知识）
  ↓
Evidence 校验（event_id 子集校验、幻觉拦截）
  ↓
Insight / Report（四层语义：事实 / 推断 / 可能 / 未知；命中异常附 runbook）
  ↓
候选知识 → 待确认（staging）→ 人工审核 → confirmed 知识库
  ↓
AgentRun 记录（状态、耗时、模型调用、成本、Trace ID）
```

关键约束（不要违反）：

```
❌ Log ───────────→ LLM
✅ Log → 结构化 → 确定性分析 → 蒸馏 Context → LLM
```

---

## 3.1 垂直能力七层模型（L0–L7）

这一节回答「垂直能力到底由什么构成」。它基于对 **11 个开源平台 / 11 个商业产品 / 6 家被市场验证的垂直 Agent 公司 / HN·Reddit·知乎·V2EX 一手社区声音**的交叉调研，用来修正「垂直能力 = 知识沉淀 + 真实使用」这个过于单薄的说法。

三个前提结论：

1. **LLM 本身几乎从不构成护城河**：最有钱的法律 AI Harvey（估值 $15.5B）自研模型，在自家 BigLaw Bench 上被 Claude / Gemini / GPT 击败后放弃自研、转多模型路由；Sierra 团队原话「最难的早已不是模型，而是围绕模型的一切」。
2. **「用着变强」是飞轮的输出，不是飞轮的零件**：它必须落到具体的物理载体（事件分组、事故记忆库、回流、golden set），不能只当口号。
3. **越靠下越硬、越不依赖 LLM、越是个人开发者能做**——个人的机会在下面几层，不在模型。

| 层 | 名称 | 它回答什么 | V1 决策 |
|---|---|---|---|
| **L0** | 数据接入与解析 | 日志「喂得进去」吗：定死事件 Schema、单领域解析到极致、模板聚类去重 | ✅ 现在做 |
| **L1** | 确定性检测 | 不靠 LLM 算数能发现异常吗：手写 analyzer、文件内 Z-score / 环比 | ✅ 现在做 |
| **L2** | 可执行工作流 | 把发现变成可执行步骤（procedure，不是知识库）：3–5 个 runbook | ✅ 现在做（轻量） |
| **L3** | 记忆与反馈闭环 | 「用着变强」的物理载体：事件分组、事故记忆库、人审回流、golden set | ✅ 现在做 |
| **L4** | 可溯源与信任 | 每条结论挂得回原始证据、可下钻；只读 + 本地的隐私卖点 | ✅ 现在做 |
| **L5** | 模型路由 | 多模型路由、型号配置映射，不自研微调 | 🟡 简化做 |
| **L6** | 分发与工作流嵌入 | 能力挂成 MCP Server / CLI，嵌进用户现有工作流 | ⏸ 后移 V1.5 |
| **L7** | 企业级护城河 | 100+ connector、驻场专家、自动拓扑、自研模型、企业数据层 | ❌ 不做 |

必须建立的几条认知：

- **知识库 ≠ 可执行 procedure**：Harvey 的 2.5 万是工作流（runbook / 状态机，LLM 拿着去执行），不是 2.5 万篇 RAG 文档。前者活、后者死。
- **反馈要回流到规则 / 检测本身，而不只是知识条目**：用户点「错了」应当自动成为回归样本；**golden set（累计 50–100 条标准答案）是成本最低、杠杆最高的垂直能力建设**。
- **一道硬墙**：arXiv 实证 ChatGPT 一次只能有效处理约 200 条日志，所以「把日志直接丢给大模型」工程上不成立——确定性压缩 / 聚合层是必需品，不是可选项。
- **LLM 在 Observe（捞信号 / 跨源关联）超人，在 Orient（根因判断）危险**：Anthropic 一线 SRE 在 QCon 2026 用 OODA 框架说明，模型会把相关性当因果性，「does Claude fix incidents? No」。

关键来源：[Harvey v. Legora 分析](https://www.legalrealist.ai/posts/26-harvey-v-legora/)、[Sierra 深度（Atlan）](https://atlan.com/know/ai-agent/ai-agent-applications/what-is-sierra-ai/)、[Anthropic SRE, QCon 2026（InfoQ）](https://www.infoq.com/presentations/claude-sre-incidents/)、[arXiv:2309.07938](https://arxiv.org/pdf/2309.07938)、[ClickHouse: AI SRE needs better observability](https://clickhouse.com/blog/ai-sre-observability-architecture)、[k8sgpt](https://k8sgpt.ai/)。

---

## 3.2 扩展姿态：留薄边界 / 做简单版 / 不做不留

判断标准（三档）：

- **留薄边界**：方向几乎确定会发生 + 留边界成本极低 + 这个边界本身就是好设计（即使不扩展也该有）。只收一道清晰边界，**不写未来实现**。
- **做简单版**：这个模块化结构 V1 本来就需要，简单版立刻产生价值（支撑现有功能 + 未来加东西不重构）。
- **不做不留**：范式不同、或属于商业模式层面——提前留的接口大概率是错的，还会污染 / 绑死现在的架构。

| 能力 | 姿态 | V1 做什么 | 不做什么 | 为什么 & 再评估信号 |
|---|---|---|---|---|
| **PB 级列存** | 留薄边界 | 所有 Event 读写集中到 Repository / ORM，SQL 不泄漏进业务 | ClickHouse schema、双写、多后端切换开关 | 列存是 ClickHouse / ES 的深坑（合并 / 分布式 / 运维），你数据量远不到 PB。**信号**：PG 在真实数据量下聚合持续超时，且存储本身成为卖点。 |
| **100+ connector** | 做简单版 | 定 parser 插件契约 + 注册表，只实现 computer_monitoring 的 1–2 个 parser | 空 connector、连接器市场、可视化配置 | 广度是体力护城河，每个 connector 都要长期维护，个人会被拖垮且没一个做深。**信号**：大量用户卡在同一个源，且收入 / 社区贡献足以覆盖维护。 |
| **自研 / 微调模型** | 天然接口已覆盖 | Router + Provider Adapter；自定义（含自训）模型走 OpenAI 兼容端点、作为一个「型号」填进配置 | 训练管线、权重加载抽象 | 微调需大量标注（你还没有），且增益会被下一代基础模型吞掉（Harvey 教训）。**信号**：golden set 证明「通用模型 + 规则」确实不达标，或单场景调用量巨大、蒸馏小模型能明显省钱且质量可复现。 |
| **eBPF 自动拓扑** | 不做不留 | 靠「开放事件 Schema + JSONL」当通用入口，未来采集器输出标准 JSONL 即可接入 | 内核采集专用接口、常驻 agent | 内核在线采集与离线文件上传是两种范式，专用接口留了必错；且需 root、门槛高，与只读本地定位冲突。**信号**：转做多主机 / 微服务在线可观测，且离线日志已无法满足关联。 |
| **企业数据统一层** | 不做不留 | `project_id` 自然归属（所有表带 project_id） | 多租户、SSO、角色权限、workspace 抽象 | 它是 bespoke 集成 + 驻场 + SSO + 合规的商业模式，不是技术接口，个人无法 scale。**信号**：明确转 B 端、已拿到企业客户、且有团队 / 资金做交付与合规。 |

**核心原则**：最好的扩展点不是提前写的抽象，而是「稳定边界 + 开放标准」——路由边界、Repository 边界、parser 契约、事件 Schema / JSONL。这些 V1 本来就该有，顺带就是未来的入口，零额外成本。**过早的抽象比没有抽象更糟：它会绑死你，还给你「已经考虑到了」的错觉。**

---

## 3.3 Domain 插件契约与开放路径

**一个 Domain 插件 = 一个自包含「目录包」：**

```text
domains/computer_monitoring/
├── parser.py        # 该领域的解析（文件 → 原始字段 → 标准事件）
├── analyzers/       # 手写确定性检测（LLM 不参与判断）
├── knowledge/       # confirmed / staging 知识目录（见 8.1）
├── runbooks/        # 可执行 procedure（runbook / checklist，YAML）
└── config.py        # 默认阈值、domain_id、版本号
```

先分清「开放给谁」：

- **开发者加领域（V1 做简单版）**：加一个 domain = 加一个目录包 + 注册一行，内核不动。V1 只注册 `computer_monitoring`；不必为证明复用而真做两个领域，只需保证「加领域不改内核」。
- **终端用户 UI 无代码自建（V1 不做）**：需要解析 / 规则 DSL、自定义 Schema 校验、安全沙箱（防绕过 Guard）、版本管理和整套表单 UI——这是平台化 + 多租户标志，V3 才做。

开放路径分三步，每一步都有独立价值、不提前付复杂度：

```text
阶段 1 · V1（现在）   代码级插件：只有你能加，写一个 domain 目录包 + 注册
阶段 2 · V1.5–V2      文件级：高级用户放一个 domain 的 YAML / 插件包，
                      复用 Git + 人审模式，审核后加载（不写代码、也不做 UI）
阶段 3 · V3+          UI 无代码自建：DSL + 沙箱 + 版本管理，等真有平台化需求再做
```

---

# 4. 技术栈与选型理由

| 用途 | 选型 | 个人全栈理由 |
|---|---|---|
| 语言 / Web | Python 3.13、FastAPI | 异步、自带接口文档、生态全 |
| ORM / 迁移 | SQLAlchemy 2.0（`Mapped` + `select()`）、Alembic | 用 2.0 原生写法，不混用 Flask 风格 API |
| 数据库 | PostgreSQL 15+ | 事务可靠，Event.payload 用 JSONB |
| 异步队列 | Redis 7 + Celery | 重试 / 超时 / 崩溃恢复白送，比自己写后台任务省心 |
| 定时 | Celery Beat | 僵尸 Run 回收等定时动作，不再额外引入 cron |
| 对象存储 | 本地磁盘起步（`uploads/`），预留 S3/MinIO 接口 | 个人项目先不依赖云，部署时再换 |
| 配置 | pydantic-settings | 环境变量 / `.env`，密钥不进代码 |
| 日志 | structlog（JSON 结构化） | 可 grep、可接 Trace ID |
| 前端 | Jinja2 模板 + 原生 JS（无构建工具） | 几个页面足够，不引入前端工程链 |
| 测试 | pytest、httpx、pytest-celery 或直接测函数 | 从阶段 01 就建，每阶段必须写 |
| 部署 | Docker + Docker Compose | 一条命令起全栈；不上 Kubernetes |
| 追踪 | 先做 Trace ID + Run 记录；OpenTelemetry 后移 | V1 可观测够用即可 |

原则：**模块化单体**。一个代码仓库、一套部署，模块边界清晰即可，不拆服务。

---

# 5. 编码顺序总表

```text
阶段 01：工程骨架（含 pytest、Compose 起 PG/Redis）
阶段 02：数据模型与迁移（9 张表 + Project 边界）
阶段 03：Domain 目录包与 computer_monitoring（含 runbook）
阶段 04：上传、脱敏与 Parser（TXT / JSONL）
阶段 05：Tool Registry
阶段 06：Model Gateway 与 Router
阶段 07：Policy 与 Cost Controller
阶段 08：AgentRun 状态机与 Worker
阶段 09：Analysis Pipeline（全链路串联，含分组 / 事故归并）
阶段 10：API
阶段 11：最简前端与报告页
阶段 12：可观测性
阶段 13：测试、Golden Set 与 E2E
阶段 14：Docker 收尾与交付
```

单元测试不是阶段 13 才开始：**每完成一个模块就写对应单测**，阶段 13 只补齐集成测试、E2E 和 Golden Set 回归。

参考工期（全职；兼职按比例放大）：约 **6–8 周**。最容易延期的是阶段 04（Parser）和阶段 11（报告页），预留缓冲。

---

# 6. 阶段 01：工程骨架

**目标**：项目能启动、能读配置、能连数据库和 Redis、能跑测试。

目录：

```text
app/
├── main.py              # FastAPI 入口
├── config.py            # pydantic-settings 配置
├── db.py                # engine / SessionLocal / get_db
├── celery_app.py        # Celery 实例
├── models/              # SQLAlchemy 模型（9 张表）
├── domains/             # 领域「目录包」插件
│   ├── base.py          # DomainBase
│   ├── registry.py
│   └── computer_monitoring/
│       ├── parser.py
│       ├── analyzers/
│       ├── knowledge/   # confirmed / staging（见 8.1）
│       ├── runbooks/
│       └── config.py
├── parsers/             # TXT / JSONL 通用 parser 基类
├── tools/               # 通用数据算子与注册器
├── gateways/            # 模型网关与路由
├── policy/              # 策略与成本控制
├── analysis/            # 分组 / 复杂度评估 / Context 构造 / 编排
├── tasks/               # Celery 任务
├── api/                 # 路由
├── web/                 # Jinja2 模板与静态资源
└── utils/               # 脱敏 / token 估算 / 时间戳归一
uploads/                 # 脱敏后的文件（本地存储）
tests/
├── unit/
├── integration/
└── datasets/            # Golden Set 固定日志
migrations/              # Alembic
docker-compose.yml
.env.example
requirements.txt
```

配置项（`.env`）：

```text
ENVIRONMENT=dev
DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/logagent
REDIS_URL=redis://localhost:6379/0
SECRET_KEY=change-me
# 模型等级 → 具体型号的映射（只在这里出现型号，业务代码只认 L1/L2/L3）
MODEL_L1=your-small-model
MODEL_L2=your-medium-model
MODEL_L3=your-large-model
MODEL_PROVIDER_BASE_URL=
MODEL_PROVIDER_API_KEY=
# 成本单价：每百万 tokens（阶段 07 算成本必需；货币单位与下方预算统一，建议用元）
MODEL_L1_PRICE_INPUT_PER_1M=
MODEL_L1_PRICE_OUTPUT_PER_1M=
MODEL_L2_PRICE_INPUT_PER_1M=
MODEL_L2_PRICE_OUTPUT_PER_1M=
MODEL_L3_PRICE_INPUT_PER_1M=
MODEL_L3_PRICE_OUTPUT_PER_1M=
# 预算（货币单位统一为人民币元）
MONTHLY_BUDGET=10
DEFAULT_RUN_MAX_COST=0.30
# 时间戳缺时区时的默认时区
DEFAULT_TIMEZONE=Asia/Shanghai
LOG_LEVEL=INFO
```

**验收**：
1. `docker compose up` 起 PostgreSQL + Redis；
2. FastAPI 启动并返回健康检查；
3. Celery worker 能启动并消费一个测试任务；
4. `pytest` 可运行（先放一个冒烟测试）；
5. 配置全部来自环境变量，代码里无密钥。

此阶段不写任何 Agent 逻辑。

---

# 7. 阶段 02：数据模型与迁移

**目标**：建立 9 张表与迁移脚本，确立 Project 为数据边界。

```text
User        id, email, password_hash, created_at
Project     id, user_id, name, status, budget_total, budget_used, created_at, updated_at
DataSource  id, project_id, type, format, location, metadata(jsonb), status, created_at
Event       id, project_id, source_id, group_id(null→EventGroup), incident_id(null→Incident),
            timestamp, event_type, severity, message, payload(jsonb), metadata(jsonb)
EventGroup  id, project_id, source_id, group_key, template, time_start, time_end,
            event_count, status, created_at          # 真源=Event.group_id；不存 event_ids 反向数组
Incident    id, project_id, title, status, severity, time_start, time_end,
            group_ids(jsonb), root_cause, source_run_id,
            created_at, updated_at                 # 真源=group_ids；不存冗余 event_ids。事故记忆库
AgentRun    id, project_id, parent_run_id, source_id, input(jsonb), idempotency_key, status,
            current_phase, phase_history(jsonb),                       # 阶段进度（11/12 节要用）
            started_at, finished_at, last_heartbeat, model_calls(jsonb), tool_usage(jsonb),
            tokens_input, tokens_output, cost_actual, error, run_metadata(jsonb)
Insight     id, project_id, run_id, incident_id(null), type, severity, confidence, title,
            summary, reasoning, limitations, created_at
Evidence    id, insight_id, source_id, event_ids(jsonb), time_range, calculation, description
```

关系链：

```text
Event ──(group_id)──→ EventGroup        同模板 / 同时间窗的事件归一组（500 告警 → N 组）
EventGroup ──(多对多, group_ids)──→ Incident   一次事故含多个分组（N 组 → 少数事故）
Incident ←── 可跨 Run 检索（事故记忆库），新分析注入「历史相似事故」
```

设计约定：

- **Project 是上下文边界**：所有业务表都带 `project_id` 并建索引。
- **指标不单独建表**：CPU / 内存 / 磁盘数值以指标事件形式写入 Event：
  `event_type="metric"`，`payload={"metric_name":"cpu_used","value":92.4,"unit":"%"}`。
  这样 V1.5 做 Baseline 时直接聚合指标事件，无需补表。
- **模型调用记录不单独建表**：每次调用作为一条记录追加到 `AgentRun.model_calls`（JSON 数组）：
  `call_id, tier, model, timestamp, tokens_input, tokens_output, cost, status`。
- **不建 Task 表**：分析意图直接体现为 AgentRun；重试产生新 Run，用 `parent_run_id` 串起谱系。
- **阶段进度用显式字段**：`status` 管 7 态生命周期；`current_phase` + `phase_history` 管「跑到哪一步」（阶段 11 进度页、阶段 12 追踪用）。`run_metadata` 只放 `stop_reason` / `completed_phases` / `skipped_phases`，不再当阶段进度的容器。
- **不建 DomainVersion 表**：领域版本用代码常量（如 `DOMAIN_VERSION="1.0.0"`），写入幂等键与 Run 记录；历史可复现靠 Git tag。
- **Policy 不建表**：策略是配置对象（阶段 07），Run 创建时把当时策略快照存入 `run_metadata`。
- **EventGroup / Incident 是表、知识不是表**：分组与事故需要跨 Run 检索与统计，故建表；领域知识（error pattern / fix 建议）用 YAML（见 8.1）。
- **分组归属只有一个真源**：`Event.group_id` 是唯一真源，`EventGroup` 不再存 `event_ids` 反向数组（查成员用 `WHERE group_id = ?`）；同理 `Incident.group_ids` 是事故真源，`Incident` 不存冗余 `event_ids`。避免双写一致性与 jsonb 数组膨胀。

隔离实现（用 SQLAlchemy 2.0 正确方式）：

- 所有读写走统一的 repository 函数，内部强制带 `project_id` 条件；
- 不依赖「开发者记得过滤」的裸查询；隔离测试见阶段 13。

**验收**：迁移可执行可回滚；能创建 Project / DataSource；能插入普通事件与指标事件并按 project 查回；事件可归入 EventGroup、再归并为 Incident 并查回；`model_calls` 可追加记录。

---

# 8. 阶段 03：Domain 目录包

**目标**：领域以目录包插件方式接入，Agent Runtime 不出现任何 `if domain == ...`。

`DomainBase` 至少定义：

```python
class DomainBase(ABC):
    domain_id: str
    version: str

    @abstractmethod
    def parse_line(self, line: str) -> dict | None: ...        # 原始行 → 原始字段

    @abstractmethod
    def normalize(self, raw: dict) -> Event: ...               # 原始字段 → 标准 Event

    @abstractmethod
    def metric_specs(self) -> list[MetricSpec]: ...            # 识别哪些指标、如何抽取

    @abstractmethod
    def analyzers(self) -> list[Analyzer]: ...                 # 手写确定性检测（统一称 analyzer）

    @abstractmethod
    def runbooks(self) -> list[Runbook]: ...                   # 可执行 procedure（YAML 加载）
```

**tool 与 analyzer 的边界（不要混）**：

```text
tool     通用数据算子：过滤 / 统计 / 时间窗口，跨领域复用，不含领域判断
analyzer 领域检测：含阈值与判断逻辑，是 domain 私有资产，产出候选异常
```

`ComputerMonitoringDomain` 提供：

- syslog / dmesg 风格行解析（时间戳、severity、进程/模块、message）；
- 指标识别：CPU 使用率、内存使用/增长、磁盘占用、负载等；
- 手写 analyzer（V1 先落 4 个核心，后续逐步扩到 15–25 个；阈值先保守，靠 Golden Set 调整）：
  - `CPUSpike`：cpu_used > 90% 持续若干采样点；
  - `MemoryGrowth`：内存指标在时间窗口内持续上升（增长率阈值）；
  - `DiskFull`：disk_used > 95%；
  - `ProcessCrash`：message 命中 crash / segfault / oom 等模式。
- **3–5 个 runbook（可执行 procedure，YAML，不是知识库文档）**，覆盖最高频场景：OOM、磁盘满、进程崩溃、CPU 飙升、内存泄漏。结构：

```yaml
id: rb_oom_001
title: 内存耗尽 / OOM 处置
applies:        # 命中哪些异常
  analyzer: ProcessCrash
  message_pattern: "Out of memory|oom-killer"
steps:          # 可执行检查清单（V1 只读展示，不自动执行）
  - "确认时间窗内内存水位与增长趋势"
  - "定位占用最高的进程"
  - "核对近期是否有新进程 / 部署"
  - "评估扩容或调整内存上限"
references: []
```

DomainRegistry 负责注册与按 id + version 加载。

**验收**：能注册并加载 computer_monitoring；不修改 Runtime 代码即可被替换；对样例行能产出标准事件、指标事件；命中异常时能取到对应 runbook。

---

## 8.1 领域知识沉淀（垂直能力增长闭环）

**为什么需要**：垂直 Agent 的护城河是**领域知识资产**，不是 Agent 框架。V1 不能只停在静态阈值，必须让每次真实分析都把「没见过的异常」沉淀为可复用知识，系统才会越用越懂这一行。这是 V1 里垂直能力的增长引擎之一（与事故记忆库互补：知识=可复用的模式/经验，事故=具体发生过的事实）。

**载体：YAML 文件，随 domain 目录包一起版本化；V1 不建知识表**

```text
domains/computer_monitoring/knowledge/
├── confirmed/               # 人工确认后生效，参与匹配与展示
│   ├── error_patterns.yaml  # 错误 / 异常模式
│   ├── root_cause_hints.yaml
│   ├── fix_suggestions.yaml
│   └── false_positives.yaml # 判定为「不是问题」的误报，用于抑制
└── staging/
    └── candidates.yaml      # 模型生成的候选（draft），待审核，不参与自动判断
```

选 YAML 的理由：人可读、可 diff、随领域代码进 Git、零迁移；写入只发生在人工确认时，V1 知识量小，完全够用。命中计数、自动强化等运行时统计放 V1.5，届时再判断是否需要建表。

**知识条目结构**：

```yaml
- id: kp_cpu_001
  kind: error_pattern        # error_pattern | root_cause_hint | fix_suggestion | false_positive
  title: CPU 持续高占用
  match:                     # 命中条件
    event_type: metric
    metric_name: cpu_used
    condition: "value > 90"  # V1 只支持少量固定操作符，不做通用表达式引擎
    min_samples: 3
  message_pattern: null      # 可选：message 正则
  severity_hint: medium
  description: ""
  related: []                # 关联的其他知识条目 id
  evidence:                  # 必填：真实来源，沿用证据校验
    run_id: ""
    event_ids: []
  confidence: 0.0
  status: confirmed          # draft | confirmed | deprecated
  created_at: ""
  updated_at: ""
```

**闭环流程**：

```text
分析中发现 confirmed 知识未覆盖的异常 / 错误
  → 模型在「同一次」分析调用里额外输出 knowledge_candidates（不新增调用、零新增成本）
  → 候选必须绑定真实 event_id（子集校验，不允许凭空总结）
  → 写入 staging/candidates.yaml，报告页「待确认知识」区可见
  → 人工审核：确认 / 编辑 / 丢弃；若其实不是问题，确认为 false_positive
  → 确认后移入 confirmed/，下次分析自动加载
  → error_pattern 命中直接给出解释；false_positive 命中则抑制该误报

V1 做到「能沉淀、能审核、能加载命中」；命中计数与自动强化放 V1.5。
```

**约束（把不可靠性挡在 confirmed 之外）**：

1. draft 知识**不参与自动结论**，只作提示——防止把一次性、偶发观察当成规律；
2. 无 evidence、无 confidence 的条目不允许确认；
3. fix_suggestion 在 V1 仅作信息展示，不自动执行（与只读原则一致）；
4. match 规则（条件 / 正则）由模型生成草案、**必须人工确认才生效**——其生成准确率无法预先保证。

**需要你自己补、我不代填的**：

- 具体知识内容（真实错误码、故障模式、修复经验）必须来自你的真实日志与经验，我不虚构这些条目，只提供结构与流程；
- condition 的表达式深度：V1 建议只实现固定操作符（`>`、`<`、`=`、持续 N 次），不做通用规则引擎。

---

# 9. 阶段 04：上传、脱敏与 Parser

**目标**：日志上传后**先脱敏、再落盘、再解析入库**。这是安全顺序，不能颠倒。

管道：

```text
Upload
  → 读取到内存（流式分块，限制大小）
  → 脱敏（正则替换，发生在写磁盘之前）
  → 保存「脱敏后」文件到本地 uploads/（原始文件处理完即删除，不持久化原文）
  → Parser（TXT / JSONL）
  → Domain.normalize
  → 批量写入 Event
  → 后台任务保存解析统计（总行 / 成功 / 坏行 / 脱敏计数），通过查询接口返回
```

**DataSource 与上传的关系（V1 简化口径）**：

- 上传请求可带 `data_source_id`；未带则按「项目 + 文件格式」自动创建（首次）或复用同名 DataSource；
- 一个上传文件关联一个 DataSource；不要求用户先手工建 DataSource。

Parser 结构（与 Agent 逻辑解耦）：

```text
BaseParser ├── TxtParser   （按行；处理多行堆栈的合并、时间戳/时区、空行/截断行）
           └── JsonlParser （每行一个 JSON；坏行计数并报告，不让单行坏数据拖垮整文件）
```

**时间戳 / 时区归一（硬规则，否则窗口与相关性全错）**：

```text
统一存储 timezone-aware UTC。
syslog 缺年份 → 按当前年补（跨年边界需留意）；
缺时区 → 按 .env 的 DEFAULT_TIMEZONE 解析后转 UTC；
完全无法解析的时间戳 → 计入坏行，不静默用当前时间替代。
```

脱敏规则（保守优先，避免误伤排障字段）：

```text
邮箱、信用卡、电话号码
IP 地址（按需，内网排障可能需要，可配置开关）
明确前缀的密钥：sk-…、ghp_…、AKIA…、xoxb-… 等
不使用 [A-Za-z0-9]{32,} 这种宽匹配——它会误伤 commit hash、trace id、session id
NER 人名/机构识别：后移，V1 不做
```

替换方式：一致性占位符（单用户全局一致，便于跨项目关联）：

```text
"user@example.com" → "[EMAIL_a1b2c3d4]"
"sk-abc..."        → "[SECRET_e5f6g7h8]"
```

- 白名单：`DataSource.no_mask_fields`（在 metadata 中）允许声明不脱敏字段；
- 误伤与漏检都要记录计数，写入 Run 元数据，供后续调整规则。

**验收**：
1. 上传 TXT / JSONL 返回 202 + job_id，后台解析入库；任务查询响应含解析统计；
2. 含邮箱 / 密钥的内容在**落盘文件和数据库**中都已被替换；磁盘上找不到原文敏感值；
3. 坏行 / 无法识别的时间戳被计数并明确报告，不静默丢弃；
4. 未带 data_source_id 的上传能自动建 / 复用 DataSource；
5. 超过大小上限的上传被拒绝并给出明确提示。

---

# 10. 阶段 05：Tool Registry

**目标**：所有工具统一注册，输入输出明确、可测试、可设超时、可记录用量。

工具元数据：`name, version, domain, input_schema, output_schema, timeout`。

V1 工具（通用数据算子，跨领域复用）：

```text
stats_calculator   事件总数、各 severity / event_type 分布、时间跨度、错误率
event_filter       按 severity / 类型 / 时间范围 / 关键词过滤
time_window        时间窗口切分与窗口内聚合
```

工具是纯确定性函数：同样输入必然同样输出，不调用模型、不含领域判断（领域判断走 analyzer）。

**验收**：三个工具结果正确、边界（空集 / 单事件 / 大窗口）行为明确；注册器能按名取用；执行结果可记录到 Run。

---

# 11. 阶段 06：Model Gateway 与 Router

**目标**：业务层只调用 `generate()`，不知道供应商；Router 只输出等级，具体型号由配置映射。

模型等级：

```text
L0  代码 / 规则（不调用模型）
L1  小模型：归类、摘要、简单解释
L2  中模型：相关性、多错误归纳
L3  大模型：复杂根因、跨时间簇的困难推理
```

Gateway 抽象：

```python
class ModelGateway(ABC):
    @abstractmethod
    def generate(self, prompt: str, schema: dict | None,
                 max_output_tokens: int) -> ModelResult: ...
    # ModelResult: content, parsed, tokens_input, tokens_output, model, raw
```

- 各供应商（DeepSeek 官方 API 等 OpenAI 兼容端点，含未来本地模型 Ollama / vLLM）各自实现 Gateway；
- Router 读取配置中的 `MODEL_L1/L2/L3` 与 `MODEL_PROVIDER_BASE_URL`，**代码中不出现具体型号**；
- 每次调用返回的实际 token 用量即时记录到 `AgentRun.model_calls`。

注意：不同模型对「结构化输出 / function calling」支持方式不同，Gateway 内部消化这些差异，对上只暴露统一接口；无法满足结构化约束的等级要在配置中标注并给出降级方式。

**验收**：至少接通一个真实供应商并返回结构化结果；切换型号只改环境变量、不改业务代码；token 与成本被记录。

---

# 12. 阶段 07：Policy 与 Cost Controller

**目标**：策略集中管理；每个 Run 有预算，超限停止并返回部分结果，禁止无限循环。

策略字段（配置对象，可按 Project 覆盖）：

```text
max_model_calls_per_run   # 如 5
max_tokens_per_run        # 如 12000
max_runtime_seconds       # 如 180
allowed_tiers             # 如 ["L1","L2","L3"]
run_max_cost              # 如 0.30
```

成本三检查点：

```text
Pre-check（创建 Run 时）
  按「蒸馏后 Context 预算 + 预计输出」估算（不是按原始事件数线性估算），
  叠加 10% 安全边际；月度预算或 Run 预算不足 → 拒绝创建。

Mid-check（每次模型调用前）
  已用 token / 调用次数 / 运行时长 / 已花成本，任一到顶 →
  停止昂贵步骤，状态置 partial_success，返回已完成部分。

Post-check（Run 结束时）
  汇总实际 token / 成本 / 调用次数，累加回 Project.budget_used，
  写入模型调用记录。
```

部分结果的表达：

```text
AgentRun.status = "partial_success"
run_metadata = {
  "stop_reason": "budget_exceeded | token_limit | runtime_limit | model_unavailable",
  "completed_phases": [...],
  "skipped_phases": [...],
  "note": "分析因预算/时限/模型不可用中断，结果可能不完整"
}
```

成本怎么算（给公式，不给死数字）：

```text
单次成本 = 输入token × 输入单价 + 输出token × 输出单价
各等级单价以你所用模型的官方现价为准，自行填入配置；
模型迭代很快，不要把任何价格或型号写死在计划里。
```

**验收**：预算不足拒绝创建；人为设置极小预算能触发 partial_success 且页面明确标注「不完整」；成本被正确累加；无法构造出无限调用的 Run。

---

# 13. 阶段 08：AgentRun 状态机与 Worker

**目标**：分析在 Worker 异步执行，状态流转可控，崩溃 / 超时 / 取消 / 重试都有明确处理。

状态机（7 态）：

```text
queued    → running
running   → completed | partial_success | failed | timeout | cancelled
queued    → cancelled

拦截非法转移：
completed / partial_success / failed / timeout / cancelled → 不可再改
failed 不可原地复活，只能新建 Run（parent_run_id 关联）
```

可靠性机制：

- **幂等键**：

```text
sha256(project_id + source_id + time_range + filters_hash
       + domain_id + DOMAIN_VERSION + PIPELINE_VERSION)
```
  相同键且已有成功 Run → 直接复用结果，不重复执行、不重复计费。

- **心跳与僵尸回收**：Worker 在各阶段及模型调用前后更新 `last_heartbeat`；
  Celery Beat 每周期检查，心跳丢失超过阈值（如 5 分钟，阈值要大于单次最慢调用）的 running Run 置为 timeout。
  长模型调用期间用独立线程维持心跳，避免被误判。

- **取消**：置 `cancel_requested` 标记，Worker 在阶段边界与每次调用前检查并抛出取消。
  诚实说明粒度：若恰好进入一次长调用，最坏要等该调用返回；不承诺「秒停」。

- **重试**：

```text
可重试：网络错误、限流、服务暂不可用（指数退避 + 抖动，最多 3 次）
不可重试：输入格式错误、认证失败、预算耗尽、校验失败
```

- **模型不可用降级链（重试耗尽后）**：

```text
当前等级模型连续失败
  → 降一级模型重试（L3→L2→L1）
  → 仍失败 → 输出纯规则 L0 报告（analyzer 已产出的确定性结果），
    状态 partial_success，stop_reason="model_unavailable"，页面标注
```

- **失败不静默**：任一阶段失败且重试无果 → Run 置 failed 并写明阶段与原因；
  绝不能在 Parser / 检测失败后继续生成一份「看起来正常」的报告。

**验收**：创建 Run 立即返回 `run_id + queued`；手动 kill Worker 后僵尸 Run 被回收；重复提交不产生重复 Run；取消能在下一个检查点生效；模型不可用时能降级到 L0 报告；各类错误分类正确。

---

# 14. 阶段 09：Analysis Pipeline

**目标**：把确定性分析与模型分析串成 V1 核心链路。

```text
加载事件
  → 指标计算（产出 metric 事件）
  → 模板聚类 / 事件分组（EventGroup）
  → 事故归并（Incident，同时间窗 / 同根因的分组合并；可复用已有 Incident）
  → analyzers / confirmed 知识命中（按下方执行顺序）
  → 异常检测（V1 仅文件内统计，见下）
  → 复杂度评估（决定 L0–L3）
  → Context 构造与蒸馏（含历史相似 Incident，按等级 token 预算）
  → 模型分析（同一次调用同时输出 insights 与 knowledge_candidates；
              L1/L2 一轮，L3 最多两轮、第二轮 self-critique，可配置开关）
  → Evidence 校验（event_id 子集校验、幻觉拦截）
  → Insight 落库；候选知识写入 staging（见 8.1）
```

**检测执行顺序（固定，避免冲突）**：

```text
1. analyzers 产出候选异常
2. confirmed error_patterns 增强解释 / 补充命中
3. false_positives 最后裁决——命中则抑制该（误报）异常
```

**V1 异常检测范围（澄清基线歧义）**：

```text
V1 只做：
  ① 静态阈值 analyzer；
  ② 文件内统计：窗口内 Z-score（相对本文件分布）、相邻时间窗环比。
V1 不做（明确 V1.5）：跨文件 / 长期历史 Baseline、季节性建模。
```

**复杂度评估规则**（V1 起点，靠 Golden Set 迭代）：

```text
L0：无 error 且无 anomaly          → 纯代码统计报告（与事件总量无关，
                                    量大只影响采样，不应因「正常但量大」升到 L3）
L1：error_count < 10 且无 high 异常 → 小模型归类解释
L2：error_count < 50，或异常涉及多个时间簇/类型
L3：存在 high severity 异常，或错误跨多簇多类型，或 L2 分析后置信度不足触发升级
事件总量只决定采样密度与是否提示缩小范围，不单独决定模型等级。
```

**Distilled Context（给模型的数据包，6 部分）**：

```text
1. Metadata   领域、时间范围、事件数
2. Statistics  event_type / severity 分布、错误率、关键指标
3. Incidents   本次归并的事故 + 检索到的「历史相似 Incident」及其根因
4. Anomalies   异常点（event_id、时间、类型、分数）
5. Samples     错误事件全给（过多则采样）；警告/正常事件采样
6. Context     规则命中结果、已知 pattern
```

Token 预算与压缩：

| 等级 | 总预算 | Meta | Stats | Incidents | Anomalies | Samples | Context |
|---|---|---|---|---|---|---|---|
| L1 | 2000 | 100 | 200 | 300 | 400 | 700 | 100 |
| L2 | 4000 | 100 | 300 | 500 | 700 | 2100 | 300 |
| L3 | 8000 | 100 | 400 | 900 | 1300 | 4800 | 500 |

超长时按序压缩：降低 Samples 采样 → 只留 top-k 异常 → 精简历史 Incident → 删低优先级 Context；
仍超限则明确报错「超出单次分析范围，请缩小时间范围」。**自动分片聚合放 V1.5，V1 不做。**

**结构化输出与四层语义**：

- 用 JSON Schema / function calling 约束输出：

```python
{
  "insights": [{
    "type": "fact" | "inference" | "possibility" | "unknown",
    "severity": "high" | "medium" | "low",
    "confidence": 0.0-1.0,
    "title": str, "summary": str,
    "evidence_ids": [str],          # 必填
    "reasoning": str,              # type != fact 时必填
    "limitations": str             # possibility / unknown 时必填
  }],
  "knowledge_candidates": [{       # 仅针对「现有知识未覆盖」的异常，结构见 8.1
    "kind": str, "title": str, "description": str,
    "match": {},                   # 命中条件草案
    "evidence_ids": [str], "severity_hint": str, "confidence": 0.0
  }]
}
```

候选知识与结论在同一次模型调用里产出，**不额外增加调用和成本**；候选同样要过 evidence 子集校验，且在人工确认前只进 staging、不影响自动结论。

四层语义在三层同时强制：

| 类型 | Schema | Prompt | 报告页渲染 |
|---|---|---|---|
| fact | evidence_ids 必须非空 | 「只有能引用具体 event_id 才能标 fact」 | 「✓ 确认」+ 证据链接 |
| inference | reasoning 必填 | 基于证据的推断，写出推理过程 | 「→ 推测」+ 推理 |
| possibility | limitations 必填 | 可能原因，说明为何无法确认 | 「? 可能」+ 局限 |
| unknown | limitations 必填 | 信息不足，说明缺什么 | 「− 未知」+ 信息缺口 |

**幻觉校验（拦截编造证据）**：

```text
1. 解析/蒸馏阶段收集本次有效 event_id → valid_ids
2. 模型返回后检查 evidence_ids ⊆ valid_ids
3. 处理：
   无效占比 > 50% → 拒绝该 Insight，带反馈重试（最多 3 次）
   无效占比 ≤ 50% → 移除无效 id，按比例下调 confidence
   全部无效但 type=possibility → 保留并在 limitations 注明
   重试仍失败 → fact 一律降级为 possibility，写明「模型未能提供有效证据」
```

**验收**：正常日志走 L0；少量错误走 L1；多错误 / 高危异常走到相应等级；事件能分组并归并为事故、历史相似事故被注入 Context；Context 不超预算；构造「模型返回假 event_id」的用例能拦截并重试 / 降级；每个 fact 都有有效证据。

---

# 15. 阶段 10：API

API 只做验证、鉴权、创建任务、返回状态，不承担分析逻辑。

鉴权方式（V1 定）：**JWT**。API 走 `Authorization: Bearer <token>`；极简网页把 token 存 `httpOnly` Cookie。自用单账号、不做角色系统（见 1 节）。用 `pyjwt`，依赖轻、API 与页面统一。

```text
POST /api/register、POST /api/login（自用可省略注册页）

POST /api/projects
GET  /api/projects
POST /api/projects/{id}/data-sources
POST /api/projects/{id}/upload            # 内存脱敏与安全文件排队，返回 202 + job_id
GET  /api/projects/{id}/uploads/{job_id}  # 后台解析进度与统计
POST /api/projects/{id}/analysis-runs     # 创建分析，立即返回 run_id + queued
GET  /api/runs/{id}                       # 状态、进度、成本
GET  /api/runs/{id}/insights
GET  /api/insights/{id}
GET  /api/insights/{id}/evidence
POST /api/runs/{id}/cancel

# 知识审核（对应阶段 11「待确认知识」区；8.1 闭环的运行时入口）
GET    /api/projects/{id}/knowledge/candidates         # 列出 staging 候选
GET    /api/projects/{id}/knowledge/confirmed          # 列出已生效知识
POST   /api/knowledge/candidates/{candidate_id}/confirm         # 确认 → 移入 confirmed
PATCH  /api/knowledge/candidates/{candidate_id}                 # 编辑后再确认
POST   /api/knowledge/candidates/{candidate_id}/reject          # 丢弃
POST   /api/knowledge/candidates/{candidate_id}/false-positive  # 标记误报 → confirmed/false_positives
```

`analysis-runs` 请求体（V1）：

```text
{
  "data_source_id": int,          # 必填：对哪个数据源分析
  "time_range": { "start": iso8601?, "end": iso8601? },   # 可空，默认全部
  "filters": { "severity"?: [], "event_type"?: [], "keyword"?: "" }  # 可空
}
```

**验收**：每个端点鉴权与参数校验生效；分析端点立即返回、不阻塞；接口文档 `/docs` 可访问；跨 Project 访问被拒绝。

---

# 16. 阶段 11：最简前端与报告页

不引入前端构建工具，Jinja2 + 原生 JS 即可：

```text
登录页
项目列表 / 新建
项目详情：上传日志（显示解析结果与脱敏计数）
发起分析 → Run 状态页（轮询 /api/runs/{id}，显示阶段进度）
报告页：
  - Insight 按四层语义着色（✓ → ? −）
  - 每条可展开 Evidence：关联事件、时间范围、计算说明
  - 命中异常附对应 runbook（只读展示处置步骤）
  - partial_success 顶部显著标注「结果不完整」及中断原因
失败态：
  - Run failed / timeout：显示失败阶段与原因，提供「重新分析」（新建 Run）入口
待确认知识区（读 staging）：
  - 列出候选条目及其证据，操作：确认 / 编辑 / 丢弃
  - 提供「这其实不是问题」按钮 → 确认为 false_positive
  - 确认后写入 confirmed，下次分析生效
```

**验收**：从上传到看报告全程不碰命令行；fact 的证据可点击核对；runbook 可见；不完整 / 失败结果有显著提示与重试入口。

---

# 17. 阶段 12：可观测性

每个 AgentRun 必须可追踪，记录：

```text
trace_id（一次请求/一次 Run 一个，贯穿日志与任务）
run_id / project_id / parent_run_id
开始 / 结束 / 耗时
使用的工具、模型等级与具体型号
每次调用的 token、成本
错误、重试、取消、降级
策略检查点的判定结果
```

- structlog 输出 JSON，带 trace_id；
- V1 做一个简单的 Run 列表 / 详情视图即可；OpenTelemetry、集中式指标平台后移。

**验收**：给定任一 Run 能完整复述「系统做了什么、花了多少、为何得到这个结论」。

---

# 18. 阶段 13：测试、Golden Set 与 E2E

**单元测试**（其实贯穿全程，这里补齐覆盖率）：

```text
Parser、脱敏、时间戳归一、Normalizer、工具、分组 / 事故归并、复杂度规则、Router、Cost、Evidence 校验
```

**Golden Set（固定数据集 + 期望结论）**：V1 先建 5 个场景，累计逐步扩到 50–100 条标注案例：

```text
tests/datasets/
├── normal/          # 正常日志，期望 L0、无异常 Insight
├── cpu_anomaly/     # 期望命中 CPU 异常
├── memory_growth/   # 期望命中内存持续增长
├── crash/           # 期望命中进程崩溃
└── malformed/       # 坏格式，期望明确失败而非假报告
```

- 每次改动重跑：既验证「不崩」，也验证「关键 Insight 命中、fact 有证据」；
- 同时记录每次 Golden Run 的成本，成本较历史明显异常时人工排查；
- V1 用人工核对 Golden Set 结果即可；自动化 LLM Judge 后移。

**集成 / E2E**：

```text
API → DB → Queue → Worker → Analysis → Insight/Evidence 全链路
隔离攻击测试：
  - 篡改 URL / ID 尝试读取其他 Project 的事件、文件 → 必须拒绝
  - 上传含密钥文件，断言磁盘与库中都不存在原文
  - 极小预算 → 期望 partial_success
  - kill Worker → 期望僵尸 Run 被回收为 timeout

分组 / 事故测试：
  - 同类刷屏日志被聚成 EventGroup，再归并为 Incident
  - 新分析能检索到历史相似 Incident

知识闭环测试：
  - 新异常 → 候选写入 staging，且候选必带有效 evidence（无证据候选不允许出现）
  - 未确认的 draft 不影响任何自动结论
  - 人工确认后进入 confirmed，下一次相似输入能命中
  - 标记 false_positive 后，对应误报在后续分析中被抑制
```

**验收**：单测通过、Golden Set 五个场景全部符合预期、E2E 跑通、隔离攻击用例全部失败于系统防线。

---

# 19. 阶段 14：Docker 收尾与交付

本地 / 单机部署：

```text
docker compose
├── web（FastAPI，也提供页面）
├── worker（Celery worker）
├── beat（Celery beat，可与 worker 合并）
├── postgres
└── redis
（对象存储 V1 用本地卷 uploads/）
```

- 镜像内不含密钥；迁移在启动时自动 / 手动可执行；
- 提供 README：环境变量说明、`docker compose up` 启动、最小使用流程；
- 不上 Kubernetes、不做多环境流水线，CI 只做「自动跑测试」一件事即可。

**验收**：全新环境 clone 后，按 README 能在本机一键起全栈并完成一次真实分析。

---

# 20. V1 不做清单（含原因与再评估信号）

为防止工程失控，以下一律不进 V1。**保留这份清单是为了明确告诉后来的人或模型：这些不是遗漏，而是有意不做**；每条给出原因，以及满足什么信号才重新评估。

**A. 范式 / 商业模式不同：V1 不做、也不留专用接口**

| 不做项 | 为什么不做 | 再评估信号 |
|---|---|---|
| eBPF 自动拓扑 | 内核在线采集与离线文件上传是两种范式，需 root 常驻、门槛高，与只读本地定位冲突（详见 3.2） | 转多主机在线可观测，且离线日志无法满足关联 |
| 企业数据统一层 | bespoke 集成 + 驻场 + SSO + 合规的商业模式，个人无法 scale | 明确转 B 端、有企业客户与团队 / 资金 |
| 自研 / 微调模型 | 需大量标注、增益会被基础模型迭代吞掉（Harvey 自研已失败） | golden set 证明通用模型不达标，或蒸馏可显著省钱且质量可复现（接入接口已天然具备） |

**B. 只留边界 / 简单版，重的部分不做**

| 不做项 | V1 只做到哪 | 再评估信号 |
|---|---|---|
| PB 级列存 | 只留 Repository / ORM 数据访问边界；列存是基础设施公司的领域，数据量不到 | PG 在真实数据量下聚合 / 查询持续超时，且存储成卖点 |
| 100+ connector | 只做 parser 插件契约 + 1–2 个实现；广度是体力护城河 | 大量用户卡同一源，且收入 / 社区贡献可覆盖维护 |
| 终端用户 UI 自建 Domain | 只做开发者代码级插件（见 3.3） | 单领域被验证后，按「文件级 → UI 级」演进 |
| MCP Server / CLI 分发 | V1 不做，仅保留 Gateway / 输出可被封装的边界 | V1.5 直接做（已列入版本路线） |

**C. 其他 V1 不做项**

| 不做项 | 为什么不做 | 再评估信号 |
|---|---|---|
| Multi-Agent | V1 单条链路已够，引入只增故障面 | 单 Agent 被指标证明扛不住（见版本路线 V2.5） |
| 自动修改设备 / 执行命令 / 自动控制 | 只读是定位与信任的基础 | V3 受控执行：审批、回滚、审计齐备 |
| 实时流处理 | V1 是被动分析 | V1.5 / V2 |
| 自动分片聚合 | 无验收的隐藏 map-reduce | V1.5 |
| 复杂长期 Memory | V1 知识 YAML + Incident + Run 记录够用 | 出现跨 Run 检索的新需求 |
| 插件市场、数十个 Domain | 平台化过早 | 单领域被验证、有资源 |
| 自主联网调查、复杂知识图谱 | 安全与可靠性不可控 | — |
| 自动根因闭环修复 | LLM 根因判断危险（相关性当因果） | L2 procedure 成熟、进入 V3 |
| 微服务拆分、Kubernetes | 模块化单体足够 | 单机扛不住真实负载 |
| 复杂计费、角色权限体系 | 自用 / 单账号即可 | 转多用户商业化 |
| 自动化 LLM Judge | 先人工，避免裁判本身不可靠 | golden set 量大到人工不现实 |
| CSV / JSON 解析 | V1 两种格式已验证主链路 | V1.5 / V2 |
| 数据 / 项目级联删除 | V1 自用可手动清理，不做级联 | 转多用户、有删除合规需求 |

---

# 21. 核心假设与证伪信号

V1 的目的之一是用最小代价验证假设，看到证伪信号就转向，不要硬撑：

| 假设 | 验证方式 | 证伪信号 |
|---|---|---|
| 分层智能能显著省钱 | 同一日志对比「分层」与「全程 L3」的成本 | 分层成本 > 全程 L3 的 70% |
| Evidence 能提升信任 | 让几位真实用户使用并评分 | 信任评分持续偏低，或用户根本不看证据 |
| Domain 可插件化扩展 | 尝试新增一个 domain（如 network_monitoring） | 需要改动核心 Runtime 代码 |
| 预算上限不摧毁分析价值 | 对比有 / 无预算下的 Insight 数量与质量 | 有预算时有效 Insight 不到无预算的一半 |
| 解析在真实日志上可用 | 用 3–5 份**真实**日志测可解析率与脱敏误伤率 | 可解析率过低，或脱敏大量抹掉排障信息 |

建议在正式铺开前，第一周先跑最后一行：真实日志的可解析率、脱敏误伤率，以及按模型现价重算的单次成本——这三个数字决定项目是否按现路线成立。

---

# 22. V1 最终验收标准

不要用「代码写完了吗」判断 V1 完成，要逐项能回答「是」：

```text
1. 能接收真实日志（TXT / JSONL）并正确解析？
2. 数值指标能被识别并以指标事件存储？
3. 同类刷屏日志能聚类为 EventGroup、再归并为 Incident？
4. 规则 / 异常检测能发现预设异常？
5. 复杂度评估能把任务分到合理的 L0–L3？
6. 模型调用与成本可控、可设预算、超限能停？
7. 模型失败能重试 / 降级（含降到 L0 报告），工具失败能明确报错？
8. 每个 fact 都有有效 Evidence，编造的 event_id 会被拦截？
9. 命中异常能展示对应 runbook？
10. 任一 Run 都能完整追踪（做了什么、花了多少）？
11. 数据不串 Project，含敏感信息的原文不落盘？
12. 能 Docker 一键部署、能重复执行？
13. 新异常能沉淀为候选、人工确认后下次能命中，误报能被标记并抑制？
```

全部为「是」，V1 才算完成。之后先做一轮 Hardening（稳定性 / 成本 / 误报 / 延迟 / 失败恢复），再进入 V1.5。

---

# 23. 工程原则（始终遵守）

### 给执行 Agent（Codex / Claude / DeepSeek harness 等）的最终指令

你现在不是在「写一个聊天机器人」，而是在实现一个**受约束、可恢复、可观测、可控制成本的垂直日志分析 Runtime**。严格遵循本文档的对象边界（9 张表）、状态机（7 态）与分级路由（L0–L3）。

第一优先级不是增加智能，而是建立正确的执行闭环（V1 实际链路，顺序与阶段 09 一致）：

```text
Upload → Mask → Parse → Normalize(Event / Metric)
       → Group(EventGroup) → Merge(Incident)
       → Analyzer / Knowledge → Anomaly(文件内)
       → Complexity Classify → Context(含历史事故)
       → Tiered Model (Gateway / Router)
       → Validate (Evidence 子集校验、幻觉拦截)
       → Insight (+Runbook) → Report
（模型不可用 → Recovery：降级 / L0 报告；候选知识 → 人审）
```

任何**无法验证、无法恢复、无法限制成本、无法审计**的 Agent 能力，都不应因为「看起来更智能」而进入核心 Runtime。先实现稳定 V1，再扩展能力；遇到设计冲突先记录并提出，不要擅自改动对象边界或状态机。

```text
1.  确定性计算不调用 LLM。
2.  不把原始日志全量扔给 LLM，先结构化、再蒸馏。
3.  具体型号不写进业务代码，只认 L0–L3 + 配置映射。
4.  Domain 不写死在 Runtime，一律走 Registry。
5.  API 不承担长任务，分析全部异步化。
6.  没有有效 Evidence 的结论不能标 fact。
7.  失败不静默：明确失败或降级，不生成看似正常的假报告。
8.  Agent 不允许无限循环：调用次数 / token / 运行时长都有硬上限。
9.  不为未来功能提前写复杂代码，只留最小接口缝。
10. 每阶段：编码 → 测试 → Commit，可随时回滚。
```

新增任何能力前，先回答三个问题：

```text
- 为什么需要它？
- 能不能用确定性代码完成？
- 它的成本、风险和失败方式是什么？
```

---

# 24. 执行节奏

```text
一个阶段
  → 编码（含该模块的单元测试）
  → 本地验证（对照该阶段「验收」逐条过）
  → Git Commit（消息写明阶段号）
  → 进入下一阶段
```

Commit 节奏与阶段一一对应，任何一步出问题都能明确定位，而不是最后攒出一个无法维护的大工程。

```text
骨架 → 数据模型 → Domain 目录包 → 脱敏/Parser → 工具 → Gateway/Router
     → Policy/Cost → 状态机/Worker → Analysis 全链路 → API
     → 前端报告页 → 可观测 → Golden Set/E2E → Docker 交付
     → V1 → Hardening → V1.5 → V2 → V2.5 → V3
```

这份文档是 **V1 的施工依据**：按阶段执行，不按临时想到的功能随意加东西。
跨版本总纲、文档地图与新窗口接续，见《Log_Intelligence_Agent_总工程文档.md》。
