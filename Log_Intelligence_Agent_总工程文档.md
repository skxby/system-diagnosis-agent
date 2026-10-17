# Log Intelligence Agent
## 总工程文档（Master / 项目总纲）

> **这是项目最高层、最稳定的总纲，不写实现细节。**
> 它的作用只有三个：
> 1. 任何**新会话 / 新窗口**，先读本文件即可掌握全貌、直接接续工作；
> 2. 它**指向**各版本详细计划与调研，不替代它们；
> 3. 只在战略级变化时更新；实现细节变化改「版本计划」，不改本总纲。

---

# 1. 一句话定位与战略

这是一个**产品 / 设备日志智能分析垂直 Agent**。

**战略定位：做「被标准喂饱的垂直分析层」**——单领域、只读、本地、证据链可下钻。

```
不做底层存储平台（ClickHouse / Elasticsearch 的领域）；
不做通用 Agent Runtime / 中台；
只做：把一个领域的日志接入、解析、确定性检测、记忆与证据做到极致，
      LLM 作为盖在上面的语义解释层。
```

这个定位踩中 2025–26 行业趋势：**底层存储平台化、上层薄 Agent 被收购**（ClickHouse 连收 HyperDX、Langfuse），对个人开发者是利好。

---

# 2. 核心信条

```
让代码处理确定性，让小模型处理简单智能，让大模型处理真正困难的推理。
LLM 是语义层，不是护城河。
```

---

# 3. 垂直能力七层模型（总览）

越靠下越硬、越不依赖 LLM、越是个人能做。详细论据见 V1 计划 3.1 与调研总报告。

| 层 | 名称 | 一句话 | 战略姿态 |
|---|---|---|---|
| **L0** | 数据接入与解析 | 定死事件 Schema、单领域解析到极致、模板聚类去重 | 现在做 |
| **L1** | 确定性检测 | 手写 analyzer、文件内统计（Z-score / 环比） | 现在做 |
| **L2** | 可执行工作流 | runbook / checklist，是可执行 procedure 不是知识库 | 现在做（轻量） |
| **L3** | 记忆与反馈闭环 | 事件分组、事故记忆库、人审回流、golden set | 现在做 |
| **L4** | 可溯源与信任 | 结论挂原始证据、可下钻；只读 + 本地隐私卖点 | 现在做 |
| **L5** | 模型路由 | 多模型路由、型号配置映射，不自研微调 | 简化做 |
| **L6** | 分发 / 工作流嵌入 | MCP Server / CLI | V1.5 |
| **L7** | 企业级护城河 | 100+ connector、驻场、自动拓扑、自研模型、企业数据层 | 不做 |

---

# 4. 系统高层架构

```text
Browser（极简页面）
  → FastAPI（鉴权 / 验证 / 创建任务 / 返回状态）
  → Celery Worker
       Domain Registry（领域目录包）
       Tool Registry（通用数据算子）
       Model Gateway + Router（统一 generate，等级 L0–L3，型号配置映射）
       Policy Engine + Cost Controller（上限 / 预算三检查点）
  → Data Pipeline：读取 → 脱敏 → 解析 → 标准化 → 指标事件
  → Analysis：分组 / 事故归并 → 检测（文件内）→ 复杂度 → Context 蒸馏 → 模型
  → Validate（证据子集校验 / 幻觉拦截）
  → Insight（+ runbook）/ Report
  → 候选知识 → 人审 → confirmed
  → AgentRun 记录（状态 / 成本 / Trace）
```

铁律：**禁止原始日志直接进 LLM；必须先结构化、确定性分析、再蒸馏。**

---

# 5. 技术栈（稳定基线）

```text
Python 3.13 / FastAPI
SQLAlchemy 2.0（Mapped + select）/ Alembic
PostgreSQL 15+（JSONB）
Redis 7 + Celery（+ Beat）
本地磁盘 uploads/ 起步（预留 S3 / MinIO）
pydantic-settings / structlog
Jinja2 + 原生 JS（无构建）
pytest / httpx；Docker Compose
不上 Kubernetes、不微服务、模块化单体
```

模型供应商：通过 OpenAI 兼容端点接入（如 DeepSeek 官方 API），型号仅在 `.env` 映射，业务代码不写死型号、不做微调。

---

# 6. 数据模型总览

V1 为 **9 张表**（字段细节见 V1 计划阶段 02）：

```text
User
Project ── 数据边界（所有业务表带 project_id）
DataSource
Event ──group_id──→ EventGroup（模板 / 时间窗分组：500 告警 → N 组）
EventGroup ──多对多──→ Incident（事故归并：N 组 → 少数事故，跨 Run 可检索）
AgentRun（parent_run_id 串重试；model_calls JSON）
Insight（四层语义 fact/inference/possibility/unknown）
Evidence（event_ids 子集校验）
```

约定：指标 = `Event(event_type="metric")`；模型调用 = `AgentRun.model_calls`；领域知识 = YAML（不建表）。

---

# 7. 版本路线

