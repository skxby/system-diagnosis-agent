# 运行架构

系统采用模块化单体：FastAPI、SQLAlchemy 2、PostgreSQL、Redis、Celery，页面使用 Jinja2 和原生 JavaScript。九张业务表、七个 Run 状态保持冻结；L0 是不调用模型的分级，可配置模型的等级是 L1–L3。

```text
浏览器 → API 鉴权 / 项目作用域 / 参数验证
  上传：有界内存 → 结构化脱敏 → 安全文件与任务快照 → 返回 202
        → uploads Celery Worker → 解析/事件与结果原子提交
  分析：输入快照 / 幂等键 → 提交 queued → Celery
Worker → 按快照筛选 SQL → 统计 / 分组 / 检测 / 事故归并
       → 复杂度 → L0 规则报告，或 Context 蒸馏 → 模型
       → 证据校验 → 分组 / 事故 / Insight / Evidence 落库
       → 规则报告 / 成本 / 阶段 / 分析范围写入 Run 元数据
浏览器 ← 项目历史 / Run 状态 / 报告 / 证据 / 知识审核 API
```

## 输入与结果真源

`AgentRun.input` 保存生效的时间窗、source、filters 和领域标识与版本。Worker 按该快照装载事件，不以任务执行时刻替代日志窗口。筛选条件先在 SQL 中生效，再计算统计、分组和有效证据集合。超过 50,000 条明确失败，不截断成正常报告。

未指定结束时间时，使用该来源最新事件时间；source_revision 保存来源最新事件 ID，Worker 只读取该 ID 上界内的事件，后上传的旧时间日志也会使下一次请求的缓存失效。重复请求保持稳定。创建同一幂等键使用 PostgreSQL 事务级 advisory lock；queued、running、completed 均可复用。算法版本为 1.1.0，旧版全量分析结果不会作为新版请求的缓存。

`Event.group_id` 是分组关系真源；`Incident.group_ids` 是事故成员真源。分组浏览接口从事件归属计算当前成员数量，避免将历史缓存计数当成当前关系。

默认允许复杂度自动选择 L1–L3；失败降级的等级上限会实际传入模型调用，避免重新按原复杂度调用高等级，已尝试的低等级不重复走外层降级。

规则报告和复杂度存于 `run_metadata`，不伪造 Insight。模型网关延迟到真正调用模型时初始化，L0 不依赖模型凭据。上行和落库前分别校验证据；落库拒绝会让 Run 明确失败。

## 异步上传与监督

上传任务存入 DataSource.meta.upload_jobs，队列只传项目/来源/任务 ID；失败可新建重试，原始文本不进 Redis。来源存在活动上传时，创建分析返回 409。Worker 先认领后提交进度，事件和最终结果在同一事务提交。

analysis、uploads、maintenance 各由独立父进程监督 solo 子进程；父进程使用真实队列探针和任务边界租约，超时后停止整个进程树、记录失败并重启。进程身份用 PID 和创建时间核对，防止 PID 复用。beat 为独立调度进程。实现与证据见 [异步运行时](async-runtime-2026-10-22.md)。

## 知识数据

seed 知识由 Domain Registry 返回的领域目录提供，是共享只读代码资产。运行时数据在：

```text
${DATA_DIR}/projects/<project_id>/knowledge/<domain>/
  staging/candidates.yaml
  confirmed/runtime_confirmed.yaml
  rejected/ 或 false_positives/
```

Worker 写候选与 API 审核使用同一 staging 文件锁，锁覆盖整个读、修改、写入过程；内容通过同目录临时文件和 `os.replace` 原子替换。confirmed 先写、staging 后移除；中断后允许幂等重试，两个文件不宣称具有数据库事务。

旧的全局运行时目录不自动加载或归入任何项目。归属不明的数据保留原文件，确认项目后再迁移，避免把跨项目污染带回系统。

## 生命周期与可观测性

状态转移通过既有状态机判定。派发失败沿用已允许的 queued→timeout，记录 dispatch_failed；新 Run 在元数据记录 queued_at，维护任务可回收排队超时。跨进程终态判定在数据库行锁内重新读取。项目花费通过原子 SQL 累加。

链路阶段会落库，并更新心跳、检查取消。阶段记录用于展示与诊断，重试以新 Run 重跑，不宣称阶段级断点恢复。

架构边界和剩余工作见 [已知限制](limitations.md)；审计决策与验证证据见 [审计处置记录](audit-2026-10-17.md)。
