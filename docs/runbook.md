# 本机运维手册

## 启动与配置

Windows 双击 `启动.cmd`：缺失时复制 `.env.example`，缺省会话密钥由 `scripts/init-env.ps1` 随机生成。不会打印密钥。手工部署必须设置至少 32 字符随机 SECRET_KEY；空值与公开占位符会在 Web 启动时被拒绝。`.env` Key 默认留空，实际凭据查找顺序见 README。

```powershell
docker compose ps -a
docker compose logs --tail=80 migrate web worker beat
```

`migrate` 的 Exited (0) 正常。`/healthz` 只确认 Web 进程；`/healthz/deps` 检查 PG/Redis，只返回可用性，不公开连接错误和版本。worker/beat 的 Up 仅说明进程仍在。

若 Docker Desktop 显示运行但当前 context 连接失败，先用 `docker context ls`、`docker --context default info` 对照端点。仅对本次命令使用已验证的 context，不必修改全局设置。

## 分析不前进

1. 从项目分析记录进入 Run，检查状态、当前阶段、心跳和原因。
2. dispatch_failed 表示没有成功确认入队；恢复 Redis/Worker 后通过“重新分析”创建新 Run。
3. 新 Run 排队/心跳超过 300 秒后由维护任务回收，扫描间隔默认 60 秒。没有 queued_at 的旧记录不能据此判断。
4. solo Worker 卡住时，先保留 Run ID 和 trace_id，再 `docker compose restart worker`。维护 Worker 在 beat 容器中消费独立队列。
5. 回收只修改数据库状态，不保证杀掉实际卡住的代码。不要仅靠 timeout 状态判断进程已经释放。

## 模型、预算与上传

- 规则报告正常出现于 L0，成本为 0。模型凭据/参数错误不重试；网络/限流可以重试并降级。查看 Run 的 attempts、notes、stop_reason。
- 402：检查项目预算剩余、MONTHLY_BUDGET、DEFAULT_RUN_MAX_COST 和价格配置；预算单位为元，项目上限不自动按月重置。
- 修改容器环境后执行 `docker compose up -d --force-recreate web worker beat`。配置只在容器创建时注入。
- 坏行样本可在上传结果查看；GB18030、UTF-8/BOM、UTF-16 BOM 会严格解码。无法解码时拒绝，不替换成乱码。
- 同数据源内重复文件不再重复插入事件；有意重新分析请调整筛选范围，失败/不完整结果用重试入口。

## 数据与恢复

保留 PostgreSQL 卷和 appdata 卷的配套备份。知识运行时文件位于 appdata 的 `projects/<id>/knowledge/<domain>/`，旧全局目录需要人工核实归属。停止使用 `docker compose down` 保留卷；`down -v` 会删除数据库、上传和运行时知识，不用于普通排障。

磁盘检查：`docker system df`、`docker compose logs --tail=100 worker`。保留排障证据后处理占用；不要把删除卷、清空表或 downgrade base 当成常规修复。

测试运行见 [CONTRIBUTING](../CONTRIBUTING.md)。`.dsh/audit_preview.py` 提供独立预览库、真实 Worker、禁止外部模型的网页验收环境。