```text
V1    被动分析：上传 → 带证据的报告（1 领域 / 2 格式 / 只读 / 9 表）
V1.5  主动分析：定时任务、历史 Baseline、自动分片、通知、知识命中计数与自动强化、
              MCP Server / CLI、文件级 Domain 扩展
V2    单源因果：趋势、时间相关性、模式识别、调查计划、连接器、领域 embedding
V2.5  Multi-Agent：仅当单 Agent 被指标证明扛不住
V3    受控执行：Action / 审批 / 权限 / 回滚 / 审计；UI 无代码自建 Domain
```

---

# 8. 跨版本工程红线（始终遵守）

```text
1. 确定性计算不调用 LLM。
2. 原始日志不直接进 LLM，先结构化、再蒸馏。
3. 型号不写进业务代码，只认 L0–L3 + 配置映射。
4. Domain 走 Registry，不在 Runtime 写 if domain。
5. 没有有效 Evidence 的结论不能标 fact。
6. 失败不静默：明确失败或降级，不生成看似正常的假报告。
7. 调用次数 / token / 时长有硬上限，不允许无限循环。
8. 只读定位不破坏；任何写操作走 V3 受控执行。
9. 不为未来提前写复杂代码，只留最小、且本身就是好设计的边界。
10. 每阶段：编码 → 测试 → Commit。
```

---

# 9. 文档地图（索引）

| 文档 | 作用 | 位置 |
|---|---|---|
| 当前实现与审计 | 接线、边界、处置证据 | `docs/architecture.md` / `docs/audit-2026-10-17.md` |
| **本总纲** | 项目全貌、跨版本路线、接续入口 | `Log_Intelligence_Agent_总工程文档.md` |
| V1 详细计划 | V1 阶段施工 / 数据模型 / 验收 | `Log_Intelligence_Agent_V1_个人全栈工程编码计划.md（最新版）.md` |
| V1.5 计划（待建） | 主动分析 / MCP / 基线 | 新窗口按本总纲第 10 节创建 |
| V2 计划（待建） | 因果 / 趋势 / 连接器 | 同上 |
| 调研总报告 | 垂直能力修正框架与来源 | 飞书：`Yl8lblb87okNLWxiKH1caCJunhc` |
| 开源平台调研 | 11 项目体检 | 飞书：`T7EZbFRwXolRpGxL1ZxccRESnPg` |
| 商业产品调研 | 11 产品六层差异 | 飞书：`FP5FbumZLoH68RxAvpjcK0xSnFe` |
| 垂直 Agent 案例 | Harvey / Sierra / Decagon 等 | 飞书：`Jt1Rbh4WwoSIs2xliorc4M1bnId` |
| 社区声音调研 | HN / Reddit / V2EX / 知乎 | 飞书：`Znhgb7CyFobxX6x8AtJcIDLcnBf` |

> 新增任何版本计划后，回到本表补一行——这是防止「文档散落 / 替代丢失」的唯一机制。

---

# 10. 新窗口接续指引（重要）

**开新窗口做 V1.5 / V2 时，按此顺序：**

1. 先读本总纲（定位、七层、红线、版本路线）；
2. 再读 V1 详细计划的「阶段成果」与「不做清单」；
3. 对照下方「待办池」确定该版本范围；
4. 检查「再评估信号」是否真的触发——没触发的不做；
5. 产出新版本计划文件，并回本总纲第 9 节登记链接。

### 10.1 V1 已明确后移到 V1.5 的待办池

```text
定时任务 / Celery Beat 主动触发
历史 Baseline（跨文件、季节性）
自动分片聚合
通知（告警 / Slack / 邮件）
知识命中计数与自动强化
MCP Server / CLI 分发
文件级 Domain 扩展（高级用户放 YAML、人审加载）
```

### 10.2 V2 待办池

```text
趋势分析、时间相关性、模式识别、调查计划
连接器扩展（按「同一源被大量用户卡住」信号驱动）
领域 embedding
```

### 10.3 已决策、勿轻易重开（需强信号）

```text
永久/强信号才重做：自研微调模型、PB 级列存、eBPF 拓扑、
  100+ connector、企业数据统一层、Multi-Agent、微服务 / K8s、
  自动根因闭环、插件市场、自动 LLM Judge
理由与再评估信号见 V1 计划第 20 节，不要重新争论。
```

---

# 11. 待补充 / 未知（新窗口需先确认）

以下会影响工期、成本与范围，目前未知，不应在文档中编造默认值：

```text
- 模型月度预算上限、可接受的单次分析成本
- 目标用户画像细节（自用 / 小团队 / 是否对外）
- 日增日志规模、单文件典型大小
- 各模型官方现价与结构化输出支持（用时以官方现网核实）
```

---

# 12. 维护约定

- 本总纲保持**高层稳定**：只在定位、能力分层、版本路线、红线发生战略变化时更新；
- 实现细节一律进版本计划；
- 每次新增 / 完成一个版本，更新第 7（路线）与第 9（文档地图）；
- 不删除历史版本计划，新版本只追加、不替代。
