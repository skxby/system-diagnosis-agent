# 异步上传与进程监督

## 调研与取舍

- [Celery 官方 Worker 文档](https://github.com/celery/celery/blob/main/docs/userguide/workers.rst)：solo 执行任务时不能处理 remote-control 请求，不能以 inspect ping 或 soft time limit 作为卡死任务的终止保证。
- [testdrivenio/fastapi-celery](https://github.com/testdrivenio/fastapi-celery)：参考 202 + task ID + 状态轮询模式；现有 Celery/PG 足够，不增加队列框架。示例只参考协议结构，不搬入其部署和授权代码。
- [psutil](https://github.com/giampaolo/psutil)：BSD-3-Clause，维护活跃，Windows/Linux 均有进程树管理；锁定已安装 7.2.2，不执行 shell 或按进程名杀其他应用。相比引入 Supervisor 平台，单父进程包装能沿用 Windows 的 solo 与既有 Docker Compose。

## 上传协议

API 必须完成接收、大小校验和内存脱敏，才能将数据持久化；这条隐私边界不交给 Redis。解析、时间归一和事件入库全部在独立 uploads 队列执行。队列消息只有项目、来源和上传任务 ID，没有日志文本、原始文件名或文件内容。

`POST /api/projects/{id}/upload` 返回 202 和 job_id/status。任务存于 `DataSource.metadata.upload_jobs`，不新增表，也不修改 AgentRun 七态。`GET .../uploads/{job_id}` 返回进度/结果，`GET .../uploads` 支持刷新后找回最近任务。失败任务通过 `POST .../uploads/{job_id}/retry` 复用安全文件并新建任务。API 先提交，再入队；派发失败明确标 failed。

Worker 在短事务中认领 queued 任务并提交 running，再从只读安全文件解析。事件与 completed 结果在同一事务提交，崩溃不会留下半批事件。重复投递不重复入库；来源内相同内容和脱敏选项复用任务。任务文件路径来自服务端保留元数据，客户端不能注入 uploads/upload_jobs；Worker 再校验项目目录边界。

来源还有待完成上传时，分析创建接口返回 409，避免提前创建空输入快照。

## Worker 监督

analysis、uploads、maintenance 各有独立监督父进程和 solo 子进程。Celery 的 ready/prerun/postrun 信号写入任务租约，父进程通过仅该实例订阅的独立探针队列发送 health.ping，验证目标消费者，而不会让同业务队列的其他 Worker 代答。任务执行时按独立 monotonic 时钟的墙钟期限监督，不因不断写健康心跳而无限延长。

超时或异常退出后先终止整个子进程树，释放事务和锁，再按既有状态转移将分析 Run 标为 timeout 或将上传任务标为 failed，随后启动新子进程。无法杀死旧子进程时明确失败，不并行启动第二个消费者。健康文件记录重启次数和原因，Docker healthcheck 只读取新鲜的监督证据。

beat 是独立调度进程，最多每五秒更新调度循环证据；维护任务由独立 maintenance Worker 消费。维护扫描也回收长期 queued/running 的上传任务，覆盖 Web 提交后入队前退出、监督器自身退出等情况。

## 配置

`UPLOAD_QUEUE=uploads`、`UPLOAD_QUEUE_TIMEOUT_SECONDS=1800`；`WORKER_TASK_TIMEOUT_SECONDS=600`；`WORKER_STARTUP_TIMEOUT_SECONDS=60`；`WORKER_PROBE_INTERVAL_SECONDS=30`、`WORKER_PROBE_TIMEOUT_SECONDS=20`。发布连接/结果读取均有有限网络等待。分析和上传任务的期限应大于允许的正常最慢调用。

## 验证入口

单元测试、真实 PG/Redis 上传测试、监督器进程级故障注入与页面流程分别验证；自动用例不能替代真实子进程被终止的证据。原有九张表、七态、L0–L3、只读领域和脱敏前置保持不变。


## 本轮验收事实

| 验收项 | 测试证据 | 入口接线 | 真机实测 |
|---|---|---|---|
| 隐私接入后异步解析 | test_post_only_masks_and_queues_before_real_worker_runs | upload_log → prepare；execute_upload → process_upload | 原始 POST 为 202，未派发前事件为 0，安全文件无 canary 原文；真实 Celery 完成后事件为 1 |
| 幂等、隔离、失败重试 | test_async_upload_pg + 既有上传用例经真实 202 轮询 | 项目作用域 → UploadJobRepository；retry_upload → _enqueue_upload | 重投不重复写入；同一用户另一项目查询/重试返回 404；派发失败和新任务重试均可见 |
| 不被导入锁阻塞排队 | test_enqueue_does_not_wait_for_event_foreign_key_lock | 来源元数据采用 NO KEY UPDATE | Event 写入持有 KEY SHARE 时，同来源新任务仍能返回 202 |
| 冻结任务的进程监督 | test_external_supervisor_kills_frozen_worker_rolls_back_and_consumes_again 两个参数场景 | worker_supervisor main → supervise；真实 Celery prerun 租约 | Windows 上 SIGSTOP 等价的整进程 suspend 后，旧 PID 已死亡；上传未提交事件回滚且标 failed；分析标 timeout；新 Worker 再次消费 ping |
| Linux 容器监督 | .dsh/check_async_container.py | 同一生产镜像、三个监督 Worker + 独立 beat | 四角色健康；异步导入真实日志 60 条；L0 完成且费用 0；冻结 analysis 消费者后 probe_timeout、重启 1 次、旧 PID 死亡、健康恢复 |
| 页面流程 | Tabbit 浏览器 | 页面 POST 202 → 任务 GET → 分析 API | 接收 202 后刷新页面，恢复 60 条解析结果；继续分析并看到 L0 报告；无 JavaScript 异常 |

自动化原始证据保存在 `.dsh/junit/all.xml`（本轮全量）及 `.dsh/junit/async-runtime-2026-10-22.xml`、`async-runtime-process-evidence.json` 与 `container-runtime-evidence.json`。这些是本轮生成的证据，不复用历史 trace 来宣称达标。所有新增主入口的静态接线均可达；settings_usage_audit 没有发现新增死配置。

正式故障夹具在 `tests/fixtures/worker_faults.py`，只有测试通过 --include 显式加载；不会随运行镜像打包，也没有对外故障注入 API。镜像按运行源码允许清单 COPY，无关工作区资料不会进入镜像。

机器摘要见 [本轮验收证据](evidence/async-runtime-2026-10-22.json)。快层 580 个用例通过，app 行覆盖率 63.13%，超过 60% CI 基线；进程监督与异步入库由真实依赖/进程用例补充验证。

825 个自动用例通过，3 个原有付费模型用例未启用；本轮不新增跳过、不删除原回归。相关异步/监督验收已提供上述三类证据，不宣称十四阶段的全部历史验收已重新执行。

## 复验命令

```powershell
pytest -q --junitxml=.dsh/junit/all.xml
python .dsh/wiring_graph.py app.services.upload_service.UploadService.prepare app.tasks.upload.process_upload app.worker_supervisor.supervise app.tasks.maintenance.reclaim_uploads
docker compose build web
python .dsh/check_async_container.py
```

容器验证会检查空闲 Redis 逻辑库的所有权，只使用临时 PostgreSQL 数据库和临时容器；结束后删除自己创建的资源。不会清空开发数据卷或覆盖现有 origin。
