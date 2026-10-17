"""API 请求 / 响应模型（Pydantic）。

**参数校验放在这里**（阶段 10 验收第 1 条）：进入业务代码之前，形状与取值
范围就已经被约束。特别注意：

- 时间必须带时区 —— 与阶段 04/07/08 同一条理由（naive 时间会让时间窗口与
  幂等键悄悄错位）；
- `time_range` 的 start 不能晚于 end；
- 未知字段一律拒绝（`extra="forbid"`），避免前端拼错字段名却"看起来成功了"。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette import status

#: 422 的状态常量。
#
# Starlette 1.7 起把 `HTTP_422_UNPROCESSABLE_ENTITY` 改名成
# `HTTP_422_UNPROCESSABLE_CONTENT`，旧名开始报弃用告警 —— 而告警长期存在会
# 训练人忽略告警输出，真正的新告警也就看不见了。
#
# 用 `getattr(..., None)` 而不是给它一个默认值：默认值会被**立即求值**，
# 在装着新版 Starlette 的环境里照样去碰那个已弃用的属性，告警一条不少。
# 取不到再退回旧名，这样新旧两个版本都对，且新版下不再有告警。
HTTP_422 = getattr(status, "HTTP_422_UNPROCESSABLE_CONTENT", None) or (
    status.HTTP_422_UNPROCESSABLE_ENTITY
)


def _require_tz(value: datetime | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(f"{field} 必须带时区（如 2026-09-23T12:00:00+08:00）")
    return value.astimezone(timezone.utc)


class StrictModel(BaseModel):
    """未知字段直接拒绝，避免拼错字段名却被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


# ============================================================
# 鉴权
# ============================================================


class RegisterRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=72)

    @field_validator("password")
    @classmethod
    def password_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 72:
            raise ValueError("口令 UTF-8 编码不能超过 72 字节")
        return value


class LoginRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=72)


class TokenResponse(StrictModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"


# ============================================================
# Project / DataSource
# ============================================================


class CreateProjectRequest(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    budget_total: float = Field(default=0.0, ge=0)

    @field_validator("budget_total")
    @classmethod
    def _budget_granularity(cls, value: float) -> float:
        """预算只允许 0 或 ≥ 0.0001，中间那一段**明确拒绝**。

        `projects.budget_total` 是 `Numeric(12,4)`，比 0.0001 更小的值会被
        数据库四舍五入成 0.0000 —— 而 0 的语义是"未设预算（不拦）"。
        于是 `0.00001` 这种"我想卡得很死"的输入会**静默变成不卡**，
        方向正好是危险的那一边（多花钱）。宁可报错说清楚。
        """
        if 0 < value < 0.0001:
            raise ValueError(
                f"budget_total={value} 太小：金额精度是 4 位小数（最小 0.0001）；"
                "填 0 表示不设预算上限"
            )
        return value


class ProjectResponse(StrictModel):
    id: int
    name: str
    status: str
    budget_total: float
    budget_used: float


class CreateDataSourceRequest(StrictModel):
    type: str = Field(default="file_upload", min_length=1, max_length=32)
    format: Literal["txt", "jsonl"]
    location: str = Field(default="", max_length=1024)
    metadata: dict[str, Any] = Field(default_factory=dict)


class DataSourceResponse(StrictModel):
    id: int
    project_id: int
    type: str
    format: str
    location: str
    status: str


# ============================================================
# 分析
# ============================================================


class TimeRange(StrictModel):
    start: datetime | None = None
    end: datetime | None = None

    @field_validator("start", "end")
    @classmethod
    def _tz_required(cls, value: datetime | None, info: Any) -> datetime | None:
        return _require_tz(value, field=info.field_name)

    @model_validator(mode="after")
    def _order(self) -> TimeRange:
        if self.start and self.end and self.start > self.end:
            raise ValueError("time_range.start 不能晚于 time_range.end")
        return self


class RunFilters(StrictModel):
    severity: list[Literal["high", "medium", "low"]] | None = None
    event_type: list[Literal["log", "metric"]] | None = None
    keyword: str | None = Field(default=None, max_length=200)


class CreateRunRequest(StrictModel):
    data_source_id: int = Field(ge=1)
    time_range: TimeRange | None = None
    filters: RunFilters | None = None
    #: 可选：指定起始等级。不传则由复杂度评估决定（正常路径）。
    start_tier: Literal["L1", "L2", "L3"] | None = None


class CreateRunResponse(StrictModel):
    """计划第 867 行：创建分析**立即返回** `run_id + queued`。"""

    run_id: int
    status: str
    reused: bool = False


class RunResponse(StrictModel):
    """计划第 868 行：状态、进度、成本。"""

    id: int
    project_id: int
    status: str
    current_phase: str | None = None
    phase_history: list[dict[str, Any]] | None = None
    tokens_input: int
    tokens_output: int
    cost_actual: float
    error: str | None = None
    run_metadata: dict[str, Any] | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_heartbeat: datetime | None = None
    cancel_requested: bool = False


class InsightResponse(StrictModel):
    id: int
    project_id: int
    run_id: int
    incident_id: int | None = None
    type: str
    severity: str
    confidence: float
    title: str
    summary: str
    reasoning: str | None = None
    limitations: str | None = None
    # 确定性诊断信息（命中的 analyzer、runbook 快照等）
    run_metadata: dict[str, Any] | None = None
    created_at: datetime


class EvidenceResponse(StrictModel):
    events: list[dict[str, Any]] = Field(default_factory=list)
    id: int
    insight_id: int
    source_id: int | None = None
    event_ids: list[Any] | None = None
    time_range: dict[str, Any] | None = None
    calculation: str | None = None
    description: str


class UploadResponse(StrictModel):
    data_source_id: int
    created_data_source: bool
    format: str
    stored_filename: str
    events_persisted: int
    parse: dict[str, Any]
    mask: dict[str, Any]


# ============================================================
# 知识审核
# ============================================================


class KnowledgeCandidateResponse(StrictModel):
    id: str
    kind: str
    title: str
    description: str = ""
    match: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    status: str = "draft"


class EditCandidateRequest(StrictModel):
    """编辑后再确认（计划第 878 行）。只允许改这几项。"""

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=2000)
    match: dict[str, Any] | None = None
    severity_hint: Literal["high", "medium", "low"] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class KnowledgeActionResponse(StrictModel):
    candidate_id: str
    action: str
    status: str
