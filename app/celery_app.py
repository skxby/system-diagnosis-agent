"""Celery 实例。

约定（与阶段 08 的可靠性机制对应）：
- broker / backend 都用 Redis，V1 不额外引入依赖；
- 内部时间一律 UTC；
- task_acks_late + reject_on_worker_lost：worker 被杀时任务可被重新投递，
  这是「僵尸 Run 回收」能成立的前提之一。
- **Worker 也要配 structlog**：见文件末尾的说明。
- **beat_schedule 里挂着僵尸 Run 的周期回收**：没有它，`reclaim_zombies`
  就只是躺在 repository 里的一个函数（真机核验发现的事实）。

关于"杀 Worker 之后会发生什么"，三条机制一起才完整：
  ① `task_acks_late`：任务会被重投递，Worker 起来后能接着跑；
  ② Worker 现在会把「running + 心跳」**立刻提交**（`RunExecutor` 的 checkpoint），
     所以被杀时库里留下的是 `running` + 未过期的心跳 —— 这正是
     `find_zombie_runs` 认的条件；
  ③ 超过心跳阈值后，beat 上的回收任务把它收成 `timeout`。
     新 queued Run 的排队基准记录在 run_metadata.queued_at，由同一维护任务回收；
     旧记录缺少该基准时不补造时间。
"""

from celery import Celery
from celery.signals import setup_logging

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "logagent",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.health", "app.tasks.analysis", "app.tasks.maintenance"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # 一次只预取一个任务：分析任务重且耗时长，预取会让队列分配严重不均
    worker_prefetch_multiplier=1,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    task_track_started=True,
    # 僵尸 Run 回收（阶段 08 验收「手动 kill Worker 后僵尸 Run 被回收」）。
    #
    # 这条 schedule 此前**根本不存在** —— beat 容器一直在空转，
    # `reclaim_zombies` 只有测试在调。于是 Worker 一旦被杀，Run 就永远停在那儿：
    # 既没人回收（没有周期任务），也没人能回收（见文件头的三条机制说明）。
    beat_schedule={
        "reap-zombie-runs": {
            "task": "app.tasks.maintenance.reap_zombie_runs",
            "schedule": float(settings.zombie_reap_interval_seconds),
        },
    },
    # 维护任务走**专用队列**。
    #
    # 第一版把它发到默认队列（和分折任务同一个），结果真机复验当场打脸：
    # 分析 Worker 一被冻结，回收任务就跟着排在队列里没人消费 ——
    # 而"分析 Worker 死了"恰恰是它唯一要处理的场景。**救火队不能住在消防站里。**
    # 现在 `maintenance` 队列由 beat 容器内嵌的 worker 消费（`-Q maintenance -B`），
    # 与 web/worker 完全独立。
    task_routes={
        "app.tasks.maintenance.*": {"queue": "maintenance"},
    },
)


@setup_logging.connect
def _configure_worker_logging(**_: object) -> None:
    """在 worker 进程里也启用 structlog。

    真机现象：`configure_logging()` 只在 `app/main.py`（Web 进程）里被调用过，
    worker 进程从来没配过。于是计划第 937 行要求的「JSON 结构化日志」在
    worker 侧不成立，日志里也没有 trace_id —— 第 928 行那句
    「HTTP 请求 → 入队 → Worker 执行 能被同一个 trace_id 串起来」实际断成两截：
    拿着 Run 详情里的 trace_id 去 grep，只能捞到 Web 那半边。

    这个 bug 的隐蔽之处在于：**worker 看起来"有日志"**（Celery 自己的
    INFO/MainProcess 行一直在打），所以没人会怀疑日志没配对。

    用 `setup_logging` 信号而不是模块级直接调用：Celery 在启动时会自己
    `hijack_root_logger`，模块级调用配的东西会被它覆盖掉；接在信号上才是
    "配在 Celery 配完之后"，顺序不会错。
    """
    from app.utils.observability import configure_logging

    configure_logging(level=settings.log_level, force=True)
