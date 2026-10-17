"""幂等键与可重试性分类（计划第 695–717 行）。

幂等键（计划第 700–701 行）：

```text
sha256(project_id + source_id + time_range + filters_hash
       + domain_id + DOMAIN_VERSION + PIPELINE_VERSION)
```

相同键且已有成功 Run → 直接复用结果，**不重复执行、不重复计费**（第 703 行）。

要点：**组成项必须稳定**。任何会随机变的东西（时间戳、UUID、字典迭代顺序）
都不能进键，否则同一份输入每次算出不同键，幂等形同虚设。故 filters 先做
**规范化序列化**（排序键、稳定分隔符）再哈希。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.gateways.base import (
    ModelRequestError, ConfigurationError,
    ModelUnavailableError,
    StructuredOutputError,
)
from app.policy import StopExecution

#: 流水线版本。改动了分析步骤的顺序/语义就要升版本——它会进幂等键，
#: 不升版本会让"同一键"在新旧流水线下指向不同语义的结果。
PIPELINE_VERSION = "1.1.0"


@dataclass(frozen=True)
class IdempotencyInputs:
    """幂等键的输入。字段与计划第 700 行一一对应。

    时间字段接受 `datetime` 或 ISO 字符串：**不要在这里就把 datetime 转成
    str**，否则 naive datetime 的报错会从「是 naive」变成「字符串不含时区」，
    丢失了真正的原因。
    """

    project_id: int
    source_id: int
    time_range_start: datetime | str
    time_range_end: datetime | str
    filters: dict[str, Any]
    domain_id: str
    domain_version: str
    pipeline_version: str = PIPELINE_VERSION


def canonical_json(value: Any) -> str:
    """稳定序列化：键排序、无多余空白、非 ASCII 不转义。

    **不能用普通 json.dumps**：字典顺序不同会得到不同字符串，进而得到不同
    哈希——同一份输入两次提交就会产生两个 Run，幂等失效。
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def normalize_time_range(start: datetime | str, end: datetime | str) -> tuple[str, str]:
    """把时间范围规范成 ISO 字符串（UTC）。

    naive 时间被拒绝：含糊的时间会让"同一时间范围"算出不同键。
    """

    def one(value: datetime | str, *, label: str) -> str:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise ValueError(f"{label} 是 naive datetime，无法用于幂等键")
            return value.astimezone(timezone.utc).isoformat()
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{label} 不是合法 ISO 时间: {text!r}") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{label} 不含时区，无法用于幂等键: {text!r}")
        return parsed.astimezone(timezone.utc).isoformat()

    return one(start, label="time_range_start"), one(end, label="time_range_end")


def compute_idempotency_key(inputs: IdempotencyInputs) -> str:
    """按计划第 700–701 行算幂等键。"""
    # normalize_time_range 会给出准确的错误（naive datetime / 缺时区 / 非法格式）
    start, end = normalize_time_range(inputs.time_range_start, inputs.time_range_end)
    payload = {
        "project_id": inputs.project_id,
        "source_id": inputs.source_id,
        "time_range": [start, end],
        "filters": inputs.filters,
        "domain_id": inputs.domain_id,
        "domain_version": inputs.domain_version,
        "pipeline_version": inputs.pipeline_version,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def make_idempotency_key(
    *,
    project_id: int,
    source_id: int,
    time_start: datetime | str,
    time_end: datetime | str,
    filters: dict[str, Any] | None = None,
    domain_id: str,
    domain_version: str,
    pipeline_version: str = PIPELINE_VERSION,
) -> str:
    """便捷入口。

    直接透传 `datetime | str`，不做 `str()` 预处理 —— 见 `IdempotencyInputs`
    的说明：提前转字符串会让 naive datetime 的报错失去准确性。
    """
    return compute_idempotency_key(
        IdempotencyInputs(
            project_id=project_id,
            source_id=source_id,
            time_range_start=time_start,
            time_range_end=time_end,
            filters=filters or {},
            domain_id=domain_id,
            domain_version=domain_version,
            pipeline_version=pipeline_version,
        )
    )


# ============================================================
# 可重试性分类（计划第 712–717 行）
# ============================================================


class ErrorKind:
    """错误分类。分类决定"重试"还是"直接失败"。"""

    RETRYABLE = "retryable"
    INPUT = "input"
    AUTH = "auth"
    BUDGET = "budget"
    VALIDATION = "validation"
    #: 分析跑完了但**结果存不进去**（约束冲突、连接中断…）。
    #: 与 input/auth/budget 并列为一类，因为它同样"再试一次没有意义"：
    #: 重试只会把同一份结论再写一次、再失败一次，还要为此再付一次模型钱。
    STORAGE = "storage"
    UNKNOWN = "unknown"


#: 哪些分类值得重试。计划第 715–716 行给的是具体情形，这里落成分类。
RETRYABLE_KINDS: frozenset[str] = frozenset({ErrorKind.RETRYABLE})


class NonRetryableError(RuntimeError):
    """明确不该重试的错误。

    计划第 716 行：输入格式错误、认证失败、预算耗尽、校验失败。
    对它们重试只是浪费时间与配额，还会掩盖真正的原因。
    """

    kind = ErrorKind.UNKNOWN


class InputFormatError(NonRetryableError):
    kind = ErrorKind.INPUT


class AuthenticationError(NonRetryableError):
    kind = ErrorKind.AUTH


class BudgetExhaustedError(NonRetryableError):
    kind = ErrorKind.BUDGET


class ValidationFailureError(NonRetryableError):
    kind = ErrorKind.VALIDATION


class StorageFailureError(NonRetryableError):
    """结果落库失败（约束冲突、连接中断、序列化失败…）。

    为什么必须单独一类：落库失败**发生在模型已经调完、钱已经花了之后**。
    若把它当"未知错误"，降级链会继续往 L2→L1 再各调一次模型，每次产出
    同一份写不进去的结论 —— 三次模型钱全白花，最后还得退到纯规则报告，
    真正的原因（写库失败）反而被埋在最里面。
    """

    kind = ErrorKind.STORAGE


def classify_error(exc: BaseException) -> str:
    """把一个异常归入上面的分类。

    判定顺序有意义：先看明确的不可重试类型，再看可重试的网络/限流类，
    最后兜底 unknown（**未知错误不当作可重试**——盲目重试未知错误
    可能放大故障，也可能在花冤枉钱）。
    """
    if isinstance(exc, NonRetryableError):
        return exc.kind
    if isinstance(exc, (ModelRequestError, ConfigurationError)):
        return ErrorKind.AUTH
    if isinstance(exc, StopExecution):
        # 预算/额度到顶：计划第 716 行明确不可重试
        return ErrorKind.BUDGET
    if isinstance(exc, ModelUnavailableError):
        return ErrorKind.RETRYABLE
    if isinstance(exc, StructuredOutputError):
        # **可重试**：结构化输出失败通常是"模型没按格式给"或"输出被 token 上限截断"，
        # 而这两件事都是**随等级变化**的 —— 换个等级/换个预算往往就能过。
        # 阶段 08 的降级链（L3→L2→L1）正是为这种情况设计的。
        #
        # 早先把它归为"校验失败不可重试"，后果是：L3 因输出截断失败后，
        # 整条链路直接退到纯规则报告（零结论），而降级链根本没被用上。
        # 真正不可重试的是**输入**类校验失败（InputFormatError 等）。
        return ErrorKind.RETRYABLE
    return ErrorKind.UNKNOWN


def is_retryable(exc: BaseException) -> bool:
    return classify_error(exc) in RETRYABLE_KINDS
