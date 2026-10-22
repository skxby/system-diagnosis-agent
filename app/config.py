"""集中配置：所有可变项来自环境变量 / .env，代码里不出现密钥与具体型号。

业务代码只认模型等级 L1 / L2 / L3；具体型号与端点只在 .env 里映射。
"""

from functools import lru_cache
from pathlib import Path
import math

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import field_validator

# .env 相对项目根定位，避免「换个 cwd 就读不到配置」这种坑
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    environment: str = "dev"

    # --- 数据库 / 队列 ---
    database_url: str = "postgresql+psycopg://logagent:logagent_dev_pw@127.0.0.1:5432/logagent"
    redis_url: str = "redis://127.0.0.1:6379/0"

    # --- 会话 ---
    secret_key: str = "change-me"
    allow_registration: bool = False
    cookie_secure: bool = False
    default_domain_id: str = "computer_monitoring"

    # --- 模型等级 → 型号映射 ---
    model_l1: str = ""
    model_l2: str = ""
    model_l3: str = ""
    model_provider_base_url: str = ""
    model_provider_api_key: str = ""

    # --- 推理档位（阶段 06 Router 用）---
    # 合法值只有 off / low / high / max：DeepSeek 官方 API 与 DSH 适配器
    # 都只接受这四个（off = 关闭思考；其余为开启思考并指定强度）。
    # 注意 medium 不是合法值，历史文档里的 medium 映射已作废。
    model_l1_reasoning: str = ""
    model_l2_reasoning: str = ""
    model_l3_reasoning: str = ""

    # --- 成本单价（元 / 每 1M token）---
    model_l1_price_input_per_1m: float = 0.0
    model_l1_price_output_per_1m: float = 0.0
    model_l2_price_input_per_1m: float = 0.0
    model_l2_price_output_per_1m: float = 0.0
    model_l3_price_input_per_1m: float = 0.0
    model_l3_price_output_per_1m: float = 0.0
    # 缓存命中价与未命中价相差约 50 倍，阶段 07 成本控制必须分开计费
    model_cache_hit_input_price_per_1m: float = 0.0
    # 高峰时段单价倍数（工作日 9:00-12:00、14:00-18:00 北京时间）
    model_peak_price_multiplier: float = 2.0

    # --- 运行时数据目录（知识库 confirmed/staging，见修订说明第 2 条）---
    data_dir: str = "./data"

    @field_validator("data_dir")
    @classmethod
    def resolve_data_dir(cls, value: str) -> str:
        path = Path(value)
        return str(path if path.is_absolute() else PROJECT_ROOT / path)

    # --- 预算 ---
    monthly_budget: float = 10.0
    default_run_max_cost: float = 0.30

    # --- 可靠性 ---
    # 僵尸 Run 的周期扫描间隔（秒）。心跳超时是 300s（见
    # app/analysis/heartbeat.py 的 DEFAULT_HEARTBEAT_TIMEOUT_SECONDS），
    # 扫描比它勤得多才有意义：否则"回收"最坏要等两倍超时。
    zombie_reap_interval_seconds: int = 60
    upload_queue: str = "uploads"
    upload_queue_timeout_seconds: int = 1800
    worker_task_timeout_seconds: float = 600
    worker_startup_timeout_seconds: float = 60
    worker_probe_interval_seconds: float = 30
    worker_probe_timeout_seconds: float = 20

    @field_validator("worker_task_timeout_seconds", "worker_startup_timeout_seconds",
                     "worker_probe_interval_seconds", "worker_probe_timeout_seconds", "upload_queue_timeout_seconds")
    @classmethod
    def positive_timeout(cls, value: float | int) -> float | int:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("任务与监督超时必须为正数")
        return value

    # --- 时间 / 日志 ---
    default_timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"

    def tier_model(self, tier: str) -> str:
        """把等级（L1/L2/L3）翻译成具体型号。业务代码只该调这个。"""
        mapping = {"L1": self.model_l1, "L2": self.model_l2, "L3": self.model_l3}
        try:
            return mapping[tier.upper()]
        except KeyError:
            raise ValueError(f"未知模型等级: {tier!r}，只接受 L1 / L2 / L3") from None

    def tier_price(self, tier: str) -> tuple[float, float]:
        """返回 (输入单价, 输出单价)，单位：元 / 每 1M token。"""
        key = tier.upper()
        if key not in {"L1", "L2", "L3"}:
            raise ValueError(f"未知模型等级: {tier!r}，只接受 L1 / L2 / L3")
        prefix = f"model_{key.lower()}_price"
        return (
            getattr(self, f"{prefix}_input_per_1m"),
            getattr(self, f"{prefix}_output_per_1m"),
        )

    def tier_reasoning(self, tier: str) -> str:
        """把等级翻译成推理档位（off / low / high / max）。

        阶段 06 的 Router 用它决定是否开启思考及强度；
        业务代码同样只认等级，不认档位字面量。
        """
        mapping = {
            "L1": self.model_l1_reasoning,
            "L2": self.model_l2_reasoning,
            "L3": self.model_l3_reasoning,
        }
        try:
            return mapping[tier.upper()]
        except KeyError:
            raise ValueError(f"未知模型等级: {tier!r}，只接受 L1 / L2 / L3") from None


@lru_cache
def get_settings() -> Settings:
    return Settings()
