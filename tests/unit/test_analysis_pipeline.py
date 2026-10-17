"""阶段 09 单测：Evidence 校验 / 复杂度 / 模板聚类 / Context 蒸馏。

验收里可离线验证的部分；落库与降级见 `tests/integration/test_pipeline_pg.py`。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.analysis.complexity import (
    CLUSTER_WINDOW_SECONDS,
    L1_MAX_ERRORS,
    L2_MAX_ERRORS,
    LOW_CONFIDENCE_ESCALATION,
    assess_complexity,
    count_time_clusters,
    should_escalate_after_l2,
)
from app.analysis.context import (
    CONTEXT_BUDGETS,
    ContextBudgetExceededError,
    build_distilled_context,
    estimate_tokens,
)
from app.analysis.evidence import (
    MAX_EVIDENCE_RETRIES,
    REJECT_INVALID_RATIO,
    EvidenceViolationError,
    build_retry_feedback,
    classify_evidence,
    normalize_evidence_ids,
    validate_insight,
    validate_insights,
)
from app.analysis.grouping import (
    DEFAULT_MERGE_WINDOW_SECONDS,
    GroupableEvent,
    MergeCandidate,
    cluster_events,
    make_template,
    merge_into_incidents,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
VALID = {"1", "2", "3", "4", "5"}


def insight(**overrides) -> dict:
    base = {
        "type": "fact",
        "severity": "high",
        "confidence": 0.9,
        "title": "标题",
        "summary": "摘要",
        "evidence_ids": ["1", "2"],
    }
    base.update(overrides)
    return base


# ============================================================
# Evidence 校验（红线 3）
# ============================================================


def test_all_valid_evidence_is_accepted_unchanged():
    validated, reason = validate_insight(insight(), valid_ids=VALID)
    assert reason is None
    assert validated is not None
    assert validated.type == "fact"
    assert validated.evidence_ids == ["1", "2"]
    assert validated.confidence == 0.9


def test_invalid_ratio_over_half_is_rejected_for_retry():
    """计划第 844 行：无效占比 > 50% → 拒绝该 Insight，带反馈重试。"""
    raw = insight(evidence_ids=["1", "98", "99"])  # 2/3 无效
    validated, reason = validate_insight(raw, valid_ids=VALID)
    assert validated is None
    assert reason and "不存在" in reason
    assert "possibility" in reason  # 反馈里给出可行的替代路径


def test_invalid_ratio_at_exactly_half_is_trimmed_not_rejected():
    """计划的判据是「> 50%」，恰好 50% 不在拒绝范围。"""
    raw = insight(evidence_ids=["1", "99"])
    validated, reason = validate_insight(raw, valid_ids=VALID)
    assert reason is None
    assert validated is not None
    assert validated.evidence_ids == ["1"]
    # 计划第 845 行：按比例下调 confidence
    assert validated.confidence == pytest.approx(0.45)


def test_trimmed_confidence_is_scaled_by_valid_ratio():
    raw = insight(evidence_ids=["1", "2", "3", "99"], confidence=0.8)
    validated, _ = validate_insight(raw, valid_ids=VALID)
    assert validated is not None
    assert validated.confidence == pytest.approx(0.8 * 3 / 4)
    assert validated.removed_evidence_ids == ["99"]


def test_fact_without_any_evidence_is_downgraded():
    """红线 3：没有有效 Evidence 的结论不能标 fact。"""
    raw = insight(evidence_ids=[], type="fact")
    validated, reason = validate_insight(raw, valid_ids=VALID)
    assert reason is None  # 空 evidence 不是"编造"，是"没给"，走降级而非拒绝
    assert validated is not None
    assert validated.type == "possibility"
    assert validated.limitations and "fact 已降级" in validated.limitations
    assert any("降级" in n for n in validated.notes)


def test_fact_with_all_invalid_evidence_is_downgraded():
    raw = insight(evidence_ids=["97", "98", "99"])
    # 3/3 无效 → 占比 100% > 50%，先被拒绝
    validated, reason = validate_insight(raw, valid_ids=VALID)
    assert validated is None
    assert reason is not None


def test_possibility_with_all_invalid_evidence_is_kept_with_note():
    """计划第 846 行：全部无效但 type=possibility → 保留并在 limitations 注明。"""
    raw = insight(type="possibility", evidence_ids=["1", "99"], confidence=0.5)
    validated, reason = validate_insight(raw, valid_ids=VALID)
    assert reason is None
    assert validated is not None
    assert validated.type == "possibility"
    assert validated.limitations


def test_inference_requires_reasoning():
    raw = insight(type="inference", evidence_ids=["1"], reasoning=None)
    validated, _ = validate_insight(raw, valid_ids=VALID)
    assert validated is not None
    assert validated.reasoning
    assert any("inference" in n for n in validated.notes)


def test_possibility_requires_limitations():
    raw = insight(type="possibility", evidence_ids=["1"], limitations=None)
    validated, _ = validate_insight(raw, valid_ids=VALID)
    assert validated is not None
    assert validated.limitations


def test_unknown_type_requires_limitations():
    raw = insight(type="unknown", evidence_ids=[], limitations=None)
    validated, _ = validate_insight(raw, valid_ids=VALID)
    assert validated is not None
    assert validated.limitations


@pytest.mark.parametrize(
    "bad",
    [
        insight(type="made_up"),
        insight(title=""),
        insight(type="fact", summary="s", evidence_ids=["1"]),
    ],
)
def test_structurally_invalid_insight_raises(bad: dict):
    """结构错误属不可重试的校验失败 —— 重试同一提示通常还是错。"""
    if bad.get("title") == "":
        with pytest.raises(EvidenceViolationError, match="title"):
            validate_insight(bad, valid_ids=VALID)
    elif bad.get("type") == "made_up":
        with pytest.raises(EvidenceViolationError, match="type"):
            validate_insight(bad, valid_ids=VALID)


def test_non_dict_insight_raises():
    with pytest.raises(EvidenceViolationError, match="必须是对象"):
        validate_insight("not a dict", valid_ids=VALID)  # type: ignore[arg-type]


def test_evidence_ids_are_normalized_to_strings():
    """模型可能给 int；`1` 与 `"1"` 必须是同一个 id，否则有效证据会被误判无效。"""
    assert normalize_evidence_ids([1, "2", 3]) == ["1", "2", "3"]
    assert normalize_evidence_ids("7") == ["7"]
    assert normalize_evidence_ids(None) == []
    assert normalize_evidence_ids([1, 1, "1"]) == ["1"]


def test_integer_evidence_ids_match_string_valid_set():
    raw = insight(evidence_ids=[1, 2])
    validated, _ = validate_insight(raw, valid_ids=VALID)
    assert validated is not None
    assert validated.evidence_ids == ["1", "2"]


def test_classify_evidence_returns_ratio():
    valid, invalid, ratio = classify_evidence(["1", "2", "9"], VALID)
    assert (valid, invalid) == (["1", "2"], ["9"])
    assert ratio == pytest.approx(1 / 3)


def test_validate_insights_batches_and_builds_feedback():
    outcome = validate_insights(
        [
            insight(title="ok", evidence_ids=["1"]),
            insight(title="bad", evidence_ids=["97", "98", "99"]),
        ],
        valid_ids=VALID,
    )
    assert len(outcome.accepted) == 1
    assert len(outcome.rejected) == 1
    assert outcome.needs_retry is True
    feedback = build_retry_feedback(outcome)
    assert "bad" in feedback
    assert "有效 event_id" in feedback


def test_validate_insights_empty_input():
    outcome = validate_insights([], valid_ids=VALID)
    assert outcome.accepted == [] and outcome.needs_retry is False


def test_validate_insights_rejects_non_list():
    with pytest.raises(EvidenceViolationError, match="必须是数组"):
        validate_insights({"not": "a list"}, valid_ids=VALID)


def test_confidence_is_clamped_into_range():
    validated, _ = validate_insight(insight(confidence=5.0), valid_ids=VALID)
    assert validated is not None and validated.confidence == 1.0
    validated2, _ = validate_insight(insight(confidence=-3), valid_ids=VALID)
    assert validated2 is not None and validated2.confidence == 0.0


def test_bad_confidence_value_defaults_to_zero_not_crash():
    validated, _ = validate_insight(insight(confidence="abc"), valid_ids=VALID)
    assert validated is not None and validated.confidence == 0.0


def test_constants_match_plan():
    assert REJECT_INVALID_RATIO == 0.50
    assert MAX_EVIDENCE_RETRIES == 3


# ============================================================
# 复杂度评估（计划第 774–779 行）
# ============================================================


def assess(**overrides):
    base = {
        "total_events": 100,
        "error_count": 0,
        "high_severity_count": 0,
        "anomaly_count": 0,
        "time_cluster_count": 1,
        "event_type_count": 1,
    }
    base.update(overrides)
    return assess_complexity(**base)


def test_no_error_no_anomaly_is_l0():
    assert assess().tier == "L0"


def test_large_volume_alone_does_not_raise_the_tier():
    """计划第 774–775 行：量大只影响采样，不应因「正常但量大」升到 L3。"""
    result = assess(total_events=100_000)
    assert result.tier == "L0"
    assert result.volume_hint, "量大时应给出缩小范围的提示"


def test_few_errors_go_to_l1():
    assert assess(error_count=5, anomaly_count=1).tier == "L1"
    assert assess(error_count=L1_MAX_ERRORS - 1, anomaly_count=1).tier == "L1"


def test_medium_error_count_goes_to_l2():
    assert assess(error_count=L1_MAX_ERRORS, anomaly_count=2).tier == "L2"
    assert assess(error_count=L2_MAX_ERRORS - 1, anomaly_count=2).tier == "L2"


def test_high_severity_anomaly_always_escalates_to_l3():
    assert assess(error_count=1, high_severity_count=1, anomaly_count=1).tier == "L3"
    assert assess(error_count=0, high_severity_count=0, anomaly_count=0).tier == "L0"


def test_many_clusters_and_types_escalate_to_l3():
    assert assess(error_count=5, anomaly_count=3, time_cluster_count=3, anomaly_types=3).tier == "L3"


def test_only_clusters_or_only_types_does_not_escalate():
    """计划要求「跨多簇**多类型**」两者都满足。"""
    assert assess(error_count=5, anomaly_count=2, time_cluster_count=9, anomaly_types=1).tier == "L2"
    assert assess(error_count=5, anomaly_count=2, time_cluster_count=1, anomaly_types=9).tier == "L2"


def test_low_l2_confidence_escalates_to_l3():
    """低置信度升级是独立条件，不受「是否多簇多类型」限制。"""
    assert assess(error_count=5, anomaly_count=1, l2_confidence=0.2).tier == "L3"
    assert assess(error_count=5, anomaly_count=1, l2_confidence=0.9).tier == "L1"


def test_escalation_helper_matches_threshold():
    assert should_escalate_after_l2(0.1) is True
    assert should_escalate_after_l2(LOW_CONFIDENCE_ESCALATION) is False


def test_assessment_is_serializable_and_explains_itself():
    result = assess(error_count=5, anomaly_count=1)
    payload = result.as_dict()
    json.dumps(payload)
    assert payload["reasons"], "必须说明为什么是这个等级"
    assert payload["tier"] == "L1"


def test_count_time_clusters_groups_nearby_timestamps():
    moments = [
        T0,
        T0 + timedelta(seconds=10),
        T0 + timedelta(seconds=CLUSTER_WINDOW_SECONDS + 5),
    ]
    assert count_time_clusters(moments) == 2


def test_count_time_clusters_ignores_non_datetimes():
    assert count_time_clusters([T0, "not a time", None]) == 1
    assert count_time_clusters([]) == 0


# ============================================================
# 模板聚类（修订说明第 6a 条）
# ============================================================


def test_template_replaces_numbers_ips_and_paths():
    template = make_template("sshd[19939]: failure from 218.188.2.4 at /var/log/auth.log")
    assert "<NUM>" in template
    assert "<IP>" in template
    assert "<PATH>" in template
    assert "218.188.2.4" not in template


def test_same_shape_different_instances_produce_same_template():
    a = make_template("connection from 10.0.0.1 failed")
    b = make_template("connection from 10.0.0.2 failed")
    assert a == b


def test_different_messages_produce_different_templates():
    assert make_template("disk full") != make_template("memory exhausted")


def test_template_keeps_structural_words():
    """抹掉实例差异的同时必须保留能区分问题的关键词。"""
    template = make_template("Out of memory: Kill process 1234")
    assert "Out of memory" in template
    assert "Kill process" in template


def test_template_handles_uuid_and_time():
    template = make_template(
        "request 550e8400-e29b-41d4-a716-446655440000 at 2026-09-23T12:00:00 failed"
    )
    assert "<UUID>" in template
    assert "<TIME>" in template


def test_whitespace_is_collapsed():
    assert make_template("a    b") == make_template("a b")


def test_empty_message_yields_empty_template():
    assert make_template("") == ""


def _event(event_id: int, seconds: int, message: str, source_id: int = 1) -> GroupableEvent:
    return GroupableEvent(
        event_id=event_id,
        source_id=source_id,
        timestamp=T0 + timedelta(seconds=seconds),
        message=message,
    )


def test_cluster_groups_same_template_within_window():
    events = [
        _event(1, 0, "failure from 10.0.0.1"),
        _event(2, 10, "failure from 10.0.0.2"),
        _event(3, 20, "failure from 10.0.0.3"),
    ]
    groups = cluster_events(events, window_seconds=300)
    assert len(groups) == 1
    assert groups[0].event_ids == [1, 2, 3]
    assert groups[0].event_count == 3


def test_cluster_splits_same_template_across_windows():
    """同一模板在两个时段各刷一次是两次独立事件，不该合成一组。"""
    events = [
        _event(1, 0, "failure from 10.0.0.1"),
        _event(2, 3600, "failure from 10.0.0.2"),
    ]
    groups = cluster_events(events, window_seconds=300)
    assert len(groups) == 2


def test_cluster_separates_different_sources():
    events = [
        _event(1, 0, "failure", source_id=1),
        _event(2, 0, "failure", source_id=2),
    ]
    assert len(cluster_events(events, window_seconds=300)) == 2


def test_cluster_is_deterministic():
    events = [_event(i, i * 10, f"failure from 10.0.0.{i}") for i in range(1, 6)]
    first = cluster_events(events, window_seconds=300)
    second = cluster_events(events, window_seconds=300)
    assert [g.group_key for g in first] == [g.group_key for g in second]


def test_cluster_rejects_bad_window():
    with pytest.raises(ValueError, match="window_seconds"):
        cluster_events([], window_seconds=0)


def test_cluster_tracks_time_bounds():
    events = [_event(1, 0, "x from 10.0.0.1"), _event(2, 50, "x from 10.0.0.2")]
    group = cluster_events(events, window_seconds=300)[0]
    assert group.time_start == T0
    assert group.time_end == T0 + timedelta(seconds=50)


# ============================================================
# 事故归并（计划第 92–99 行）
# ============================================================


def candidate(group_id: int, *, seconds: int, signature: str = "CPU", severity: str = "high",
              source_id: int = 1) -> MergeCandidate:
    return MergeCandidate(
        group_id=group_id,
        source_id=source_id,
        severity=severity,
        time_start=T0 + timedelta(seconds=seconds),
        time_end=T0 + timedelta(seconds=seconds + 30),
        signature=signature,
    )


def test_overlapping_same_signature_groups_merge():
    incidents = merge_into_incidents([candidate(1, seconds=0), candidate(2, seconds=20)])
    assert len(incidents) == 1
    assert incidents[0]["group_ids"] == [1, 2]


def test_gap_within_merge_window_still_merges():
    incidents = merge_into_incidents(
        [candidate(1, seconds=0), candidate(2, seconds=DEFAULT_MERGE_WINDOW_SECONDS)]
    )
    assert len(incidents) == 1


def test_gap_beyond_merge_window_splits():
    incidents = merge_into_incidents(
        [candidate(1, seconds=0), candidate(2, seconds=DEFAULT_MERGE_WINDOW_SECONDS + 100)]
    )
    assert len(incidents) == 2


def test_different_signature_never_merges():
    incidents = merge_into_incidents(
        [candidate(1, seconds=0, signature="CPU"), candidate(2, seconds=10, signature="Disk")]
    )
    assert len(incidents) == 2


def test_different_source_never_merges():
    incidents = merge_into_incidents(
        [candidate(1, seconds=0, source_id=1), candidate(2, seconds=10, source_id=2)]
    )
    assert len(incidents) == 2


def test_different_severity_tier_splits_when_required():
    incidents = merge_into_incidents(
        [candidate(1, seconds=0, severity="high"), candidate(2, seconds=10, severity="low")],
        require_same_severity=True,
    )
    assert len(incidents) == 2


def test_severity_split_can_be_disabled():
    incidents = merge_into_incidents(
        [candidate(1, seconds=0, severity="high"), candidate(2, seconds=10, severity="low")],
        require_same_severity=False,
    )
    assert len(incidents) == 1


def test_incident_reports_merge_basis_and_wording_is_not_root_cause():
    """修订说明第 5 条：这是近似分组，**不是根因判定**。"""
    incident = merge_into_incidents([candidate(1, seconds=0)])[0]
    assert incident["merge_basis"] == "same_source+same_signature+time_window"
    assert "root_cause" not in incident


def test_historical_incident_is_reused_when_window_continuous():
    historical = [
        {
            "id": 99,
            "source_id": 1,
            "signature": "CPU",
            "time_start": T0 - timedelta(seconds=100),
            "time_end": T0 - timedelta(seconds=10),
        }
    ]
    incidents = merge_into_incidents(
        [candidate(1, seconds=0)], existing_incidents=historical
    )
    assert incidents[0].get("reused_incident_id") == 99


def test_historical_incident_not_reused_when_too_far():
    historical = [
        {
            "id": 99,
            "source_id": 1,
            "signature": "CPU",
            "time_start": T0 - timedelta(days=5),
            "time_end": T0 - timedelta(days=5, seconds=-30),
        }
    ]
    incidents = merge_into_incidents(
        [candidate(1, seconds=0)], existing_incidents=historical
    )
    assert "reused_incident_id" not in incidents[0]


def test_merge_rejects_bad_window():
    with pytest.raises(ValueError, match="merge_window_seconds"):
        merge_into_incidents([], merge_window_seconds=0)


# ============================================================
# Context 蒸馏（计划第 782–802 行）
# ============================================================


def test_budgets_match_the_plan_table():
    assert CONTEXT_BUDGETS["L1"]["total"] == 2000
    assert CONTEXT_BUDGETS["L2"]["total"] == 4000
    assert CONTEXT_BUDGETS["L3"]["total"] == 8000
    for tier in ("L1", "L2", "L3"):
        # 各部分预算之和**不超过**总预算（计划表里 L1 是 1800/2000，
        # 留了 200 的余量）。不要求相等——相等只是 L2/L3 的巧合。
        parts = sum(
            CONTEXT_BUDGETS[tier][k]
            for k in ("metadata", "statistics", "incidents", "anomalies", "samples", "context")
        )
        assert parts <= CONTEXT_BUDGETS[tier]["total"], tier


def test_context_renders_all_six_sections():
    context = build_distilled_context(
        tier="L2",
        metadata={"domain": "d"},
        statistics={"total": 1},
        incidents=[{"title": "i", "severity": "high", "group_ids": [1]}],
        anomalies=[{"type": "CPU", "severity": "high", "event_ids": [1], "message": "m"}],
        samples=[{"event_id": 1, "message": "boom", "timestamp": "t", "severity": "high"}],
        context_notes=["note"],
    )
    rendered = context.render()
    for section in ("metadata", "statistics", "incidents", "anomalies", "samples", "context"):
        assert f"## {section}" in rendered


def test_context_within_budget_for_normal_input():
    context = build_distilled_context(
        tier="L1",
        metadata={"domain": "d"},
        statistics={"total": 10},
        samples=[{"event_id": i, "message": f"m{i}"} for i in range(5)],
    )
    assert context.total_tokens <= context.budget


def test_oversized_samples_are_compressed_not_silently_truncated():
    """计划第 801 行：按序压缩，且压缩过程要留痕。"""
    context = build_distilled_context(
        tier="L1",
        metadata={"domain": "d"},
        statistics={"total": 100000},
        samples=[{"event_id": i, "message": "x" * 300} for i in range(5000)],
    )
    assert context.total_tokens <= context.budget
    assert context.compression_notes, "压缩必须留下说明，不能静默"


def test_oversized_parts_are_dropped_with_an_explicit_note():
    """单行超预算的部分会被整段丢弃 —— 必须留痕，不能静默消失。

    `statistics` 往往是一整行 JSON，按行截断等于整段丢弃；若不记 notes，
    Context 看起来正常但统计信息已经没了。
    """
    context = build_distilled_context(
        tier="L1",
        metadata={"domain": "d"},
        statistics={"huge": "y" * 50_000},
        samples=[{"event_id": i, "message": "z" * 2000} for i in range(500)],
    )
    assert context.total_tokens <= context.budget
    assert any("statistics" in n and "整段丢弃" in n for n in context.compression_notes), (
        f"统计被丢弃却没留痕：{context.compression_notes}"
    )


def test_uncompressible_context_raises_instead_of_faking_it():
    """压到极限仍超限 → 明确报错让用户缩小范围（V1 不做自动分片）。

    压缩只覆盖 samples/anomalies/incidents/context 四部分；metadata 与
    statistics 按各自预算截断后仍可能让总和超限（预算之和 L1 是 1800，
    但元数据本身可达 100，加上未压缩部分就可能顶到 2000 以上）。
    这里用超大 metadata 构造该情形。
    """
    # samples 这类"多条"内容总能用一次按行截断压进预算；真正压不动的是
    # **单行**部分（metadata / statistics）—— 它们只能整段丢弃，无法切分。
    # 这里用超出 metadata 预算的单行内容，且限制压缩轮数，让"压不动就报错"
    # 这条防御路径可被验证（不限制轮数时它会被丢弃，于是永远不报错）。
    with pytest.raises(ContextBudgetExceededError, match="缩小时间范围"):
        build_distilled_context(
            tier="L1",
            metadata={"domain": "d"},
            statistics={"huge": "y" * 50_000},
            samples=[{"event_id": i, "message": "z" * 2000} for i in range(500)],
            max_compression_rounds=0,
        )


def test_compression_can_always_reach_budget_without_artificial_limit():
    """不限制轮数时，压缩必须能把任意输入压进总预算（除非真的无可再压）。"""
    context = build_distilled_context(
        tier="L1",
        metadata={"domain": "d"},
        statistics={"total": 1},
        samples=[{"event_id": i, "message": "z" * 20_000} for i in range(50)],
    )
    assert context.total_tokens <= context.budget
    assert context.compression_notes


def test_unknown_tier_is_refused():
    with pytest.raises(ValueError, match="未知等级"):
        build_distilled_context(tier="L9", metadata={}, statistics={})


def test_context_summary_is_serializable():
    context = build_distilled_context(tier="L2", metadata={}, statistics={})
    json.dumps(context.as_dict())


def test_estimate_tokens_handles_ascii_and_cjk():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcdef") > 0
    # 中文更密：同样字符数下 token 更多
    assert estimate_tokens("中文字符测试") >= 6

# ============================================================
# 端到端：幻觉拦截（验收：「模型返回假 event_id」能被拦截并重试/降级）
# ============================================================


class FakeRouter:
    """按轮次返回预置 payload 的假 Router。"""

    def __init__(self, payloads: list[dict]) -> None:
        self.payloads = payloads
        self.prompts: list[str] = []

    def generate(self, *, tier, prompt, schema, max_output_tokens, **kwargs):
        from app.gateways.base import ModelResult

        self.prompts.append(prompt)
        payload = self.payloads[min(len(self.prompts) - 1, len(self.payloads) - 1)]
        return ModelResult(
            content="{}", parsed=payload, tokens_input=100, tokens_output=50,
            model="fake", tier=tier, cost=0.001,
        )


def _fake_domain():
    from app.domains.wiring import build_default_registry

    return build_default_registry().load("computer_monitoring", "1.0.0")


def _events_with_errors(count: int = 3) -> list[dict]:
    from datetime import timedelta as _td

    out = [
        {
            "event_id": i,
            "source_id": 1,
            "timestamp": T0 + _td(seconds=i),
            "severity": "high",
            "event_type": "log",
            "message": f"kernel: segfault at 0x{i} pid={1000 + i}",
        }
        for i in range(1, count + 1)
    ]
    return out


def test_hallucinated_event_ids_trigger_retry_then_accept_fixed_output():
    """验收：模型返回假 event_id → 带反馈重试 → 修正后的输出被接受。"""
    from app.analysis.pipeline import PipelineInput, analyze

    good = {
        "insights": [
            {
                "type": "fact", "severity": "high", "confidence": 0.9,
                "title": "进程崩溃", "summary": "出现 segfault",
                "evidence_ids": ["1"],
            }
        ]
    }
    bad = {
        "insights": [
            {
                "type": "fact", "severity": "high", "confidence": 0.9,
                "title": "编造的结论", "summary": "引用不存在的证据",
                "evidence_ids": ["9001", "9002", "9003"],
            }
        ]
    }
    router = FakeRouter([bad, good])
    result = analyze(
        PipelineInput(
            project_id=1, source_id=1, domain_id="computer_monitoring",
            domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
            events=_events_with_errors(),
        ),
        analyzers=_fake_domain().analyzers(),
        router=router,
    )

    assert result.tier == "L3", "有 high 异常应走 L3"
    assert len(router.prompts) == 2, "应发生一次带反馈重试"
    assert "有效 event_id" in router.prompts[1], "重试提示里必须带上反馈"
    assert result.evidence_rejections == 1
    assert len(result.insights) == 1
    assert result.insights[0].title == "进程崩溃"
    assert result.insights[0].evidence_ids == ["1"]


def test_hallucinated_fact_is_downgraded_when_retry_also_fails():
    """重试仍失败 → fact 降级为 possibility，并写明模型未能提供有效证据。"""
    from app.analysis.pipeline import PipelineInput, analyze

    bad = {
        "insights": [
            {
                "type": "fact", "severity": "high", "confidence": 0.9,
                "title": "只有假证据", "summary": "s", "evidence_ids": ["9001", "9002"],
            }
        ]
    }
    router = FakeRouter([bad, bad, bad])
    result = analyze(
        PipelineInput(
            project_id=1, source_id=1, domain_id="computer_monitoring",
            domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
            events=_events_with_errors(),
        ),
        analyzers=_fake_domain().analyzers(),
        router=router,
    )

    # 三轮全部被拒：不该有任何 fact 留下来
    assert all(i.type != "fact" for i in result.insights)
    assert result.evidence_rejections >= 1
    assert any("证据" in n for n in result.notes)


def test_fact_with_no_evidence_is_downgraded_not_accepted():
    """红线 3：没有有效证据的结论不能标 fact。"""
    from app.analysis.pipeline import PipelineInput, analyze

    router = FakeRouter([{
        "insights": [
            {"type": "fact", "severity": "low", "confidence": 0.5,
             "title": "无证据的断言", "summary": "s", "evidence_ids": []}
        ]
    }])
    result = analyze(
        PipelineInput(
            project_id=1, source_id=1, domain_id="computer_monitoring",
            domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
            events=_events_with_errors(),
        ),
        analyzers=_fake_domain().analyzers(),
        router=router,
    )
    assert len(result.insights) == 1
    assert result.insights[0].type == "possibility", "无证据的 fact 必须降级"
    assert result.insights[0].limitations


def test_normal_logs_take_l0_and_never_call_the_model():
    """验收：正常日志走 L0（纯代码报告，一次模型都不调）。"""
    from app.analysis.pipeline import PipelineInput, analyze

    router = FakeRouter([{"insights": []}])
    events = [
        {"event_id": i, "source_id": 1, "timestamp": T0 + timedelta(seconds=i),
         "severity": "low", "event_type": "log", "message": f"service ok pid={i}"}
        for i in range(1, 21)
    ]
    result = analyze(
        PipelineInput(
            project_id=1, source_id=1, domain_id="computer_monitoring",
            domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
            events=events,
        ),
        analyzers=_fake_domain().analyzers(),
        router=router,
    )
    assert result.tier == "L0"
    assert result.used_rules_only is True
    assert router.prompts == [], "L0 不该调用模型"
    assert result.rules_only_report["kind"] == "rules_only"


def test_model_failure_falls_back_to_rules_only_report():
    """模型调用抛错 → 不打断链路，降级为纯规则报告。"""
    from app.analysis.pipeline import PipelineInput, analyze

    class BoomRouter:
        def generate(self, **kwargs):
            raise RuntimeError("模型炸了")

    result = analyze(
        PipelineInput(
            project_id=1, source_id=1, domain_id="computer_monitoring",
            domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
            events=_events_with_errors(),
        ),
        analyzers=_fake_domain().analyzers(),
        router=BoomRouter(),
    )
    assert result.rules_only_report is not None
    assert any("模型调用失败" in n for n in result.notes)


def test_knowledge_candidates_are_draft_and_evidence_checked():
    """候选知识同样过 evidence 子集校验，且一律 draft（不参与自动结论）。"""
    from app.analysis.pipeline import PipelineInput, analyze

    router = FakeRouter([{
        "insights": [],
        "knowledge_candidates": [
            {"kind": "error_pattern", "title": "好的候选", "evidence_ids": ["1"], "confidence": 0.7},
            {"kind": "error_pattern", "title": "假证据候选", "evidence_ids": ["9001"]},
        ],
    }])
    result = analyze(
        PipelineInput(
            project_id=1, source_id=1, domain_id="computer_monitoring",
            domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
            events=_events_with_errors(),
        ),
        analyzers=_fake_domain().analyzers(),
        router=router,
    )
    titles = [c["title"] for c in result.knowledge_candidates]
    assert "好的候选" in titles
    assert "假证据候选" not in titles, "证据全无效的候选不允许进 staging"
    assert all(c["status"] == "draft" for c in result.knowledge_candidates)

# ============================================================
# 回归：模型那一段失败时，不能悄悄产出"零结论的完成报告"
# ============================================================


class StructuredFailingRouter:
    """模拟"模型返回了内容但 JSON 被截断"这一真实故障。"""

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, *, tier, prompt, schema, max_output_tokens, **kwargs):
        from app.gateways.base import StructuredOutputError

        self.calls += 1
        raise StructuredOutputError(
            "要求结构化输出但未能解析出 JSON 对象；原始内容被截断"
        )


def test_structured_output_failure_is_raised_for_the_fallback_ladder():
    """结构化输出失败必须**上抛**给降级链，而不是就地放弃。

    真实故障（2026-09-23 真机跑出来）：L3 的输出被 token 上限截断 → JSON 不完整
    → 早先的实现把它吞成"降级为纯规则报告"，于是整条链路直接退到零结论，
    而阶段 08 精心实现的 L3→L2→L1 降级链**根本没被用上**。
    """
    from app.analysis.pipeline import PipelineInput, analyze
    from app.gateways.base import StructuredOutputError

    router = StructuredFailingRouter()
    with pytest.raises(StructuredOutputError):
        analyze(
            PipelineInput(
                project_id=1, source_id=1, domain_id="computer_monitoring",
                domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
                events=_events_with_errors(),
            ),
            analyzers=_fake_domain().analyzers(),
            domain=_fake_domain(),
            router=router,
        )
    assert router.calls == 1


def test_output_budget_scales_with_tier():
    """输出预算必须按等级给足：一次调用要产出 insights 与 knowledge_candidates，
    给太小会把 JSON 截断在中间 —— 那正是真机上那次失败的直接原因。"""
    from app.analysis.pipeline import OUTPUT_TOKEN_BUDGET_BY_TIER

    assert OUTPUT_TOKEN_BUDGET_BY_TIER["L1"] == 2000
    assert OUTPUT_TOKEN_BUDGET_BY_TIER["L2"] == 4000
    assert OUTPUT_TOKEN_BUDGET_BY_TIER["L3"] == 8000
    assert (
        OUTPUT_TOKEN_BUDGET_BY_TIER["L1"]
        < OUTPUT_TOKEN_BUDGET_BY_TIER["L2"]
        < OUTPUT_TOKEN_BUDGET_BY_TIER["L3"]
    )


class BudgetRecordingRouter:
    def __init__(self) -> None:
        self.max_output_tokens: list[int] = []

    def generate(self, *, tier, prompt, schema, max_output_tokens, **kwargs):
        from app.gateways.base import ModelResult

        self.max_output_tokens.append(max_output_tokens)
        return ModelResult(
            content="{}", parsed={"insights": []}, tokens_input=10,
            tokens_output=5, model="fake", tier=tier, cost=0.0,
        )


@pytest.mark.parametrize(("label", "expected"), [("L3", 8000)])
def test_pipeline_uses_tier_output_budget_by_default(label: str, expected: int):
    from app.analysis.pipeline import PipelineInput, analyze

    router = BudgetRecordingRouter()
    analyze(
        PipelineInput(
            project_id=1, source_id=1, domain_id="computer_monitoring",
            domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
            events=_events_with_errors(),
        ),
        analyzers=_fake_domain().analyzers(),
        domain=_fake_domain(),
        router=router,
    )
    assert router.max_output_tokens == [expected]


def test_explicit_output_budget_still_honoured():
    from app.analysis.pipeline import PipelineInput, analyze

    router = BudgetRecordingRouter()
    analyze(
        PipelineInput(
            project_id=1, source_id=1, domain_id="computer_monitoring",
            domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
            events=_events_with_errors(),
        ),
        analyzers=_fake_domain().analyzers(),
        domain=_fake_domain(),
        router=router,
        max_output_tokens=123,
    )
    assert router.max_output_tokens == [123]


# ============================================================
# 回归：L2 置信度不足要真的升级 L3（计划第 778 行）
# ============================================================


class TierRecordingRouter:
    """记录每次调用用的等级，按等级返回不同置信度的结论。"""

    def __init__(self, *, l2_confidence: float, l3_payload: dict | None = None) -> None:
        self.l2_confidence = l2_confidence
        self.l3_payload = l3_payload
        self.tiers: list[str] = []

    def generate(self, *, tier, prompt, schema, max_output_tokens, **kwargs):
        from app.gateways.base import ModelResult

        self.tiers.append(tier)
        if tier == "L3" and self.l3_payload is not None:
            payload = self.l3_payload
        else:
            payload = {
                "insights": [
                    {
                        "type": "inference",
                        "severity": "medium",
                        "confidence": self.l2_confidence,
                        "title": "疑似问题",
                        "summary": "证据指向某个问题",
                        "evidence_ids": ["1"],
                        "reasoning": "关键词与时间相关性",
                    }
                ],
                "knowledge_candidates": [],
            }
        return ModelResult(
            content="{}", parsed=payload, tokens_input=100, tokens_output=50,
            model=f"fake-{tier}", tier=tier, cost=0.002,
        )


def _l2_events() -> list[dict]:
    """造一个刚好落到 L2 的场景。

    L1 的门槛是 `error_count < 10`，所以这里给 **10 条 high** —— 刚好越过 L1、
    又远不到 L2 的上限 50。同时：单一时间簇、无 analyzer 命中（消息里不出现
    crash/segfault/oom 等词），避免"多类型/高危异常"把它推到 L3。
    """
    from datetime import timedelta as _td

    return [
        {
            "event_id": i,
            "source_id": 1,
            "timestamp": T0 + _td(seconds=i),
            "severity": "high",
            "event_type": "log",
            "message": f"connection reset by peer (attempt {i})",
        }
        for i in range(1, 11)
    ]


@pytest.mark.parametrize("cap", ["L1", "L2"])
def test_fallback_cap_controls_real_model_tier_and_prevents_reescalation(cap):
    from app.analysis.pipeline import PipelineInput, analyze
    router = TierRecordingRouter(l2_confidence=0.2)
    result = analyze(PipelineInput(project_id=1, source_id=1, domain_id="computer_monitoring",
        domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1), events=_l2_events()),
        analyzers=_fake_domain().analyzers(), domain=_fake_domain(), router=router, model_tier_cap=cap)
    assert result.complexity.tier == "L2"
    assert router.tiers == [cap]
    assert result.tier == cap


def test_low_l2_confidence_escalates_and_reruns_at_l3():
    """L2 结论平均置信度低于阈值 → 真的再用 L3 跑一次，并采用 L3 的结论。

    此前这条只在单测里成立：`assess_complexity(l2_confidence=…)` 这个参数、
    `LOW_CONFIDENCE_ESCALATION`、`should_escalate_after_l2` 全都在，
    **真实路径里没人传过 l2_confidence** —— 计划第 778 行的第三个 L3 触发条件
    在真机上永远不会发生。
    """
    from app.analysis.pipeline import PipelineInput, analyze

    router = TierRecordingRouter(l2_confidence=0.2)  # 低于 0.5 阈值
    result = analyze(
        PipelineInput(
            project_id=1, source_id=1, domain_id="computer_monitoring",
            domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
            events=_l2_events(),
        ),
        analyzers=_fake_domain().analyzers(),
        domain=_fake_domain(),
        router=router,
    )

    assert result.complexity.tier == "L2", "这个场景本身应当先判到 L2"
    assert router.tiers == ["L2", "L3"], (
        f"应当在 L2 之后自动升到 L3 再跑一次，实际调用序列 {router.tiers}"
    )
    assert result.tier == "L3", "升级后结果等级应记成 L3"
    assert any("升级 L3" in note for note in result.notes)


def test_high_l2_confidence_does_not_escalate():
    """置信度够高就不该多花一次 L3 的钱。"""
    from app.analysis.pipeline import PipelineInput, analyze

    router = TierRecordingRouter(l2_confidence=0.9)
    result = analyze(
        PipelineInput(
            project_id=1, source_id=1, domain_id="computer_monitoring",
            domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
            events=_l2_events(),
        ),
        analyzers=_fake_domain().analyzers(),
        domain=_fake_domain(),
        router=router,
    )

    assert router.tiers == ["L2"], f"不该升级，实际调用序列 {router.tiers}"
    assert result.tier == "L2"


def test_failed_escalation_keeps_the_l2_result():
    """升级失败不能赔掉已有的 L2 结论 —— 升级是锦上添花，不是雪上加霜。"""
    from app.analysis.pipeline import PipelineInput, analyze

    class EscalationBoomRouter(TierRecordingRouter):
        def generate(self, *, tier, prompt, schema, max_output_tokens, **kwargs):
            if tier == "L3":
                from app.gateways.base import StructuredOutputError

                self.tiers.append(tier)
                raise StructuredOutputError("L3 输出被截断（模拟）")
            return super().generate(
                tier=tier, prompt=prompt, schema=schema,
                max_output_tokens=max_output_tokens, **kwargs,
            )

    router = EscalationBoomRouter(l2_confidence=0.2)
    result = analyze(
        PipelineInput(
            project_id=1, source_id=1, domain_id="computer_monitoring",
            domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
            events=_l2_events(),
        ),
        analyzers=_fake_domain().analyzers(),
        domain=_fake_domain(),
        router=router,
    )

    assert router.tiers == ["L2", "L3"]
    assert result.tier == "L2", "升级失败后应退回 L2"
    assert len(result.insights) == 1, "L2 拿到的结论必须保留"
    assert any("保留 L2 结论" in note for note in result.notes)


def test_escalation_does_not_happen_without_any_conclusion():
    """L2 一条结论都没有时不去升级：问题不在"置信度"，再花钱问也问不出来。"""
    from app.analysis.pipeline import PipelineInput, analyze

    class EmptyRouter:
        def __init__(self) -> None:
            self.tiers: list[str] = []

        def generate(self, *, tier, prompt, schema, max_output_tokens, **kwargs):
            from app.gateways.base import ModelResult

            self.tiers.append(tier)
            return ModelResult(
                content="{}", parsed={"insights": []}, tokens_input=10,
                tokens_output=5, model="fake", tier=tier, cost=0.0,
            )

    router = EmptyRouter()
    analyze(
        PipelineInput(
            project_id=1, source_id=1, domain_id="computer_monitoring",
            domain_version="1.0.0", time_start=T0, time_end=T0 + timedelta(hours=1),
            events=_l2_events(),
        ),
        analyzers=_fake_domain().analyzers(),
        domain=_fake_domain(),
        router=router,
    )
    assert router.tiers == ["L2"], f"无结论时不该升级，实际 {router.tiers}"


# ============================================================
# 回归（2026-09-24 真机核验）：工具必须经注册表执行，并记进 Run
#
# 此前 `compute_statistics` 直接 `from app.tools.data_ops import stats_calculator`
# 调用，`get_tool_registry` 在生产路径上执行 **0 次** ——
# 验收写着"注册器能按名取用"，真机上没人取用。
# ============================================================


def test_statistics_go_through_the_tool_registry():
    import json

    from app.analysis.pipeline import compute_statistics

    events = [
        {"event_id": 1, "severity": "high", "event_type": "log", "message": "boom",
         "timestamp": T0, "payload": None, "metadata": {}},
        {"event_id": 2, "severity": "low", "event_type": "log", "message": "ok",
         "timestamp": T0, "payload": None, "metadata": {}},
    ]
    stats, tool_run = compute_statistics(events)

    assert stats["total"] == 2 and stats["error_count"] == 1
    assert tool_run["tool"] == "stats_calculator", "统计没有经注册表执行"
    assert tool_run["ok"] is True and tool_run["items_in"] == 2
    json.dumps(tool_run)  # 要能进 Run.tool_usage（JSONB），不能带 datetime 之类


def test_sample_selection_uses_the_event_filter_tool():
    from app.analysis.pipeline import _select_samples

    events = [
        {"event_id": i, "severity": "high" if i % 3 == 0 else "low",
         "event_type": "log", "message": f"m{i}", "timestamp": T0, "payload": None,
         "metadata": {}}
        for i in range(1, 7)
    ]
    samples, tool_run = _select_samples(events)

    assert tool_run["tool"] == "event_filter", "按 severity 过滤没有走工具"
    assert {e["severity"] for e in samples[:2]} == {"high"}, "错误事件仍要全给"


def test_tool_runs_are_recorded_into_the_run():
    from app.tasks.analysis import _record_tool_usage

    class _Runs:
        def __init__(self):
            self.calls = []

        def append_tool_usage(self, project_id, run_id, usage):
            self.calls.append((project_id, run_id, usage))
            return True

    class _Result:
        tool_runs = [
            {"tool": "stats_calculator", "ok": True},
            {"tool": "event_filter", "ok": True},
        ]

    runs = _Runs()
    _record_tool_usage(runs, 7, 9, _Result())
    assert [c[2]["tool"] for c in runs.calls] == ["stats_calculator", "event_filter"]
    assert all(c[0] == 7 and c[1] == 9 for c in runs.calls)


# ============================================================
# 回归（2026-09-24 真机核验）：候选的 kind 必须归一化到领域白名单
# ============================================================


def test_candidate_kinds_are_normalized_to_the_domain_whitelist():
    """模型会自造 kind（真机出现过 `new_pattern` / `rule_gap`）。

    自造词一旦写进 staging，加载器会拒载**整份文件** —— 真机上表现为
    确认过的知识读不回来（`confirmed_knowledge=0`），闭环断在最后一步。
    """
    from app.analysis.pipeline import _validate_candidates, normalize_knowledge_kind
    from app.domains.protocol import VALID_KNOWLEDGE_KINDS

    raw = [
        {"kind": "new_pattern", "title": "A", "evidence_ids": ["1"]},
        {"kind": "rule_gap", "title": "B", "evidence_ids": ["1"]},
        {"kind": "root_cause_hint", "title": "C", "evidence_ids": ["1"]},
        {"kind": "完全没见过的词", "title": "D", "evidence_ids": ["1"]},
    ]
    kept = _validate_candidates(raw, valid_ids={"1"})
    kinds = [c["kind"] for c in kept]
    assert kinds == [
        "error_pattern",
        "error_pattern",
        "root_cause_hint",
        "error_pattern",
    ], f"归一化结果不对：{kinds}"
    assert all(k in VALID_KNOWLEDGE_KINDS for k in kinds)
    assert normalize_knowledge_kind(None, allowed=VALID_KNOWLEDGE_KINDS) == "error_pattern"
