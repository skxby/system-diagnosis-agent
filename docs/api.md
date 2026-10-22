# API 使用约定

详细字段与在线调用入口为 `/docs`。业务接口使用 `Authorization: Bearer <JWT>`，网页使用 HttpOnly Cookie；浏览器跨站写入会被拒绝。

| 接口 | 行为 |
|---|---|
| `POST /api/register`、`POST /api/login` | 首次初始化注册；后续注册受开关控制。登录有 Redis 共享速率限制 |
| `POST /api/logout` | 清除浏览器登录 Cookie |
| `POST /api/projects/{id}/upload` | multipart：file、fmt、可选 data_source_id、mask_ip；返回 202、job_id/status；解析入库异步。已完成的重复上传在 POST 回应中 events_persisted=0、parse.duplicate=true |
| `GET /api/projects/{id}/uploads`、`.../uploads/{job_id}` | 最近上传任务与进度/最终结果；客户端不能写 uploads/upload_jobs 保留元数据 |
| `POST /api/projects/{id}/uploads/{job_id}/retry` | 仅允许失败任务重试，返回 202 和新任务 ID，复用已脱敏文件 |
| `POST /api/projects/{id}/analysis-runs` | 返回 202、run_id、status、reused；派发失败时 status=timeout，错误原因从 Run 查询 |
| `GET /api/projects/{id}/runs?limit=20&before_id=...` | 归属校验后的历史列表，按 ID 降序；limit 最大 100 |
| `GET /api/runs/{id}`、`GET /api/runs/{id}/detail` | 按资源反查所属项目；可选 project_id 只作交叉核对 |
| `GET /api/runs/{id}/insights` | 本 Run 结论 |
| `GET /api/insights/{id}/evidence` | 证据 ID、时间范围、说明及脱敏后的 events 内容 |
| `GET /api/projects/{id}/groups`、`.../incidents` | 分组与事故列表，支持 limit、before_id |
| `GET /api/projects/{id}/groups/{group_id}/events` | 由 Event.group_id 派生的成员事件，支持 limit、before_id |
| `GET /api/projects/{id}/knowledge/candidates`、`.../confirmed` | 当前项目候选与生效知识 |
| `PATCH /api/knowledge/candidates/{id}?project_id=...` | 编辑候选白名单字段；空标题拒绝 |
| `POST /api/knowledge/candidates/{id}/{confirm|reject|false-positive}?project_id=...` | 在指定且已鉴权的项目目录内审核 |

分析请求示例：

```json
{"data_source_id": 1, "time_range": {"start": "2026-10-01T00:00:00+08:00", "end": "2026-10-02T00:00:00+08:00"}, "filters": {"severity": ["high"], "event_type": ["log"], "keyword": "timeout"}}
```

时间必须带时区，边界包含起止时刻。keyword 为区分大小写的字面包含，`%`、`_` 不作为 SQL 通配符。省略时间窗时结束点取当前来源最新事件时间；SQL 同时限制 project/source/事件 ID 上界/时间/过滤条件。新上传即使时间早于最新事件也会使新请求缓存失效。

start_tier 可选，作为模型等级上限；省略时允许复杂度自动选到 L3。重试时保存原来的上限。

`/healthz/workers` 检查 analysis/uploads/maintenance/beat 的新鲜监督证据；未就绪返回 503。

409 包括数据源仍在解析或不允许重试的上传状态；401 表示未登录；402 是预算不足；403 是注册关闭或跨站写入；404 包括资源不属于当前用户；413 是上传超限；422 是参数不合法；429 是登录限速；503 包括 Redis 登录保护不可用。跨项目访问使用 404 避免透露资源存在性。
