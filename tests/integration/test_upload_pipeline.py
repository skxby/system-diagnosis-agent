"""阶段 04 验收的集成测试（真实 PostgreSQL）。

逐条对应计划第 563–568 行的验收：
  1. 上传 TXT / JSONL 能解析为事件入库，响应含解析统计
  2. 含邮箱 / 密钥的内容在**落盘文件和数据库**中都已被替换；磁盘上找不到原文
  3. 坏行 / 无法识别的时间戳被计数并明确报告，不静默丢弃
  4. 未带 data_source_id 的上传能自动建 / 复用 DataSource
  5. 超过大小上限的上传被拒绝并给出明确提示
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.db import SessionLocal
from app.models import DataSource, Project, User
from app.repositories import (
    DataSourceRepository,
    EventRepository,
    ProjectRepository,
    UserRepository,
)
from app.services import MAX_UPLOAD_BYTES, UploadService, UploadTooLargeError

pytestmark = pytest.mark.integration
UTC = timezone.utc


@pytest.fixture()
def session():
    s = SessionLocal()
    try:
        yield s
        s.rollback()
    finally:
        s.close()


@pytest.fixture()
def project(session):
    users = UserRepository(session)
    user = users.add(
        User(email=f"stage04-{datetime.now(UTC).timestamp()}@example.com", password_hash="x")
    )
    session.flush()
    projects = ProjectRepository(session)
    proj = projects.add(Project(user_id=user.id, name="p04", budget_total=10, budget_used=0))
    session.flush()
    return proj


@pytest.fixture()
def service(session, project, work_tmp) -> UploadService:
    return UploadService(session, uploads_dir=work_tmp / "uploads")


# 含敏感值的合成 TXT。刻意不用 logs/ 下的样本：
# ① 那里没有密钥样本；② 不该为了测试去改动真实样本（AGENTS.md 禁止事项）。
TXT_WITH_SECRETS = """\
Jun 14 15:16:01 combo sshd(pam_unix)[19939]: authentication failure; rhost=218.188.2.4 user=root
Jun 14 15:16:02 combo kernel: contact admin at ops@example.com for help
Jun 14 15:16:03 combo app[100]: starting with api_key=sk-abcdef0123456789abcdef
Jun 14 15:16:04 combo app[100]: loaded token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789
Jun 14 15:16:05 combo app[100]: disk usage 91%
Jun 14 15:16:06 combo app[100]: Unhandled exception in worker
    at com.example.Foo.bar(Foo.java:42)
    at com.example.Baz.qux(Baz.java:99)
Caused by: java.lang.NullPointerException
    at com.example.Foo.bar(Foo.java:40)

