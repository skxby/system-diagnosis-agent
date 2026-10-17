"""把 Pipeline 产出落库（计划第 751 行：Insight 落库）。

**红线 3 在落库这一侧同样成立**：`Evidence.event_ids` 必须 ⊆ 本次有效
`event_id`。上行（`evidence.py`）已经校验过一遍，这里在做写入前**再断言一次**——
因为这是最后一道闸门，一旦脏数据进库，报告页面就再也分不清"有证据"和"没证据"了。

Project 隔离经 repository：`Insight` 带 `project_id`，`Evidence` 通过
`insight_id` 归属（它没有 `project_id` 列，见阶段 02 的表结构）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.analysis.evidence import ValidatedInsight
from app.models.enums import INSIGHT_TYPE_FACT
from app.models.evidence import Evidence
from app.models.insight import Insight


class EvidencePersistenceError(ValueError):
    """落库前的最后一道证据校验没过 —— 拒绝写入，不静默放过。"""


@dataclass
class PersistenceResult:
    """落库结果。"""

    insight_ids: list[int] = field(default_factory=list)
    evidence_ids: list[int] = field(default_factory=list)
    #: 因证据越界被拒绝的（不应发生；发生即为上游漏检）
    rejected: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "insight_count": len(self.insight_ids),
            "evidence_count": len(self.evidence_ids),
            "rejected": list(self.rejected),
        }


@dataclass
class GroupingResult:
    """分组与事故的落库结果。"""

    group_ids: list[int] = field(default_factory=list)
    incident_ids: list[int] = field(default_factory=list)
    groups_created: int = 0
    incidents_created: int = 0
    incidents_reused: int = 0
    events_grouped: int = 0
    events_linked_to_incident: int = 0
    #: event_id → incident_id，供结论回填 `Insight.incident_id`
    incident_by_event: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_count": len(self.group_ids),
            "incident_count": len(self.incident_ids),
            "groups_created": self.groups_created,
            "incidents_created": self.incidents_created,
            "incidents_reused": self.incidents_reused,
            "events_grouped": self.events_grouped,
            "events_linked_to_incident": self.events_linked_to_incident,
        }


def incident_title(signature: str, *, groups: int, events: int) -> str:
    """构造事故标题：**签名放在最前面的方括号里**。

    冻结表里 Incident 没有 `signature` 列（计划第 325–327 行只给了
    title / severity / group_ids / root_cause / 时间），而
    `merge_into_incidents(existing_incidents=...)` 判断"这次的与历史的是不是同一类"
    必须按签名比。V1 用标题前缀承载它 —— **读写各一处**
    （本函数与 `signature_from_title`），改格式必须同时改这两处并跑测试。

    为什么不在 Incident 上加一列：那要动迁移与 9 表边界；
    而"签名"本身就是分组的派生物，能派生就不新增真源。
    """
    return f"[{signature}] {groups} 个分组 / {events} 条事件"


def signature_from_title(title: str) -> str | None:
    """从标题前缀取回签名；不符合约定格式时返回 `None`（不猜）。"""
    text = str(title or "")
    if not text.startswith("["):
        return None
    end = text.find("]")
    return text[1:end] if end > 1 else None


def persist_grouping(
    *,
    project_id: int,
    run_id: int,
    groups: list[dict[str, Any]],
    incidents: list[dict[str, Any]],
    group_repository: Any,
    incident_repository: Any,
    event_repository: Any,
) -> GroupingResult:
    """把分组与事故落库，并回填 `Event.group_id` / `Event.incident_id`。

    为什么必须有这一步：分组与归并此前**只发生在内存里**（喂 Context 用），
    真机上 177 条 Run 跑完，`event_groups` 与 `incidents` 两张表始终是 0 行 ——
    于是"事故记忆库"永远是空的，"历史相似事故注入 Context"也无从谈起。
    `create_group` / `create_incident` 两个方法写好了、单测也全绿，就是没有生产调用方。

    分组的唯一键是 `(project, source, group_key)`（冻结表的唯一约束），
    所以同一个模板簇在后续 Run 里会**复用**同一行，只更新计数与时间跨度；
    成员关系仍以 `Event.group_id` 为真源，不存反向数组。
    """
    result = GroupingResult()
    db_group_ids: list[int] = []

    for group in groups:
        source_id = int(group.get("source_id") or 0)
        group_key = str(group.get("group_key") or "")
        if not source_id or not group_key:
            # 缺 key 的分组无法与唯一约束对齐：跳过而不是写一行"看起来对"的脏数据
            db_group_ids.append(0)
            continue

        existing = group_repository.find_by_key(project_id, source_id, group_key)
        if existing is None:
            row = group_repository.create_group(
                project_id,
                source_id=source_id,
                group_key=group_key,
                template=group.get("template"),
                time_start=group.get("time_start"),
                time_end=group.get("time_end"),
            )
            group_repository.session.flush()
            result.groups_created += 1
        else:
            row = existing
            # 复用已有分组：只推进时间跨度与模板，计数按本次重算
            if group.get("time_start") and (
                row.time_start is None or group["time_start"] < row.time_start
            ):
                row.time_start = group["time_start"]
            if group.get("time_end") and (
                row.time_end is None or group["time_end"] > row.time_end
            ):
                row.time_end = group["time_end"]
            if not row.template and group.get("template"):
                row.template = group.get("template")

        event_ids = [int(e) for e in (group.get("event_ids") or [])]
        group_repository.set_event_count(project_id, int(row.id), len(event_ids))
        result.events_grouped += event_repository.assign_group(
            project_id, event_ids, int(row.id)
        )
        db_group_ids.append(int(row.id))

    result.group_ids = [g for g in db_group_ids if g]

    for incident in incidents:
        index_ids = [int(i) for i in (incident.get("group_ids") or [])]
        member_ids = [
            db_group_ids[i]
            for i in index_ids
            if 0 <= i < len(db_group_ids) and db_group_ids[i]
        ]
        if not member_ids:
            continue

        reused_id = incident.get("reused_incident_id")
        if reused_id:
            # 与历史事故同类且时间窗连续 → 并入它，而不是新开一条
            row = incident_repository.get(project_id, int(reused_id))
            if row is not None:
                merged = list(dict.fromkeys([*[int(g) for g in (row.group_ids or [])], *member_ids]))
                row.group_ids = merged
                if incident.get("time_end") and (
                    row.time_end is None or incident["time_end"] > row.time_end
                ):
                    row.time_end = incident["time_end"]
                result.incidents_reused += 1
                result.incident_ids.append(int(row.id))
                member_ids = merged
            else:
                reused_id = None
        if not reused_id:
            row = incident_repository.create_incident(
                project_id,
                title=incident_title(
                    str(incident.get("signature") or "unclassified"),
                    groups=len(member_ids),
                    events=sum(
                        len(groups[i].get("event_ids") or [])
                        for i in index_ids
                        if 0 <= i < len(groups)
                    ),
                ),
                severity=str(incident.get("severity") or "low"),
                group_ids=member_ids,
                time_start=incident.get("time_start"),
                time_end=incident.get("time_end"),
                source_run_id=run_id,
            )
            incident_repository.session.flush()
            result.incidents_created += 1
            result.incident_ids.append(int(row.id))

        touched = event_repository.assign_incident(project_id, member_ids, int(row.id))
        result.events_linked_to_incident += touched
        for event_id in event_repository.events_in_groups(project_id, member_ids):
            result.incident_by_event[str(event_id)] = int(row.id)

    return result


def persist_insights(
    *,
    project_id: int,
    run_id: int,
    insights: list[ValidatedInsight],
    valid_event_ids: set[str] | list[str],
    insight_repository: Any,
    evidence_repository: Any,
    source_id: int | None = None,
    incident_id_by_title: dict[str, int] | None = None,
    incident_by_event: dict[str, int] | None = None,
    timestamps_by_event: dict[str, Any] | None = None,
) -> PersistenceResult:
    """把校验后的 Insight 与其 Evidence 写入数据库。

    `valid_event_ids` 是本次分析的有效集合；任何越界的 evidence 都会被拒绝
    该条 Insight（而不是悄悄裁掉写进去）——到了这一层还越界，说明上游有 bug，
    应当让它在测试里暴露，而不是被"容错"掩盖。

    `incident_by_event`：`event_id → incident_id`。结论与事故的关联**按证据事件**
    判定（证据落在哪个事故的成员事件里，就归属哪个事故），
    而不是按标题字符串匹配 —— 模型给的标题每次都可能不一样，
    按标题关联等于把"关联关系"交给运气。
    """
    valid = {str(v) for v in valid_event_ids}
    result = PersistenceResult()
    incident_map = incident_id_by_title or {}
    event_incident = incident_by_event or {}

    for item in insights:
        evidence_ids = [str(e) for e in item.evidence_ids]
        invalid = [e for e in evidence_ids if e not in valid]

        # 落库前的最后一道闸门
        if invalid:
            result.rejected.append(
                f"{item.title}：evidence_ids 越界 {invalid}（上游校验漏检）"
            )
            continue
        if item.type == INSIGHT_TYPE_FACT and not evidence_ids:
            result.rejected.append(f"{item.title}：fact 缺少有效证据（上游校验漏检）")
            continue

        insight = Insight(
            project_id=project_id,
            run_id=run_id,
            incident_id=(
                incident_map.get(item.title)
                or next(
                    (event_incident[e] for e in evidence_ids if e in event_incident),
                    None,
                )
            ),
            type=item.type,
            severity=item.severity,
            confidence=item.confidence,
            title=item.title,
            summary=item.summary,
            reasoning=item.reasoning,
            limitations=item.limitations,
            # extras 只放 JSON 安全的确定性诊断信息（如 runbook 快照、命中的 analyzer）
            run_metadata=dict(item.extras) if item.extras else None,
        )
        insight_repository.add(project_id, insight)
        # 需要 id 才能挂 Evidence，故先 flush 拿到自增 id
        insight_repository.session.flush()
        result.insight_ids.append(int(insight.id))

        if evidence_ids:
            timestamps = [timestamps_by_event[e] for e in evidence_ids
                          if timestamps_by_event and e in timestamps_by_event]
            evidence = Evidence(
                insight_id=insight.id,
                source_id=source_id,
                event_ids=evidence_ids,
                time_range={"start": min(timestamps).isoformat(), "end": max(timestamps).isoformat()} if timestamps else None,
                calculation=f"直接引用 {len(evidence_ids)} 条已校验事件；不构成根因证明",
                description=f"支撑「{item.title}」的事件",
            )
            evidence_repository.add(project_id, evidence)
            evidence_repository.session.flush()
            result.evidence_ids.append(int(evidence.id))

    return result


def assert_no_evidence_violation(result: PersistenceResult) -> None:
    """把"上游漏检"变成显式失败。

    测试与生产路径都调它：如果被拒绝的条数不为 0，说明红线 3 在上游被绕过，
    必须让这件事响亮地暴露，而不是留在返回值的角落里（红线 4）。
    """
    if result.rejected:
        raise EvidencePersistenceError(
            "落库前证据校验失败：" + "；".join(result.rejected)
        )
