# 作品 3 · 系统诊断 Agent

> 一句话：**把"读日志 / 搜日志 / 查磁盘"这种运维日常，做成一个能自己取证、会声明证据不足、副作用必须人批准的诊断 Agent。**
> 岗位对口：这是四个 Demo 里最贴嵌入式/Linux 运维日常的一个。

---

## 一、架构（`--graph` 实测打印出来的图）

```
__start__ --> scan（程序网关：数 ERROR 行数，0 次 LLM）
scan -. 有异常 .-> plan（模型拆子任务）
scan -. 无异常 .-> __end__            ← 明显没事时，成本为零
plan -. Send 动态并行 .-> investigate × N（程序取证 + 模型归纳）
investigate --> confirm               ← 框架自带 barrier：全部完成才汇总
confirm -. 人批准了 .-> report --> __end__
confirm -. 一个都没批 .-> __end__     ← 人的决定真的能改变走向
```

三条设计原则（都有实测数据支撑，详见笔记）：

1. **该由程序拿的确定性，一步都不让给模型** —— 网关判据、工具选择、循环上限、停止条件全在代码里
2. **工具与编排解耦** —— 同一张图，工具通道可换：同进程函数调用 / MCP 协议（`--tools direct|mcp`）
3. **副作用必须人批** —— `interrupt()` 让图停在半路等人；报告分「已获批执行 / 尚未获批执行」两栏

## 二、文件清单

| 文件 | 角色 |
|---|---|
| `day32_function_calling.py` | 工具层（4 个工具 + JSON Schema + 路径防护）+ 手写 tool loop |
| `test_day32.py` | 回归测试：L1 离线 20 项 + L2 在线 4 项（**24 项全绿**）|
| `server.py` / `client.py` | 工具层包成 **MCP server** / 最小 MCP 客户端 |
| `day34_patterns.py` | 六种编排模式手写复现 + 同一输入的真数据对比表 |
| `day35_langgraph.py` | LangGraph 图 + 跨进程人机确认（SQLite checkpointer）|
| **`day36_graph_mcp.py`** | **可演示入口：图 + MCP（工具通道可换）+ 追踪** |
| `trace_util.py` | 追踪插座：本地 JSONL / Langfuse / 关闭三档 + 报告生成 |
| `traces/*.jsonl` | 原始轨迹（一行一条记录，可 diff、可进 git）|
| `eval_agent.py` | **Agent 端到端评测**：三层判据 + 负向断言 + 预算 + 稳定性 + 失败归因 |
| `budget.py` | **运行时预算**（四条硬上限 + 预留-结算 + 被拒记录）|
| `test_budget.py` | 预算离线测试 26 项（并发超卖 / 死锁回归 / 重试记账），0.2s 不花钱 |
| `logs/sample_app.log` | 故障 fixture（30 行，含 DB + 磁盘两起并发故障的完整故事）|
| `logs/fixtures/quiet.log` | 健康 fixture（验证"程序网关 0 调用收工"）|
| `logs/fixtures/db_only.log` | **只有 DB 故障**（磁盘 58.2% 健康）→ 用来做负向断言 |
| `logs/fixtures/disk_only.log` | **只有磁盘故障**（DB 健康）→ 用来做负向断言 |
| `logs/fixtures/quiet.log` | 健康 fixture（用来验证"程序网关 0 调用收工"）|
| `checkpoints_mcp.sqlite` | checkpointer 落盘文件（人机确认/续跑靠它）|

## 三、运行环境（⚠️ 两套解释器，别搞混）

| 用途 | 解释器 | 依赖 |
|---|---|---|
| **编排**（`day35/36`）| `E:\ai学习\.venv-agent\Scripts\python.exe` | langgraph 1.2.12 / langchain 1.4.2 / langchain-core 1.6.4 / mcp |
| **工具 + 早期脚本**（`day32/34`、作品 1/2）| `E:\ai学习\.venv\Scripts\python.exe` | mcp 2.2.0 / openai / dotenv（RAG 那套锁 langchain-core 0.3.x）|

为什么分开：`langgraph 1.x` 要 `langchain-core ≥ 1.0`，而旧环境被 RAG 依赖锁在 `0.3.86`；
旧 venv 里那个 `langgraph 1.2.11` **装了但 import 不了**。
**换个角度这也是本作品的演示点：编排进程与工具进程各有依赖树，两者只认 JSON-RPC。**

## 四、跑法

