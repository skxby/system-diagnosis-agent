# 项目指令（AGENTS.md）——Log Intelligence Agent V1

> DSH 会在每次会话开始自动加载本文件。它是一份「路由文件」，不是超级提示词。
> 详细阶段要求看《Log_Intelligence_Agent_V1_个人全栈工程编码计划.md（最新版）.md》，
> 跨版本总纲看《Log_Intelligence_Agent_总工程文档.md》。按需查阅，不要整篇塞进上下文。
> 保持本文件 ≤ 150 行；新增内容优先加到「硬约束」或指向子文档。

## 项目一句话

个人全栈、单领域、只读的**日志分析 Agent Runtime**：上传日志 → 脱敏 → 解析 → 分组/归并 →
规则+模型分层分析 → 带证据的 Insight 报告。核心不是「更聪明」，而是**受约束、可恢复、可观测、成本可控**。

## 先看哪份文档

| 什么时候读 | 读什么 |
|---|---|
| 接手 / 新会话第一站 | `Log_Intelligence_Agent_总工程文档.md` |
| 要核对某阶段的验收标准 | `Log_Intelligence_Agent_V1_个人全栈工程编码计划.md（最新版）.md` |
| 想把它跑起来 / 用页面走一遍 | `README.md` |
| **要判断"某功能真机上到底生效没有"** | `阶段验收表-真机口径.md` + `真机核验报告-2026-09-24.md` |
| 核对当前审计修复与剩余边界 | `docs/audit-2026-10-17.md` + `docs/architecture.md` + `docs/limitations.md` |
| 改 Parser / 脱敏规则 | `logs/README.md`（真实语料、基线、已知误伤） |
| 固定动作（加表/验收/提交/成本/评测） | `.dsh/skills/*/SKILL.md` |

## 技术栈（不要替换）

- 后端：FastAPI + SQLAlchemy 2.x + Alembic + Celery（Windows 必加 `-P solo`）+ Psycopg3
- 存储：PostgreSQL + Redis（用 Docker Compose 起；本机无 PG/Redis）
- 语言：Python 3.13（**不要用 3.14**，Celery 不支持）
- 前端：Jinja2 + 原生 JS（不引入构建工具）
- 模型：`deepseek-flash`（OpenAI 兼容端点，型号只写在配置层）

## 首次运行与验证命令

```bash
docker compose up -d postgres redis          # 只起基础设施
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows；无 pyproject，别用 pip install -e ".[dev]"
alembic upgrade head
pytest -q
uvicorn app.main:app --reload                # /healthz /healthz/deps /docs
celery -A app.celery_app worker -P solo -l info
celery -A app.celery_app worker -P solo -l info -Q maintenance -B   # 僵尸回收（beat + 专用队列）
```

最省事的路径是 `docker compose up -d`（README 第 2 节），本机不需要装 Python。

## 硬约束（不可违反，违反即失败）

1. **确定性计算不调用 LLM。** 解析/脱敏/时间戳归一/分组/复杂度规则 → 纯代码。
2. **型号不进业务代码。** 业务只认 L0–L3；具体型号只在配置（`.env`）映射。
3. **没有有效 Evidence 的结论不能标 fact。** evidence_ids 必须 ⊆ 本次有效 event_id，越界即拦截/降级。
4. **失败不静默。** 明确失败或降级，绝不产出「看似正常的假报告」。
5. **脱敏在落盘前。** 任何原始敏感原文不得进磁盘、不得进库。
6. **对象边界冻结**：9 张表、7 态状态机、L0–L3 分级，三处已自洽；**改表/改状态机前必须先问**。
7. **API 不承担长任务**，分析全部异步（Celery）；创建 Run 立即返回 `run_id + queued`。
8. **Domain 不写死**，一律走 Registry；核心 Runtime 不 import 具体 domain。
9. **每阶段节奏**：编码（含单测）→ 对照该阶段「验收」逐条过 → Git Commit（消息带阶段号）。
10. **不提前为未来功能写复杂代码**，只留最小接口缝（不做清单见计划 20 节）。

## 验收口径（2026-09-24 起）

**测试全绿 ≠ 达标。** 每条验收必须同时具备三样证据，缺一样只能标 ⚠️ 待实测：

1. **测试证据**（`.dsh/junit/all.xml` 逐用例计数）；
2. **真机接线**（`python .dsh/wiring_graph.py <符号>`：从真实入口点是否可达）；
3. **真机实测**（`python .dsh/real_path_evidence.py` 的落库/落盘事实、`trace_*.json` 的函数级追踪）。

出表用 `python .dsh/stage_acceptance.py` → `阶段验收表-真机口径.md`。
上一轮"14 个阶段全 ✅"只有一个口径（测试全绿），漏掉了三条从未在真机生效的成本闸门 ——
细节与修法见《真机核验报告-2026-09-24.md》。

## 分组/归属真源（防返工）

- `Event.group_id` 是分组**唯一真源**；`EventGroup` **不存** `event_ids` 反向数组。
- `Incident.group_ids` 是事故真源；`Incident` **不存**冗余 `event_ids`。
- `AgentRun.status` 管 7 态；`AgentRun.current_phase`/`phase_history` 管阶段进度，两者正交。
- 事故与结论的关联**按证据事件**判定（`incident_by_event`），不按标题字符串匹配。

## Incident 归并（V1 启发式，非因果）

同 `source_id` + 时间窗重叠或 ≤10min + 同一「异常签名」（同 analyzer 或同模板簇家族）+ severity 同档
→ 归并为同一 Incident。**这是近似分组，不是根因判定**；根因只能标 `inference`/`possibility` 并挂证据。

## 代码风格与提交

- 命名清晰优先于简短；类型注解齐全；纯函数优先、副作用外推。
- 单测覆盖每个阶段核心模块；测试用 `tests/` 下真实数据（`logs/`）与固定用例。
- Commit 遵循「阶段 N：<做了什么>」或「修复：<什么不生效>」；一个逻辑一个 commit，可随时回滚。
- **跨进程的状态判定一律查库**（`status_of` / `merge_run_metadata`），不要读会话里缓存的对象。

## 可用数据与配置

- 真实日志：`logs/`（5 份 TXT + 2 份真实 JSONL + 1 份平文负样本），用于阶段 04 调参。
- 模型/Key：见《模型与Key配置方案.md》；Key 只进 `.env`，被 `.gitignore` 挡住。
- 6 条历史文档不一致已修订，见《开工前6条文档不一致-修订说明.md》。
- 过程文档（本文件、模型与 Key 方案、交接评估、DSH 提示词清单）都在版本库里，clone 即可读。

## 禁止事项

- 不要 `git push` 到任何远程，除非我明确要求。
- 不要修改 `logs/` 下的样本内容。
- 不要删除或跳过测试来「通过」验收。
- 不要升级 Python 到 3.14，不要用 passlib，不要引入 Drain3（V1）。
- 不要把"配置项写了"当成"生效了"：闸门类的配置一律要有真机证据（见验收口径）。
