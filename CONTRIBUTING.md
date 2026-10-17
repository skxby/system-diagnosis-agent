# 参与贡献（CONTRIBUTING）

先说清楚这个仓库的定位：它是**个人全栈、单领域、只读**的日志分析 Runtime 的 V1，
刻意**不做**很多东西（见编码计划第 20 节的"不做清单"与 README 第 8 节）。
所以在这里，"加功能"不一定受欢迎，**"让它更可信"永远受欢迎**。

---

## 一、最快上手的路径

```bash
git clone https://github.com/skxby/evidencelog && cd evidencelog
cp .env.example .env          # 设置随机 SECRET_KEY；模型 Key 可选
docker compose up -d          # 一键起全栈（首次构建 1-3 分钟）
```

不想用 Docker 跑应用、只想跑测试：`docker compose up -d postgres redis`，
再按 README 第 6 节建 Python 3.13 的 `.venv` 并安装 `requirements.lock.txt`，运行 `pytest -q`。
全量测试自动创建临时 PostgreSQL 数据库并跑迁移，结束后删除；数据库账号需有建库权限，不向开发库写测试数据。
无 PG/Redis 时可先运行 `pytest tests/unit`；完整验收必须运行全量测试。

`.env` 里除了 Key，**型号与单价也要照 `.env.example` 留着**（型号名与单价不是密钥）：
少了它们，成本控制器根本构造不出来，`test_create_run_is_refused_when_budget_is_insufficient`
这条"真机口径"验收就无从执行 —— 它会**显式报错并告诉你要补哪几个变量**，
而不是悄悄跳过（2026-09-24 CI 上就是这么发现的：同一份代码本地 402、CI 202 放行）。

---

## 二、改动前必须先读的两份东西

1. `AGENTS.md` —— 10 条**硬约束**（红线）。违反其中任何一条的 PR 一律不会合：
   确定性计算不调 LLM、型号不进业务代码、没有有效证据不能标 fact、
   失败不静默、脱敏在落盘前、对象边界（9 表 / 7 态 / L0–L3）不擅自改、
   API 不承担长任务、Domain 走 Registry、每阶段"编码→测试→验收→提交"、
   不为未来功能提前写复杂代码。
2. `Log_Intelligence_Agent_V1_个人全栈工程编码计划.md（最新版）.md` ——
   要改某个模块时，先看它当初为什么这么设计、验收标准是什么。

---

## 三、提交前请自检（这几条不是形式主义）

```bash
pytest -q                                   # 必须全绿；不许删/跳过测试来"通过"
python .dsh/settings_usage_audit.py         # 声明了却没人读的配置项：应为"无"
python .dsh/dead_wiring_audit.py            # 写了却没人调用的符号：应为 0（或用 --gaps 说明）
python .dsh/stage_acceptance.py             # 出真机口径验收表（改了行为就要看它）
```

- **测试全绿 ≠ 功能生效。** 这个仓库最贵的一课是：三条成本闸门曾经单测全绿、
  真机上从未被调用过。所以**改动只要碰真实路径，就要给真机证据**
  （见 `真机核验报告-2026-09-24.md`）：落库/落盘的事实、或函数级追踪。
- **不要放宽断言、不要 mock 掉真实依赖、不要绕过 `logs/` 的真实日志。**
  为了让测试通过而削弱它，比测试失败更糟。
- **密钥永远不进仓库。** `.env` 已被 `.gitignore` / `.dockerignore` 挡住；
  单测 `test_no_real_key_anywhere_in_tracked_files` 会扫**全部已跟踪文件**。
  一旦误提交，必须**同时**清历史与轮换 Key（清历史不能代替轮换）。

---

## 四、提交信息与粒度

- 一个逻辑一个 commit，消息第一行是结论（`修复：<什么不生效>`、`功能：<做了什么>`、
  `文档：<改了什么>`），正文写清**为什么**与**怎么验证的**。
- 历史里能直接搜到"某条验收为什么这么定"，这比漂亮的提交列表有用。

---

## 五、Bug 报告请带上这些

1. `docker compose ps` 的输出；2. Run 的 `id` 与页面 `/runs/<id>/detail` 里的复述；
3. 相关 `trace_id`（Web 与 Worker 的日志里都有，一条 grep 能串起整条链路）；
4. 如果是解析/脱敏问题：一段**脱敏后**的样本（不要贴原始敏感内容）。

---

## 六、不接受的改动

- 把 `logs/` 下的语料改成"能被解析"（其中的 Web access 平文日志是**负样本**，
  见 `logs/README.md`；要支持它得**加一个领域**，不是放宽 parser）。
- 为了变"聪明"而引入 Multi-Agent、实时流、图谱等（V1 不做清单里明确排除）。
- 在核心 Runtime 里 `import` 具体领域、或写 `if domain == ...`。