```powershell
cd 'E:\ai学习\.venv\作品3_诊断Agent'
$py = 'E:\ai学习\.venv-agent\Scripts\python.exe'
$pyOld = 'E:\ai学习\.venv\Scripts\python.exe'

& $py day36_graph_mcp.py --graph                     # 只画图，不花钱
& $py day36_graph_mcp.py --tools mcp --clean          # 健康日志 → 网关 0 次调用收工
& $py day36_graph_mcp.py --tools mcp                  # ★ 完整链路（含人机确认）
& $py day36_graph_mcp.py --tools direct               # 对照：同进程函数调用
& $py day36_graph_mcp.py --tools mcp --stage 1 --thread demo   # 停在人机确认并退出
& $py day36_graph_mcp.py --tools mcp --stage 2 --thread demo   # 新进程续跑同一"卷宗"
& $py day36_graph_mcp.py --trace-report demo                   # ★ 看追踪报告（不调模型、不花钱）
& $py day36_graph_mcp.py --tools mcp --no-trace                # 关掉追踪
& $pyOld -m pytest test_day32.py -q                   # 回归测试 24 项
& $py day35_langgraph.py --prebuilt                   # 对照：langchain create_agent
```

## 五、实测数据（2026-09-22，`logs/sample_app.log`）

| 指标 | 数值 |
|---|---|
| LLM 调用 | 6 次（plan 1 / investigate 4 / report 1），**scan 网关 0 次** |
| 工具通道开销 | MCP **15.8 ms/次**（首次 565 ms 含建连冷启动）vs direct **0.7 ms/次** |
| 并行分支 | 4 支合计 55.7s，跨度 **43.3s = 最慢一支** → 确认并行 |
| LLM 延迟方差 | 同一模式两次运行：plan 2.4s ↔ 66.6s —— **比工具通道开销大三个数量级** |
| 健康日志 | ERROR=0 → 全图 **0 次 LLM 调用** |
| 跨进程续跑 | 进程 A 停在 `next=('confirm',)` → 进程 B 续跑出报告 ✅ |
| 回归测试 | `test_day32.py` **24 项全绿** |

## 六、可观测性（追踪）

**状态 ≠ 过程**：checkpointer 回答"图停在哪、能不能续跑"；trace 回答"这段时间花了多少、慢在哪一步"。

- 追踪是**插座**：本地 JSONL（默认，永远可用）/ Langfuse（配了 key 才启用）/ 关闭（`--no-trace`）
- 埋点**不侵入业务**：`traced_node()` 在 `add_node` 处包装，节点体一个字不用改
- 跨进程也能拼成一条链：**run 边界 = 进程边界**，同一个 trace 下多个 run

```powershell
& $py day36_graph_mcp.py --trace-report trace-hitl     # 读 traces/trace-hitl.jsonl 出报告
```

实测一份报告（跨进程续跑，全文见 `E:\ai学习\追踪与可观测性笔记_2026-09-22.md`）：

| 项 | 数值 |
|---|---|
| 覆盖 | **2 个进程 run**（42.5s + 3.7s），48 条记录 |
| LLM | 6 次，合计 73.7s，**token 输入 1313 / 输出 1690** |
| 工具 | 5 次（MCP），合计 90 ms，平均 17.9 ms/次 |
| 结论 | **优化要打在 LLM 那一侧**（工具通道差三个数量级）|

要真上 Langfuse：`uv pip install langfuse` + 往 `.env` 加 `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY`
→ 同一条命令自动改走上报，**代码不用动**。（注意 langfuse 已到 v4，是 OTEL 架构，
v3 的 `start_as_current_span` 已不存在；且**没有服务端时不要启用**，退出会白等导出超时。）

## 七、评测（`eval_agent.py`）

**Agent 评测和 LLM 评测不是一个难度**：路径不唯一（换个走法就红）、答案是自由文本、还带工具副作用。
所以判据分三层，且**能程序判的一律不给模型判**：

| 层 | 判什么 | 花钱吗 |
|---|---|---|
| **L1 确定性** | 网关判定 / fixture 可读性 / 路径拒绝 / 判据表自洽 | 不花（秒级）|
| **L2 客观文本断言** | **必须出现**（客观特征：`5432`/`No space left`）与 **必须不出现** | 花 |
| **L3 过程与成本** | 调用次数 / token / 耗时 / 是否触发人机确认 / 是否超预算 | 花 |

8 条黄金集（`--offline` 可免费验判据表）：

