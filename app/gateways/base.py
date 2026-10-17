"""Model Gateway 抽象（计划第 605–619 行）。

业务层只调用 `generate()`，**不知道供应商**；供应商差异在各自实现里消化。

三个必须守住的边界：
1. **代码中不出现具体型号**（计划第 616 行）——型号只从配置读入；
2. 各供应商对「结构化输出 / function calling」的支持方式不同，差异在本层消化，
   对上只暴露统一接口（计划第 619 行）；
3. 每次调用返回的**实际** token 用量要能被记录（计划第 617 行）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

#: 模型等级。L0 是「代码 / 规则，不调用模型」（计划第 599 行）。
TIER_L0 = "L0"
TIER_L1 = "L1"
TIER_L2 = "L2"
TIER_L3 = "L3"

TIERS: tuple[str, ...] = (TIER_L0, TIER_L1, TIER_L2, TIER_L3)
#: 真正会调用模型的等级
MODEL_TIERS: tuple[str, ...] = (TIER_L1, TIER_L2, TIER_L3)

#: 推理档位的合法取值。**只有这四个**——DeepSeek 官方 API 与 DSH 适配器
#: 都只接受 off / low / high / max。`medium` 不是合法值，历史文档里的
#: medium 映射已作废（见 .env 注释与《模型与Key配置方案》）。
VALID_REASONING_EFFORTS: tuple[str, ...] = ("off", "low", "high", "max")


class GatewayError(RuntimeError):
    """Gateway 层错误的基类。"""


class ModelUnavailableError(GatewayError):
    """模型不可用（网络 / 限流 / 服务端错误）。

    调用方（阶段 08 的重试与降级链）需要把它与「输入错误」区分开：
    前者可重试，后者重试无意义。
    """


class StructuredOutputError(GatewayError):
    """要求结构化输出但拿不到合法 JSON。

    **不静默降级成纯文本**：那会让下游拿到一个"看起来有结构"的字符串。
    """


class ModelRequestError(GatewayError):
    """上游拒绝请求（认证/参数），重试和切换等级均无意义。"""


class L0DoesNotCallModelError(GatewayError):
    """L0 是代码/规则等级，不该发起模型调用（计划第 599 行）。"""


class ConfigurationError(GatewayError):
    """配置缺失或不合法。启动时就该暴露，而不是等第一次调用。"""


@dataclass(frozen=True)
class TierConfig:
    """一个等级的配置（型号 + 档位 + 单价）。单价单位：元 / 每百万 token。"""

    tier: str
    model: str
    reasoning: str
    price_input_per_1m: float
    price_output_per_1m: float
    #: 缓存命中的输入单价（元 / 每百万 token）。0 表示"未配置"——
    #: 此时命中部分仍按正常输入价计，**不做任何静默折扣**：
    #: 悄悄按更便宜的价算会让账单看起来比实际低，方向正好是危险的。
    price_cache_hit_input_per_1m: float = 0.0
    #: 高峰时段的价格倍数。配置里的单价按**闲时价**填，高峰时段乘它。
    #: 默认 1.0 = 不做峰谷调整（不配置就不生效，不猜）。
    peak_price_multiplier: float = 1.0

    def estimate_cost(
        self,
        tokens_input: int,
        tokens_output: int,
        *,
        cached_tokens: int = 0,
        peak: bool = False,
    ) -> float:
        """按 token 用量估算成本（元）。

        缓存命中的输入 token 按 `price_cache_hit_input_per_1m` 计（未配置则按正常价）。
        供应商返回的输入 token 数**包含**命中部分（DeepSeek 另用
        `prompt_cache_hit_tokens` 给出命中数），故计费输入 = 输入 - 命中。

        为什么必须区分缓存：命中价通常比输入价低一个数量级（示例配置 0.02 vs 0.5）。
        一律按输入价算会**高估**成本，而高估的那部分会被 Mid-check 的
        「已花成本 ≥ run_max_cost」读到，让 Run 提前判定超预算而中断 ——
        等于拿一份偏高的账单去砍本来还能跑完的分析。

        为什么必须区分峰谷：配置里的单价是**闲时价**，高峰时段的真实价格要乘
        `peak_price_multiplier`。不乘的话账单被**低估**，方向更糟 ——
        预算是"钱够不够"的闸门，低估会让它在该拦的时候不拦。
        """
        cached = max(0, min(int(cached_tokens), int(tokens_input)))
        multiplier = self.peak_price_multiplier if peak else 1.0

        if self.price_cache_hit_input_per_1m > 0:
            billable_input = tokens_input - cached
            input_cost = (
                billable_input / 1_000_000 * self.price_input_per_1m
                + cached / 1_000_000 * self.price_cache_hit_input_per_1m
            )
        else:
            input_cost = tokens_input / 1_000_000 * self.price_input_per_1m

        return multiplier * (
            input_cost + tokens_output / 1_000_000 * self.price_output_per_1m
        )


@dataclass
class ModelResult:
    """一次模型调用的结果。

    字段与计划第 612 行的注释一致：`content, parsed, tokens_input, tokens_output,
    model, raw`。额外带 `tier` 与 `cost`，便于**即时记录**到 `AgentRun.model_calls`。
    """

    content: str
    parsed: dict[str, Any] | None
    tokens_input: int
    tokens_output: int
    model: str
    raw: dict[str, Any] = field(default_factory=dict)
    tier: str = ""
    #: 本次估算成本（元）
    cost: float = 0.0
    #: 供应商返回的思考 token 数（DeepSeek 会带）
    reasoning_tokens: int = 0
    #: 缓存命中的输入 token 数（成本差异可达 50 倍，阶段 07 要用）
    cached_tokens: int = 0

    def as_model_call(self, *, call_id: str, status: str = "ok") -> dict[str, Any]:
        """转成写进 `AgentRun.model_calls` 的一条记录。

        计划第 352 行约定的字段：`call_id, tier, model, timestamp, tokens_input,
        tokens_output, cost, status`。
        """
        from datetime import datetime, timezone

        return {
            "call_id": call_id,
            "tier": self.tier,
            "model": self.model,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "cost": self.cost,
            "status": status,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }


class ModelGateway(ABC):
    """供应商网关契约（计划第 608–612 行照抄）。"""

    #: 供应商标识，仅用于日志与记录，不参与业务判断
    provider: str

    @abstractmethod
    def generate(
        self,
        prompt: str,
        schema: dict[str, Any] | None,
        max_output_tokens: int,
        *,
        model: str,
        reasoning: str | None = None,
    ) -> ModelResult:
        """调用模型。

        `schema` 非空时要求结构化输出（JSON）；拿不到合法 JSON 必须抛
        `StructuredOutputError`，**不得退化成纯文本**。
        """
