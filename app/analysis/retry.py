"""重试与模型降级链（计划第 712–726 行）。

```text
可重试：网络错误、限流、服务暂不可用（指数退避 + 抖动，最多 3 次）
不可重试：输入格式错误、认证失败、预算耗尽、校验失败

降级链（重试耗尽后）：
  当前等级模型连续失败 → 降一级（L3→L2→L1）
  → 仍失败 → 输出纯规则 L0 报告（analyzer 已有的确定性结果），
    状态 partial_success，stop_reason="model_unavailable"
```

**降级不是静默的**：L0 是「纯规则报告」，必须让调用方知道模型没参与，
否则报告看起来和正常分析一样（红线 4）。
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.analysis.idempotency import ErrorKind, classify_error, is_retryable
from app.gateways.base import MODEL_TIERS
from app.policy import STOP_MODEL_UNAVAILABLE
from app.policy.cost_controller import StopExecution
from app.utils.observability import get_logger

T = TypeVar("T")

logger = get_logger(__name__)

#: 计划第 715 行：最多 3 次
MAX_RETRIES = 3
#: 指数退避基数（秒）
BASE_DELAY_SECONDS = 0.5
#: 退避上限，避免退避到天荒地老
MAX_DELAY_SECONDS = 30.0
DEFAULT_JITTER_RATIO = 0.25


@dataclass
class RetryPolicy:
    """重试策略。"""

    max_retries: int = MAX_RETRIES
    base_delay_seconds: float = BASE_DELAY_SECONDS
    max_delay_seconds: float = MAX_DELAY_SECONDS
    jitter_ratio: float = DEFAULT_JITTER_RATIO

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError(f"max_retries 不能为负，收到 {self.max_retries}")
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds 必须为正")

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """第 `attempt` 次重试前的等待秒数（attempt 从 1 开始）。

        指数退避 + **抖动**：没有抖动的话，多个同时失败的调用会在同一刻
        一起重试，把限流的服务再打一次（thundering herd）。
        """
        if attempt < 1:
            raise ValueError(f"attempt 从 1 开始，收到 {attempt}")
        exponential = self.base_delay_seconds * (2 ** (attempt - 1))
        capped = min(exponential, self.max_delay_seconds)
        source = rng or random
        jitter = capped * self.jitter_ratio * (source.random() * 2 - 1)
        return max(0.0, capped + jitter)


@dataclass
class AttemptRecord:
    """一次尝试的记录，用于事后说明"为什么重试了/为什么放弃"。"""

    attempt: int
    error_kind: str
    error: str
    delay_before: float = 0.0


@dataclass
class RetryOutcome:
    """重试的最终结果。"""

    value: Any = None
    ok: bool = False
    attempts: int = 0
    history: list[AttemptRecord] = field(default_factory=list)
    last_error: BaseException | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "attempts": self.attempts,
            "history": [
                {
                    "attempt": r.attempt,
                    "error_kind": r.error_kind,
                    "error": r.error,
                    "delay_before": round(r.delay_before, 3),
                }
                for r in self.history
            ],
            "last_error": (
                f"{type(self.last_error).__name__}: {self.last_error}"
                if self.last_error
                else None
            ),
        }


def call_with_retry(
    func: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
) -> RetryOutcome:
    """按策略调用 `func`，可重试错误才重试。

    不可重试的错误**立刻返回失败**，不浪费时间与配额（计划第 716 行）。
    `sleep` 可注入，便于测试不必真的等。
    """
    effective = policy or RetryPolicy()
    do_sleep = sleep or (lambda seconds: None)
    history: list[AttemptRecord] = []
    last_exc: BaseException | None = None

    for attempt in range(1, effective.max_retries + 2):  # 首次 + max_retries 次重试
        try:
            value = func()
        except BaseException as exc:  # noqa: BLE001 - 需要按类型决定是否重试
            last_exc = exc
            kind = classify_error(exc)
            # **留痕**：这里若不记日志，被吞掉的异常在整条链路里不留任何痕迹，
            # 最终表现为"分析完成但没有结论"，排查时完全无从下手
            # （2026-09-23 真机故障就是这么被掩盖的）。
            logger.warning(
                "attempt_failed", attempt=attempt, error_kind=kind,
                error=f"{type(exc).__name__}: {exc}",
            )
            if not is_retryable(exc):
                history.append(
                    AttemptRecord(attempt=attempt, error_kind=kind, error=f"{type(exc).__name__}: {exc}")
                )
                return RetryOutcome(
                    ok=False, attempts=attempt, history=history, last_error=exc
                )

            if attempt > effective.max_retries:
                history.append(
                    AttemptRecord(
                        attempt=attempt,
                        error_kind=kind,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                break

            delay = effective.delay_for(attempt, rng=rng)
            history.append(
                AttemptRecord(
                    attempt=attempt,
                    error_kind=kind,
                    error=f"{type(exc).__name__}: {exc}",
                    delay_before=delay,
                )
            )
            do_sleep(delay)
            continue
        else:
            history.append(AttemptRecord(attempt=attempt, error_kind="ok", error=""))
            return RetryOutcome(value=value, ok=True, attempts=attempt, history=history)

    return RetryOutcome(ok=False, attempts=effective.max_retries + 1, history=history, last_error=last_exc)


# ============================================================
# 降级链
# ============================================================


def downgrade_ladder(start_tier: str) -> list[str]:
    """从 `start_tier` 开始的降级顺序（计划第 723 行：L3→L2→L1）。

    L1 再降就是 L0——但 L0 不是"再试一次模型"，而是纯规则报告，
    故不放进这个列表，由调用方单独处理（语义不同，混在一起会让人以为
    L0 也是一次模型调用）。
    """
    normalized = start_tier.upper()
    if normalized not in MODEL_TIERS:
        raise ValueError(f"{start_tier!r} 不是可调用的模型等级，只接受 {MODEL_TIERS}")
    order = list(MODEL_TIERS)  # ("L1","L2","L3")
    return order[: order.index(normalized) + 1][::-1]


@dataclass
class FallbackAttempt:
    tier: str
    ok: bool
    error_kind: str | None = None
    error: str | None = None
    retry: dict[str, Any] | None = None


@dataclass
class FallbackOutcome:
    """降级链的最终结果。

    `used_rules_only=True` 表示**所有模型等级都失败，最终产出的是纯规则 L0 报告**
    ——调用方必须据此把 Run 置为 `partial_success` 并在页面上标注
    （计划第 725 行）。
    """

    value: Any = None
    ok: bool = False
    tier_used: str | None = None
    used_rules_only: bool = False
    stop_reason: str | None = None
    attempts: list[FallbackAttempt] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tier_used": self.tier_used,
            "used_rules_only": self.used_rules_only,
            "stop_reason": self.stop_reason,
            "attempts": [
                {
                    "tier": a.tier,
                    "ok": a.ok,
                    "error_kind": a.error_kind,
                    "error": a.error,
                    "retry": a.retry,
                }
                for a in self.attempts
            ],
        }


def call_with_fallback(
    attempt_tier: Callable[[str], T],
    *,
    start_tier: str,
    rules_only_fallback: Callable[[], T] | None = None,
    retry_policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] | None = None,
    rng: random.Random | None = None,
) -> FallbackOutcome:
    """按降级链依次尝试；全失败则产出纯规则报告。

    `attempt_tier(tier)` 内部负责真正的模型调用。不可重试的错误会**中止整条链**
    （例如预算耗尽、认证失败——降级重试没有意义，只是多花钱）。
    """
    attempts: list[FallbackAttempt] = []
    ceiling = 4

    for tier in downgrade_ladder(start_tier):
        if int(tier[-1]) >= ceiling:
            continue
        outcome = call_with_retry(
            lambda t=tier: attempt_tier(t),
            policy=retry_policy,
            sleep=sleep,
            rng=rng,
        )
        if outcome.ok:
            attempts.append(
                FallbackAttempt(tier=tier, ok=True, retry=outcome.as_dict())
            )
            return FallbackOutcome(
                value=outcome.value,
                ok=True,
                tier_used=tier,
                used_rules_only=False,
                attempts=attempts,
            )

        kind = classify_error(outcome.last_error) if outcome.last_error else ErrorKind.UNKNOWN
        actual_tier = getattr(outcome.last_error, "tier_used", tier)
        if actual_tier in MODEL_TIERS:
            ceiling = min(ceiling, int(actual_tier[-1]))
        # `StopExecution` 自带计划第 660 行的 stop_reason 词汇
        # （budget_exceeded / token_limit / runtime_limit / call_limit）。
        # 若一律用错误分类（"budget"）当 stop_reason，就和计划里的取值对不上：
        # 判"该不该收成 partial_success"的那一处按计划词汇比对，
        # 结果永远匹配不上 —— Run 会被报成 failed，而它其实是"到点收工、已有结论"。
        stop_reason = kind
        if isinstance(outcome.last_error, StopExecution):
            stop_reason = outcome.last_error.reason

        attempts.append(
            FallbackAttempt(
                tier=tier,
                ok=False,
                error_kind=kind,
                error=str(outcome.last_error) if outcome.last_error else "unknown",
                retry=outcome.as_dict(),
            )
        )

        # 不可重试的错误：继续降级只是多花钱、多等待，直接停
        if kind not in (ErrorKind.RETRYABLE, ErrorKind.UNKNOWN):
            return FallbackOutcome(
                ok=False,
                # 如实回报**真正的原因**（如 input / budget / auth），
                # 不要一律写成 model_unavailable —— 那会把"日志格式不认识"
                # 报成"模型不可用"，让人往完全错误的方向排查。
                stop_reason=stop_reason,
                attempts=attempts,
            )

    # 全部等级失败 → 纯规则 L0 报告
    if rules_only_fallback is None:
        return FallbackOutcome(
            ok=False,
            used_rules_only=True,
            stop_reason=STOP_MODEL_UNAVAILABLE,
            attempts=attempts,
        )

    rules_value = rules_only_fallback()
    return FallbackOutcome(
        value=rules_value,
        ok=True,
        tier_used="L0",
        used_rules_only=True,
        stop_reason=STOP_MODEL_UNAVAILABLE,
        attempts=attempts,
    )