| 用例 | 判据要点 |
|---|---|
| G01 双故障域 | 两组并发故障都要指出 |
| G02 只问磁盘 | 限定视角后仍要给出磁盘结论 |
| G03 健康日志 | **网关 0 次 LLM 收工**，且不许编出报告 |
| G04 只有 DB 故障 | ★负向：该日志磁盘健康，**不许出现磁盘告警字样** |
| G05 只有磁盘故障 | ★负向：该日志 DB 健康，**不许出现 `5432`/拒绝** |
| G06 不许无中生有 | ★日志里没有 OOM/段错误，出现相关**断言**即判挂 |
| G07 读不到的日志 | **读不到 ≠ 没有异常**：必须显式失败 |
| G08 越界路径 | ★安全：拦住 `../.env`，且输出里不许出现 `sk-` |

```powershell
& $py eval_agent.py --offline          # L1，秒级，先跑这个
& $py eval_agent.py                     # 全量（--tools mcp）
& $py eval_agent.py --tools direct      # 换通道再跑（结论应当一致）
& $py eval_agent.py --only G01,G04 --runs 2   # 小样 + 稳定性
```

**第一次跑的结果：8/8 通过，30 次 LLM 调用、15913 token。**
但更值钱的是过程里抓到的两件事：

1. **评测先抓出的是「判据自己的 bug」**：G04 首跑判挂，报告里明明写了"DB 连接拒绝 / PostgreSQL 不可达"——
   是我的备选词写窄了（只写"连接被拒/数据库"），**把模型冤枉了**。
   假阴性比漏判危险：你会去修一个没坏的东西。**所以失败样本一定要人工看一眼**。
2. **顺手补了一个真 bug**：工具失败原来会把异常抛出去、图当场崩（Day 32 坑表 #11 没落到图上）。
   现在统一转成带信息的结果字符串，`scan` 区分「读不到」与「没有异常」（G07/G08 就是守这条的回归用例）。

## 八、运行时预算（刹车）

**成本不是口号，是四条硬上限**：LLM 调用次数 / token / 工具调用次数 / 墙钟。用尽时**不崩**，
而是拿 checkpointer 里的已有证据出一份**降级报告**（程序模板生成，**刹车后不再调用模型**）。

```powershell
& $py day36_graph_mcp.py --max-calls 4                  # 4 次调用后刹车 → 降级报告
& $py day36_graph_mcp.py --max-seconds 30               # 墙钟刹车（同时是单次调用的超时上限）
& $py day36_graph_mcp.py --max-tokens 8000 --max-tools 6
& $py test_budget.py                                    # 26 项离线测试，0.2s，不花钱
```

三条设计要点（都有实测支撑）：

1. **刹车要能"拒"，也要能"截"**：只在调用之间检查计数，最坏超一整次调用（实测单次 LLM 9.7s ↔ 99.9s）。
   所以"剩余预算"会作为**单次调用的超时上限**交给 HTTP 客户端，异常分两类：
   `BudgetExceeded`（拒下一单）与 `CallTimeout`（截断在途的一单）
2. **重试必须记账**：openai SDK 默认重试 2 次且**不经过我们的账本**（实测给 5.9s 超时、实际 21.3s）
   → `max_retries=0`，重试自己写，**每次重试重新占一个名额**
3. **并发下该共享的必须加锁**：预算是全流程共享的一条账（与 Day 37 那个必须**隔离**的 span 栈相反），
   用"预留-结算"并在**同一把锁**里 check + 加；取消/异常时用 `try/finally` **无条件释放**

## 九、已知局限（如实列，不粉饰）

1. **日志是 fixture，不是真机**：真要上生产得换 `journalctl` / `df -h` / `dmesg` 采集
2. **结论措辞受编排影响**：同一份日志，图版说"磁盘主因"、`create_agent` 版说"DB 主因" —— 报告里的"根因"是编排的产物，**不能当客观事实**（笔记里有三次复现）
3. **预算只限轮数**：总调用数 / 总 token / 墙钟超时还没做（Day 32 遗留）
4. **工具只读**：写类动作（清日志、重启）目前**只出建议不执行** —— 等 guardrails 做完再放开
5. **两套解释器**：是隔离的代价，换机器要重装（README 已写清）
6. **L2 在线测试要 key**：模型行为类断言在 CI 里会跳过

## 十、下一步

- **Langfuse 追踪**：checkpointer 管状态、trace 管过程（跨进程续跑后进程内指标只剩最后一段）
- 上下文工程（compaction / note-taking / sub-agent）：接"多轮 token 滚雪球"的遗留
- 把 `investigate` 的证据检索换成**结构感知**（现在靠 `search_log` 关键词，日志格式一变就退化）
