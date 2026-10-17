# Log Intelligence Agent

[![tests](https://github.com/skxby/evidencelog/actions/workflows/ci.yml/badge.svg)](https://github.com/skxby/evidencelog/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

把日志变成**带证据、可追溯、成本可控**的分析报告。

V1 是个人全栈、单领域、**只读**的日志分析 Runtime：上传日志 → 脱敏 → 解析 →
分组/归并 → 规则 + 模型分层分析 → 带证据的 Insight 报告。
核心不是「更聪明」，而是**受约束、可恢复、可观测、成本可控**。

> 本 README 只写**怎么把它跑起来、怎么用**。架构与阶段设计见
> 《Log_Intelligence_Agent_V1_个人全栈工程编码计划》。

### 文档地图

| 文档 | 什么时候读 |
|---|---|
| `README.md`（本文件） | 想把它跑起来、想用页面走一遍最小流程 |
| `Log_Intelligence_Agent_总工程文档.md` | **新会话 / 新窗口的第一站**：项目最高层总纲，读完即可接续工作 |
| `Log_Intelligence_Agent_V1_个人全栈工程编码计划.md（最新版）.md` | 想知道"为什么这么设计"、要核对某个阶段的验收标准 |
| `阶段验收表-真机口径.md` + `真机核验报告-2026-09-24.md` | 想判断"某功能在真机上到底生效没有"——每条验收 = 测试证据 + 真机接线 + 真机实测 |
| `logs/README.md` | 要改 Parser / 脱敏规则，需要真实日志语料与量化基线 |
| `AGENTS.md` | 要给这个仓库提交改动：10 条硬约束与验收口径 |
| `CONTRIBUTING.md` / `CODE_OF_CONDUCT.md` | 想参与：怎么跑、提交前自检什么、讨论的规矩 |
| `SECURITY.md` | 要报安全类问题，或想知道"哪些数据会离开本机 / 会发给模型供应商" |
| `docs/ui-guide.md` / `docs/api.md` | 网页流程与接口约定 |
| `docs/architecture.md` / `docs/limitations.md` | 当前接线、数据边界与尚未实现能力 |
| `docs/runbook.md` / `docs/audit-2026-10-17.md` | 运维处置、审计核实与验证证据 |
| `.dsh/skills/*/SKILL.md` | 要按既有节奏做加表、验收、提交、成本核算等固定动作 |

> 代码与测试里大量出现「计划第 N 行」这类引用（`tests/datasets/*/expect.json`
> 的 `sources` 字段尤其如此）。**编码计划就在本仓库里**，所以那些引用在
> clone 之后是可核对的 —— 引用一份不在仓库里的文档等于没引用。

---

## 1. 前置要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Docker Desktop | 已安装并**保持运行** | 全栈都在容器里跑 |
| Docker Compose | v2 及以上（`docker compose`，不是 `docker-compose`） | |
| DeepSeek API Key | 一把可用的 `sk-...` | 没有也能起，但模型分析会降级为纯规则报告 |

**只用 Docker 跑的话，本机不需要装 Python。**
（想直跑测试才需要 Python 3.13；不要用 3.14，Celery 官方支持列表到 3.13 为止。）

---

## 2. 一键起全栈

**Windows 最省事的一条路**：装好 [Docker Desktop](https://www.docker.com/products/docker-desktop/) 并让它跑起来，
然后**双击仓库根目录的 `启动.cmd`** —— 它会自己 `docker compose up -d`、等健康检查通过、
再把 `http://127.0.0.1:8000/login` 在浏览器里打开。停止就双击 `停止.cmd`（数据卷保留，上传与分析结果都还在）。

想先看看"跑完一次真实分析长什么样"：

- **不装 Python 也行的路** —— 页面上三步：① 注册一个账号 → ② 新建项目 →
  ③ 在项目页上传一份真语料（用仓库里的 `logs/Mac_2k.log` 就行，2000 行真实系统日志）
  → 点「开始分析」，从状态页进入报告。L0 显示规则统计；需要模型的分支取决于模型配置与服务可用性。
- **想一条命令备好**（需要宿主 Python，见第 6 节建 `.venv`）：

```powershell
.venv\Scripts\python.exe .dsh\demo_setup.py     # 建好演示账号 + 一份已跑完的报告，并打印可点的链接
```

这条命令建出来的演示账号是 **`demo@evidencelog.local` / `evidencelog-demo-2026`**
（只在本机数据库内；首次初始化可注册账号，后续注册默认关闭。口令至少 8 个字符，UTF-8 编码不超过 72 字节）。

下面是手工版（Linux / macOS / 想自己控制每一步时用）：

```bash
git clone <本仓库>
cd <仓库目录>

# ① 准备配置
cp .env.example .env
```

打开 `.env`，设置随机会话密钥；需要模型时再填写真实 API Key：

```dotenv
MODEL_PROVIDER_API_KEY=sk-你的真实Key
SECRET_KEY=换成一串你自己的随机值
```

SECRET_KEY 必须为至少 32 字符的随机值；Windows 启动脚本会自动生成。
模型名称、推理档位和价格由 `.env.example` 提供示例，代码默认名称为空、价格为 0；付费分析必须按供应商实际配置。
未配置任何有效模型凭据时，Web 仍可启动，L0 正常完成；需要模型的分析会明确失败并保留规则报告。
**`.env` 已被 `.gitignore` 与 `.dockerignore` 双双挡住 —— 不会进 Git，也不会进镜像。**

```bash
# ② 起全栈（首次会构建镜像，约 1-3 分钟）
docker compose up -d

# ③ 确认都健康
docker compose ps -a
```

期望看到：

```
logagent-postgres   Up (healthy)
logagent-redis      Up (healthy)
logagent-migrate    Exited (0)      ← 迁移跑完即退出，这是正常的
logagent-web        Up (healthy)
logagent-worker     Up
logagent-beat       Up
```

浏览器打开 **http://127.0.0.1:8000** 。

> 端口只绑 `127.0.0.1`，不暴露到局域网 —— 与「只读 + 本地」的定位一致。
> 需要远程访问请自行配端口转发，并自行承担暴露风险。

---

## 3. 最小使用流程（全程不碰命令行）

1. **登录** —— 首次使用点「首次使用？注册这个邮箱」，用同一个邮箱 + 口令即可。
2. **建项目** —— 项目是数据边界，所有日志与结论都归属某个项目。
3. **上传日志** —— 选 `.txt`（syslog / log4j / Apache error 三类行结构）或 `.jsonl`（逐行 JSON）。
   上传后页面会显示**解析统计**（总行 / 成功 / 坏行 / 跳过 / 合并）与**脱敏计数**。
   坏行会列出样本 —— 不会被静默丢弃。
   > 边界提示：**Web access 平文日志不属本领域**（那是"站点访问"语义，不是"主机/进程监控"），
   > 整批会计入坏行 —— 这是有意为之，负样本见 `logs/nginx_plain.log` 与 `logs/README.md`。
   > 要让它能解析得**加一个领域**（新目录包 + 注册一行），而不是放宽现有 parser。
4. **发起分析** —— 选数据源，可留空时间范围。API **立即返回** `run_id + status`，正常为 queued。时间窗、来源、严重度、类型和关键词均在 SQL 筛选阶段生效。派发失败会显示 timeout / dispatch_failed。
5. **看状态** —— 状态页每 3 秒刷新，显示阶段进度、token、成本、心跳。
6. **看报告** —— 结论按四层语义着色：
   - `✓ 确认`（fact）：**证据可展开逐条核对**，点开能看到关联事件 id、时间和脱敏后的内容
   - `→ 推测`（inference）：附推理过程
   - `? 可能`（possibility）：附局限说明
   - `− 未知`（unknown）：附信息缺口

   命中异常时会附上对应的**处置步骤（runbook）**，V1 只读展示、不自动执行。
7. **审核知识** —— 报告页下方「待确认知识」列出分析中发现的候选；
   可以确认 / 编辑 / 丢弃，或点「这其实不是问题」记为误报。
   确认后写入数据卷，下次分析自动加载。

**部分成功与失败会显著标注，并都能一键重新分析**：
`partial_success` 顶部显示「⚠ 结果不完整」及中断原因；`failed` / `timeout`
显示失败阶段与原因。两种状态下都有「重新分析（新建 Run）」按钮 ——
它按**原 Run 存下来的输入快照**新建一个 Run（不用你回项目页重填时间窗，
填得不一样就不是同一次分析了），新 Run 用 `parent_run_id` 指回原 Run。

> `completed` 的 Run **不提供**重新分析：同样的输入再跑一遍只是重复计费。
> 要换口径请回项目页调整时间窗或过滤条件后重新发起。

---

项目页的「分析记录」可回到历史状态和报告，支持直接收藏 Run 链接。
「事件分组与事故记忆库」提供归并结果与事件下钻；事故不代表根因。

## 4. 环境变量说明

完整清单见 `.env.example`。关键项：

| 变量 | 模板值 / 运行约定 | 说明 |
|---|---|---|
| `DATABASE_URL` | `...@127.0.0.1:5432/logagent` | 宿主机直跑用。**compose 里会被覆盖成 `@postgres:5432`** |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | 同上，compose 里覆盖成 `@redis:6379` |
| `POSTGRES_PASSWORD` | `logagent_dev_pw` | compose 与 `DATABASE_URL` 共用；换它要一起换 |
| `SECRET_KEY` | 空；启动脚本生成随机值 | Web 启动拒绝空值、公开占位符和不足 32 字符的密钥 |
| `ALLOW_REGISTRATION` | `false` | 只允许空库首次初始化注册；显式开启后允许其他账号注册 |
| `COOKIE_SECURE` | `false`（本机 HTTP） | HTTPS 部署设置 true |
| `DEFAULT_DOMAIN_ID` | `computer_monitoring` | Registry 中的默认领域，业务代码不写死名称 |
| `MODEL_PROVIDER_BASE_URL` | `https://api.deepseek.com` | OpenAI 兼容端点 |
| `MODEL_PROVIDER_API_KEY` | 空 | 供应商密钥。留空会继续查找别名环境变量和 DSH 凭据；都找不到时需要模型的分支失败并保留规则报告 |
| `MODEL_L1/L2/L3` | `deepseek-flash` | 等级 → 型号映射。**型号只出现在这里**，业务代码只认 L1–L3 |
| `MODEL_L*_REASONING` | `off` / `low` / `high` | 合法值只有 `off`/`low`/`high`/`max`；`medium` 是非法值 |
| `MODEL_L*_PRICE_*_PER_1M` | `1` / `4` | 单价，**元 / 每百万 token**；用于成本核算 |
| `MODEL_CACHE_HIT_INPUT_PRICE_PER_1M` | `0.02` | 缓存命中的输入单价（比输入价低一个数量级）。按命中 token 数单独计费，不配则按正常输入价 |
| `MODEL_PEAK_PRICE_MULTIPLIER` | `2` | 高峰时段倍数（窗口＝工作日 9–12、14–18 点，按 `DEFAULT_TIMEZONE`）；单价按闲时价填，填 1 表示不区分峰谷 |
| `MONTHLY_BUDGET` / `DEFAULT_RUN_MAX_COST` | `10` / `0.30` | 月度预算 / 单次 Run 上限，货币单位统一为**人民币元**；`0` 表示该项不设上限（单次为 `0` 则是"不许花钱"） |
| `ZOMBIE_REAP_INTERVAL_SECONDS` | `60` | 僵尸 Run 的周期扫描间隔（beat 任务）。心跳超时是 300s，扫描必须比它勤 |
| `DATA_DIR` | `./data` | 运行时数据（知识库 confirmed/staging）；compose 里是 `/app/data`（卷） |
| `DEFAULT_TIMEZONE` | `Asia/Shanghai` | 日志时间戳缺时区时按它解析，再转 UTC 存库 |

### 密钥放哪

优先级：**显式传入 > Settings（环境变量优先于 `.env`）> 别名环境变量 > `$DSH_HOME/.credentials.yaml`**。
第三条是为了复用已在 DeepSeek Harness 里配好的 Key，避免同一把 Key 存两份。
凭据仅在真正调用模型时解析；L0 不初始化网关。认证/参数错误不重试并明确失败，网络故障可重试后降级为 partial_success。两种情况均保留规则报告。

---

## 5. 常用操作

```bash
# 看日志（JSON 行，带 trace_id —— 排障时按 trace_id 一 grep 就能串起整条链路）
docker compose logs -f web
docker compose logs -f worker

# 只重启应用（改代码后）
docker compose up -d --build web worker

# 手动跑迁移
docker compose up migrate

# 进数据库看一眼
docker compose exec postgres psql -U logagent -d logagent -c '\dt'

# 停止（保留数据）／清空数据
docker compose down
docker compose down -v
```

**接口文档**：http://127.0.0.1:8000/docs

---

## 6. 不用 Docker 直跑（开发 / 跑测试）

```bash
# 只起基础设施
docker compose up -d postgres redis

python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.lock.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.lock.txt    # macOS / Linux

alembic upgrade head
pytest -q                                          # 单测 + 集成测试
uvicorn app.main:app --reload                      # http://127.0.0.1:8000
celery -A app.celery_app worker -P solo -l info    # Windows 必须 -P solo
celery -A app.celery_app worker -P solo -l info -Q maintenance -B   # 僵尸回收（beat + 维护队列）
```

> 第三个进程不是可选的：**僵尸 Run 的回收靠它**。分析 worker 挂了正是它要处理的场景，
> 所以它必须独立于分析 worker（走 `maintenance` 队列），否则回收任务会和分析任务
> 一起被卡住 —— compose 里这件事由 `logagent-beat` 容器负责。

### 跑 Golden Set

五个固定场景（正常 / CPU 异常 / 内存增长 / 崩溃 / 坏格式），逐条对照
`tests/datasets/<场景>/expect.json` 里的期望：

```bash
pytest tests/golden -q
```

期望值是可编辑的 JSON，每条都带 `sources` 字段注明依据（对应编码计划哪一行）。
默认**不调用模型**，所以跑一次是零成本、结果可复现。

### 真实模型连通性验证（会产生费用）

默认跳过。要跑需显式开启：

```bash
RUN_LIVE_MODEL_TESTS=1 pytest tests/integration/test_gateway_live.py -v -s
```

### 评测与验收脚本（`.dsh/`）

下面这些脚本里，**只有两处会产生模型费用**：`golden_eval.py` 加 `RUN_GOLDEN_LIVE=1`、
以及 `real_path_evidence.py` 的默认模式；其余都是确定性、可复现、零成本的。

| 脚本 | 作用 | 命令 |
|---|---|---|
| `.dsh/parser_eval.py` | 用 `logs/` 下 8 份真实日志量化可解析率与脱敏误伤率 | `python .dsh/parser_eval.py` |
| `.dsh/golden_eval.py` | 跑 Golden Set 五个场景并打印逐条断言与成本 | `python .dsh/golden_eval.py`（加 `RUN_GOLDEN_LIVE=1` 走真模型，**付费**） |
| `.dsh/stage_verify.py` | 按「阶段 → 承载验收的测试文件」出可核对的计数表 | `python .dsh/stage_verify.py` |
| `.dsh/stage_acceptance.py` | **出「真机口径」阶段验收表**：每条验收 = 测试证据 + 真机接线 + 真机实测 | `python .dsh/stage_acceptance.py` |
| `.dsh/wiring_graph.py` | 从真实入口点做调用图可达性；`--gaps` 穷举"写好了但没接上"的符号（含方法级） | `python .dsh/wiring_graph.py --gaps` |
| `.dsh/live_path_trace.py` | 给 Web / Worker 装函数级 tracer，记录一次真实分析**实际执行了哪些函数** | `python .dsh/live_path_trace.py web 8001` / `... worker` |
| `.dsh/real_path_evidence.py` | 真机运行时证据采集（落库、落盘、契约、上传、取消、僵尸、E2E+Golden 汇总） | `python .dsh/real_path_evidence.py [--contract\|--uploads\|--cancel-test\|--zombie-start\|--sweep]` |
| `.dsh/budget_gate_check.py` | 真机核验成本闸门：项目预算 / 金额精度 / 未设上限 / **月度预算跨月** / **峰谷价口径** | `python .dsh/budget_gate_check.py` |
| `.dsh/final_verify.cjs` | 对着**真跑起来的全栈**做 16 项端到端检查（真实 HTTP） | `node .dsh/final_verify.cjs` |
| `.dsh/dead_wiring_audit.py` | 扫"顶层符号没人用 / 只有 tests 在用"（方法级缺口看 `wiring_graph.py --gaps`） | `python .dsh/dead_wiring_audit.py` |
| `.dsh/settings_usage_audit.py` | 逐个确认 `.env`/Settings 里的配置项**真的被读过** | `python .dsh/settings_usage_audit.py` |
| `.dsh/runbook_probe.py` | 重放真机事件，看 runbook 为什么没挂上 | `python .dsh/runbook_probe.py <project_id>` |
| `.dsh/cancel_stale_probe.py` | 复现"取消慢一步"的机制（Worker 长事务的行锁） | `python .dsh/cancel_stale_probe.py <run_id>` |
| `.dsh/history_probe.py` | 同项目再跑一次，看历史事故有没有真的注入 Context | `python .dsh/history_probe.py <project_id>` |
| `.dsh/knowledge_loop_probe.py` | 知识闭环：候选 → 经接口确认 → 下次分析是否加载 | `python .dsh/knowledge_loop_probe.py <project_id>` |
| `.dsh/edge_scenarios.py` | 边界场景：迁移回滚 / 模型不可用降级 / 假 event_id / 极小预算 partial / L1 样本 / 重试入口 | `python .dsh/edge_scenarios.py --migration`（其余模式见文件头） |
| `.dsh/stub_provider.py` | 本地桩供应商（OpenAI 兼容）：造假 event_id、可控 token 用量，**零费用**验异常输入 | `python .dsh/stub_provider.py` |
| `.dsh/fresh_clone_check.py` | 全新 clone → 一键起全栈 → 16 项 E2E → 原栈恢复（会停/起 docker） | `python .dsh/fresh_clone_check.py [--remote <url>]` |
| `.dsh/demo_setup.py` | 备好一个**可直接点开**的演示环境（已知账号 + 项目 + 真实语料 + 一次跑完的分析），并打印链接 | `python .dsh/demo_setup.py [--log logs/Linux_2k.log] [--no-run]` |
| `.dsh/run_status.py` | 看某条 Run 的状态、心跳、花费与中断原因 | `python .dsh/run_status.py [run_id …]` |
| `tests/unit/test_web_ui_contract.py` | 核对「页面 JS 调用的接口」与真实路由逐条对齐（阶段 11「全程不碰命令行」的机器化检查） | `pytest tests/unit/test_web_ui_contract.py -q` |

最近一次实测（2026-09-24，真机口径见《[阶段验收表-真机口径.md](阶段验收表-真机口径.md)》）：

```text
解析率   域内 5 份系统日志 10000/10000 = 100%；含负样本 nginx_plain 11500/12500 = 92.00%
脱敏     parser_eval(mask_ip=True) 替换 12268 次；11575 个排障字段（uid=/pid=/HTTP 状态码/请求路径/容器 ID）零误伤；Web 默认 mask_ip=False，可勾选开启
Golden   离线 18/18 断言 ¥0
E2E      容器全栈 16/16；真实调用 1607/3058 tokens、¥0.0138、7 条结论
真机分析 真实语料 Mac_2k 2000 事件 → 15 结论 15 证据、1848 个事件分组、593 条事故
成本闸门 真实花掉 ¥0.0167 → 库里「本月已花」同步 → 再发起被 402 拦下；上月花满 → 放行
可靠性   分析中调取消 → running→cancelled；冻结 Worker → 维护队列把 Run 收成 timeout
降级     Key 换成无效值 → partial_success + model_unavailable + 纯规则报告（¥0，不再假装"没发现异常"）
重建     临时库 upgrade → downgrade base → upgrade 全通过；不填时间范围的 Run 也能「重新分析」
知识闭环 候选 4 条进 staging → 确认一条 → 下一次分析加载 confirmed 知识 1 条
工具用量 Run.tool_usage 记录 event_filter / stats_calculator 的耗时与输入条数
异常输入 桩供应商造假 event_id → 被拦下并重试；极小单次上限 → partial_success（预算到顶）
分档     真实样本覆盖 L0（空源）/ L1（少量单一错误）/ L2→L3（有 high 异常）
交付     全新 clone → 一键起全栈 → 16/16 端到端（含一次真实分析 ¥0.015）→ 原栈恢复
全量     pytest 753 passed / 3 skipped（跳过的是需 RUN_LIVE_MODEL_TESTS=1 的付费用例）
验收     60 条验收：60 达标 / 0 待实测 / 0 真机未达标
```

> ℹ️ 这张表的口径是**每条验收 = 测试证据 + 真机接线 + 真机实测**，
> 任何一条都不许只凭"测试全绿"给 ✅ —— 上一轮的 14 个 ✅ 就是这么漏掉阶段 07 的。
> 首轮查出 11 条"测试全绿但真机没生效"，四批修复后全部转正；
> 逐条的根因、修法与真机复验证据见
> 《[真机核验报告-2026-09-24.md](真机核验报告-2026-09-24.md)》。

> `logs/` 是外部公开真实日志语料（出处见 `logs/README.md`），已在版本库内，
> 故**全新 clone 也跑得了这些基线**（`.gitattributes` 对 `logs/*.log` 关掉了
> 换行符规范化：语料字节不能被悄悄改写，否则基线不可比）。

---

## 7. 排障

| 症状 | 处理 |
|---|---|
| `docker compose ps` 里 migrate 显示 `Exited (0)` | **正常**：迁移跑完即退出，web/worker 等它成功后才启动 |
| 上传整批计为坏行（如 Web access 平文日志） | **不是 bug**：本领域只认 syslog / log4j / Apache error 与逐行 JSON，见 §3 的边界提示 |
| web 起不来，日志显示连不上数据库 | 确认 postgres 是 `healthy`；`docker compose logs postgres` |
| 模型分析总是降级成纯规则报告 | `.env` 里 `MODEL_PROVIDER_API_KEY` 是否为空／是否有效；此时 Run 会是 `partial_success` + `model_unavailable`（不再假装"没发现异常"） |
| 报告里没有结论 | 看 Run 状态：`partial_success`/`failed` 会写明中断原因；L0（正常日志）本就无异常结论 |
| 明明预算不够，发起分析却成功了（本该 402） | 看日志有没有 `成本闸门未接入：型号未配置` / `成本闸门单价全是 0`：这两种情况下 Pre-check 要么缺席、要么估出 0 元，**都会放行**。按 `.env.example` 补齐 `MODEL_L1/L2/L3` 与单价即恢复（型号名与单价不是密钥；CI 里就配在 `.github/workflows/ci.yml`） |
| 想确认某次分析到底做了什么 | 打开 `/runs/<id>/detail?project_id=<pid>`，页面会复述阶段、花费、结论与证据 |
| 端口被占用 | 改 compose 里的端口映射；注意只绑 `127.0.0.1` |
| Celery 在 Windows 卡住 | 直跑时必须 `-P solo`（compose 里已加） |
| 跑测试时看到 `StarletteDeprecationWarning: ... httpx ... install httpx2` | **已知上游弃用，暂不迁移**：换 httpx2 会波及所有用 `TestClient` 的集成测试。已在 `pytest.ini` 里显式标为已知告警（这样出现别的告警时一眼能看见），等 Starlette 正式切换再动 |
| Worker 崩了，Run 卡在 running/queued | beat 容器上的维护队列会按心跳超时收成 `timeout`（间隔见 `ZOMBIE_REAP_INTERVAL_SECONDS`）；日志搜 `zombie_runs_reclaimed` |

---

## 8. V1 有意不做的事

这些不是遗漏，是**刻意不做**（完整清单与再评估信号见编码计划第 20 节）：

- 跨文件 / 长期历史 Baseline、季节性建模（只做文件内统计）
- 自动分片聚合、实时流处理、Multi-Agent
- 自动执行处置步骤（只读定位：runbook 仅展示，不自动跑命令）
- CSV / JSON 导入（V1 只支持 TXT / JSONL）
- Kubernetes、多环境流水线、复杂计费与角色权限
- 自动化 LLM Judge（Golden Set 先人工核对）

---

## 9. 许可与参与

- **许可**：[MIT](LICENSE)。仓库内 `logs/` 的日志语料来自公开数据集，
  出处与许可见 `logs/README.md`。
- **参与**：先读 [`CONTRIBUTING.md`](CONTRIBUTING.md)（提交前自检清单）与
  [`AGENTS.md`](AGENTS.md)（10 条硬约束）；讨论请遵守
  [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。
- **提交前请记住这个仓库最贵的一课**：*测试全绿 ≠ 功能生效*。
  三条成本闸门曾经单测全绿、真机上从未被调用过 ——
  所以碰真实路径的改动，请给出真机证据（落库/落盘事实或函数级追踪），
  方法与工具见 `真机核验报告-2026-09-24.md` 与 `.dsh/`。