this is a completely unparseable line %%%
"""

JSONL_CONTENT = """\
{"time": "17/May/2015:08:05:32 +0000", "remote_ip": "93.180.71.3", "request": "GET /a HTTP/1.1", "response": 304}
{"time": "17/May/2015:08:05:33 +0000", "remote_ip": "93.180.71.4", "request": "GET /b HTTP/1.1", "response": 200}
{"time": "17/May/2015:08:05:34 +0000", "remote_ip": "93.180.71.5", "request": "GET /c HTTP/1.1", "response": 500}
{ this is not valid json }
{"remote_ip": "93.180.71.6", "request": "GET /d HTTP/1.1"}
"""


# ============================================================
# 验收 1：TXT / JSONL 能解析入库，响应含统计
# ============================================================


def test_upload_txt_persists_events_and_returns_stats(service, project, session):
    result = service.ingest(
        project_id=project.id,
        content=TXT_WITH_SECRETS,
        filename="syslog.txt",
        fmt="txt",
    )

    assert result.events_persisted > 0
    stats = result.parse_stats
    assert stats["total_lines"] > 0
    assert stats["parsed"] == result.events_persisted
    # 多行堆栈应被合并，而不是算成两条坏行
    assert stats["merged"] >= 2
    # 那一行 %% 必须被计为坏行并留样本（验收 3）
    assert stats["bad_lines"] >= 1
    assert stats["bad_line_samples"]

    events = EventRepository(session)
    assert events.count(project.id) == result.events_persisted


def test_upload_jsonl_persists_events_and_reports_bad_lines(service, project, session):
    result = service.ingest(
        project_id=project.id,
        content=JSONL_CONTENT,
        filename="access.jsonl",
        fmt="jsonl",
    )

    # 3 行合法 JSON 有 time 字段；`{ this is not valid json }` 与缺 time 的那行算坏行
    assert result.events_persisted == 3
    assert result.parse_stats["bad_lines"] == 2
    assert result.parse_stats["invalid_json"] == 1
    assert result.parse_stats["missing_timestamp"] == 1

    events = EventRepository(session)
    assert events.count(project.id) == 3


def test_jsonl_timestamps_are_converted_to_utc(service, project, session):
    service.ingest(
        project_id=project.id,
        content=JSONL_CONTENT,
        filename="access.jsonl",
        fmt="jsonl",
    )
    events = EventRepository(session).list_all(project.id)
    for event in events:
        assert event.timestamp.tzinfo is not None
        assert event.timestamp.utcoffset().total_seconds() == 0


# ============================================================
# 验收 2：脱敏在落盘前 —— 磁盘与数据库都找不到原文
# ============================================================


def test_secrets_are_masked_before_writing_to_disk(service, project, work_tmp):
    result = service.ingest(
        project_id=project.id,
        content=TXT_WITH_SECRETS,
        filename="syslog.txt",
        fmt="txt",
    )

    stored = work_tmp / "uploads" / str(project.id) / result.stored_filename
    assert stored.is_file()
    on_disk = stored.read_text(encoding="utf-8")

    for secret in (
        "ops@example.com",
        "sk-abcdef0123456789abcdef",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    ):
        assert secret not in on_disk, f"落盘文件里仍含原文敏感值：{secret}"

    assert "[EMAIL_" in on_disk
    assert "[SECRET_" in on_disk


def test_secrets_are_masked_before_reaching_the_database(service, project, session):
    service.ingest(
        project_id=project.id,
        content=TXT_WITH_SECRETS,
        filename="syslog.txt",
        fmt="txt",
    )

    events = EventRepository(session).list_all(project.id)
    blob = "\n".join(f"{e.message}\n{e.payload}\n{e.meta}" for e in events)

    for secret in (
        "ops@example.com",
        "sk-abcdef0123456789abcdef",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    ):
        assert secret not in blob, f"数据库事件里仍含原文敏感值：{secret}"


def test_masked_placeholder_is_consistent_across_lines(service, project, session):
    """同一邮箱出现多次必须得到同一占位符（便于跨项目关联）。"""
    content = (
        "Jun 14 15:16:01 host a[1]: mail ops@example.com sent\n"
        "Jun 14 15:16:02 host a[1]: mail ops@example.com bounced\n"
    )
    result = service.ingest(
        project_id=project.id, content=content, filename="m.txt", fmt="txt"
    )
    import re

    tokens = set(re.findall(r"\[EMAIL_[0-9a-f]{16}\]", result.masked_text))
    assert len(tokens) == 1, f"同一邮箱产生了多个占位符：{tokens}"


def test_raw_source_file_is_never_persisted(service, project, work_tmp):
    """原始文件不持久化：uploads/ 下只应有脱敏后的版本。"""
    service.ingest(
        project_id=project.id,
        content=TXT_WITH_SECRETS,
        filename="syslog.txt",
        fmt="txt",
    )
    files = list((work_tmp / "uploads").rglob("*"))
    written = [f for f in files if f.is_file()]
    assert len(written) == 1
    assert "sk-abcdef0123456789abcdef" not in written[0].read_text(encoding="utf-8")


def test_ip_masking_is_off_by_default_and_can_be_enabled(service, project, session):
    """计划第 547 行：IP 默认不脱敏（内网排障需要），可配置开关。"""
    result = service.ingest(
        project_id=project.id,
        content=TXT_WITH_SECRETS,
        filename="a.txt",
        fmt="txt",
    )
    assert "218.188.2.4" in result.masked_text, "默认不该脱敏 IP"
    assert result.mask_stats["by_rule"].get("ipv4") is None

    # 打开开关后必须脱敏
    sources = DataSourceRepository(session)
    ds = sources.get(project.id, result.data_source_id)
    ds.meta = {"mask_ip": True}
    session.flush()

    result2 = service.ingest(
        project_id=project.id,
        data_source_id=ds.id,
        content=TXT_WITH_SECRETS,
        filename="b.txt",
        fmt="txt",
    )
    assert "218.188.2.4" not in result2.masked_text
    assert result2.mask_stats["by_rule"]["ipv4"] > 0


def test_mask_stats_break_down_by_rule(service, project):
    result = service.ingest(
        project_id=project.id,
        content=TXT_WITH_SECRETS,
        filename="a.txt",
        fmt="txt",
    )
    by_rule = result.mask_stats["by_rule"]
    assert by_rule["email"] == 1
    assert result.mask_stats["total"] >= 2


# ============================================================
# 验收 3：坏行被计数并报告，不静默丢弃
# ============================================================


def test_bad_lines_are_counted_and_sampled_not_dropped_silently(service, project):
    result = service.ingest(
        project_id=project.id,
        content="Jun 14 15:16:01 host a[1]: good line\n%%% garbage %%%\n",
        filename="a.txt",
        fmt="txt",
    )
    assert result.parse_stats["bad_lines"] == 1
    samples = result.parse_stats["bad_line_samples"]
    assert any("garbage" in s for s in samples)
    assert result.parse_stats["bad_line_rate"] > 0


def test_unparseable_timestamp_is_counted_not_replaced_with_now(service, project, session):
    """计划第 540 行：完全无法解析的时间戳计入坏行，不静默用当前时间替代。"""
    content = (
        '{"timestamp": "not-a-real-timestamp", "message": "x"}\n'
        '{"timestamp": "2026-09-23T12:00:00", "message": "ok"}\n'
    )
    result = service.ingest(
        project_id=project.id, content=content, filename="a.jsonl", fmt="jsonl"
    )
    # 时间戳无法解析的那条不进入事件表
    assert result.events_persisted == 1
    assert result.parse_stats["bad_lines"] >= 1
    assert any("时间戳无法解析" in s for s in result.parse_stats["bad_line_samples"])


def test_disk_upload_parse_rate_on_real_logs(service, project, session):
    """用 logs/ 里的真实样本跑一遍完整管道，解析率必须达标。"""
    sample = Path(__file__).resolve().parents[2] / "logs" / "Zookeeper_2k.log"
    if not sample.is_file():
        pytest.skip("缺少真实样本")

    result = service.ingest(
        project_id=project.id,
        content=sample.read_bytes(),
        filename="Zookeeper_2k.log",
        fmt="txt",
    )
    stats = result.parse_stats
    rate = stats["parsed"] / (stats["parsed"] + stats["bad_lines"])
    assert rate >= 0.99, f"真实样本解析率 {rate:.4f} 低于 0.99：{stats}"
    assert result.events_persisted == stats["parsed"]


# ============================================================
# 验收 4：未带 data_source_id 时自动建 / 复用
# ============================================================


def test_first_upload_creates_data_source(service, project):
    result = service.ingest(
        project_id=project.id, content="Jun 14 15:16:01 h a[1]: x\n", filename="a.txt", fmt="txt"
    )
    assert result.created_data_source is True
    assert result.data_source_id > 0


def test_second_upload_reuses_data_source_of_same_format(service, project, session):
    first = service.ingest(
        project_id=project.id, content="Jun 14 15:16:01 h a[1]: x\n", filename="a.txt", fmt="txt"
    )
    second = service.ingest(
        project_id=project.id, content="Jun 14 15:16:02 h a[1]: y\n", filename="b.txt", fmt="txt"
    )
    assert second.created_data_source is False
    assert second.data_source_id == first.data_source_id

    # 不同格式应各自建一个
    third = service.ingest(
        project_id=project.id,
        content='{"timestamp": "2026-09-23T12:00:00", "message": "z"}\n',
        filename="c.jsonl",
        fmt="jsonl",
    )
    assert third.created_data_source is True
    assert third.data_source_id != first.data_source_id

    assert DataSourceRepository(session).count(project.id) == 2


def test_explicit_data_source_id_of_another_project_is_rejected(service, project, session):
    users = UserRepository(session)
    other_user = users.add(
        User(email=f"other-{datetime.now(UTC).timestamp()}@example.com", password_hash="x")
    )
    session.flush()
    other_project = ProjectRepository(session).add(
        Project(user_id=other_user.id, name="other", budget_total=1, budget_used=0)
    )
    session.flush()
    foreign = DataSourceRepository(session).add(
        other_project.id,
        DataSource(project_id=other_project.id, type="file_upload", format="txt", location="x"),
    )
    session.flush()

    with pytest.raises(ValueError, match="不存在或不属于"):
        service.ingest(
            project_id=project.id,
            data_source_id=foreign.id,
            content="Jun 14 15:16:01 h a[1]: x\n",
            filename="a.txt",
            fmt="txt",
        )


# ============================================================
# 验收 5：超限被拒绝并给明确提示
# ============================================================


def test_oversized_upload_is_rejected_with_clear_message(session, project, work_tmp):
    small = UploadService(session, uploads_dir=work_tmp / "uploads", max_upload_bytes=100)
    with pytest.raises(UploadTooLargeError) as excinfo:
        small.ingest(
            project_id=project.id, content="x" * 101, filename="big.txt", fmt="txt"
        )
    message = str(excinfo.value)
    assert "超过上限" in message
    assert "101" in message


def test_default_limit_is_ten_mib():
    assert MAX_UPLOAD_BYTES == 10 * 1024 * 1024


def test_unsupported_format_is_refused(service, project):
    with pytest.raises(ValueError, match="不支持的格式"):
        service.ingest(
            project_id=project.id, content="a,b,c\n", filename="a.csv", fmt="csv"
        )


def test_path_traversal_in_filename_cannot_escape_uploads_dir(service, project, work_tmp):
    """文件名里带路径分隔符不能写到 uploads/ 之外。"""
    result = service.ingest(
        project_id=project.id,
        content="Jun 14 15:16:01 h a[1]: x\n",
        filename="../../evil.txt",
        fmt="txt",
    )
    stored = work_tmp / "uploads" / str(project.id) / result.stored_filename
    assert stored.is_file()
    assert ".." not in result.stored_filename
    assert not (work_tmp / "evil.txt").exists()
