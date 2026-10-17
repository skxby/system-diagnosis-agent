"""上传管道（计划第 509–520 行）。

```text
Upload
  → 读取到内存（流式分块，限制大小）
  → 脱敏（正则替换，发生在写磁盘之前）      ← 红线 5
  → 保存「脱敏后」文件到 uploads/（原始文件不持久化）
  → Parser（TXT / JSONL）
  → Domain.normalize
  → 批量写入 Event
  → 返回解析统计（总行 / 成功 / 坏行 / 脱敏计数）
```

**安全顺序不可颠倒**：本模块里脱敏永远在 `write_text` 之前，且没有任何路径
会先把原文写到磁盘。这是红线 5 的实现位置，改动时请保持这个顺序可一眼看出。

本模块刻意**不依赖 FastAPI**：接收 bytes/str，便于直接单测；
HTTP 层（阶段 10）只负责把 UploadFile 读成 bytes 再调这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
import hashlib
from sqlalchemy import text

from sqlalchemy.orm import Session

from app.domains.registry import DomainRegistry, get_domain_registry
from app.models import enums
from app.models.datasource import DataSource
from app.models.event import Event
from app.parsers import get_parser
from app.repositories.datasource import DataSourceRepository
from app.repositories.event import EventRepository
from app.utils.masking import Masker
from app.utils.timestamps import TimestampUnparseableError, parse_timestamp, resolve_year_for_month_day, get_zone

#: 上传大小上限（10 MiB）。超限**拒绝**并给明确提示（验收 5）。
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

#: 入库批量大小
BATCH_SIZE = 500


class UploadTooLargeError(ValueError):
    """超过大小上限 —— 明确拒绝，不截断、不静默丢弃尾部（验收 5）。"""


class UnsupportedFormatError(ValueError):
    """格式不在 V1 支持范围内。"""


@dataclass
class UploadResult:
    """上传结果。计划第 519 行要求含解析统计。"""

    data_source_id: int
    created_data_source: bool
    format: str
    stored_filename: str
    events_persisted: int
    parse_stats: dict[str, Any]
    mask_stats: dict[str, Any]
    #: 脱敏后的文本（供测试与预览；调用方不应把它当原文缓存）
    masked_text: str = field(repr=False, default="")

    def as_dict(self) -> dict[str, Any]:
        return {
            "data_source_id": self.data_source_id,
            "created_data_source": self.created_data_source,
            "format": self.format,
            "stored_filename": self.stored_filename,
            "events_persisted": self.events_persisted,
            "parse": self.parse_stats,
            "mask": self.mask_stats,
        }


def mask_options_from_datasource(data_source: DataSource) -> dict[str, Any]:
    """从 `DataSource.metadata` 读脱敏选项（计划第 560 行）。

    V1 支持的键：
      `mask_ip`        —— 是否脱敏 IP（内网排障常需要，默认 False）
      `mask_disabled`  —— 明确关闭脱敏（仅供可控场景；默认 False）
      `no_mask_fields` —— 声明不脱敏的字段名，**V1 仅记录不实现**（见下方说明）
    """
    meta = data_source.meta or {}
    return {
        "mask_ip": bool(meta.get("mask_ip", False)),
        "enabled": not bool(meta.get("mask_disabled", False)),
        "no_mask_fields": tuple(meta.get("no_mask_fields", ()) or ()),
    }


def _json_safe_meta(event: Any) -> dict[str, Any] | None:
    """为 Event.metadata 抽出 JSON 安全的溯源信息。

    **不要直接把 `event.raw` 塞进 JSONB 列**：原始字段里含 `datetime`
    （领域解析出来的 ts）与任意嵌套对象，`json.dumps` 会直接抛
    `TypeError: Object of type datetime is not JSON serializable`。
    而且把整份原始字段都存进每一行事件，也会让表无谓膨胀。

    这里只保留「定位这条事件来自哪里」的最小信息。
    """
    raw = getattr(event, "raw", None)
    if not isinstance(raw, dict):
        return None

    meta: dict[str, Any] = {}
    for key in ("format", "host", "proc", "pid", "level", "thread", "extra"):
        value = raw.get(key)
        if value is not None and isinstance(value, (str, int, float, bool)):
            meta[key] = value

    # JSONL 的原始对象：先确认可序列化再放，避免把整个管道打挂
    raw_json = raw.get("raw_json")
    if isinstance(raw_json, dict):
        try:
            import json

            json.dumps(raw_json)
        except (TypeError, ValueError):
            meta["raw_json_unserializable"] = True
        else:
            meta["raw_json"] = raw_json

    return meta or None


class UploadService:
    """把「上传 → 脱敏 → 落盘 → 解析 → 入库」串起来。"""

    def __init__(
        self,
        session: Session,
        *,
        uploads_dir: Path,
        domain_registry: DomainRegistry | None = None,
        domain_id: str | None = None,
        domain_version: str | None = None,
        max_upload_bytes: int = MAX_UPLOAD_BYTES,
        default_timezone: str | None = None,
    ) -> None:
        self.session = session
        self.uploads_dir = Path(uploads_dir)
        self.registry = domain_registry or get_domain_registry()
        from app.config import get_settings
        self.domain = self.registry.load(domain_id or get_settings().default_domain_id, domain_version)
        self.max_upload_bytes = max_upload_bytes
        self.default_timezone = default_timezone or get_settings().default_timezone

    # ---------- 主流程 ----------

    def ingest(
        self,
        *,
        project_id: int,
        content: bytes | str,
        filename: str,
        fmt: str,
        data_source_id: int | None = None,
        mask_ip: bool | None = None,
        now: datetime | None = None,
    ) -> UploadResult:
        raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
        self._enforce_size_limit(raw_bytes)
        # 同项目同格式的首次上传也要串行，避免并发创建两个来源绕过内容去重。
        lock_key = int.from_bytes(hashlib.sha256(f"upload:{project_id}:{fmt}".encode()).digest()[:8],
                                  "big", signed=True)
        self.session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key})

        data_source, created = self._resolve_data_source(
            project_id=project_id, fmt=fmt, filename="已脱敏上传文件", data_source_id=data_source_id
        )
        options = mask_options_from_datasource(data_source)
        if mask_ip is not None:
            options["mask_ip"] = mask_ip
        if not options["enabled"]:
            raise ValueError("上传不允许关闭脱敏；请移除数据源的 mask_disabled 配置")
        self.session.refresh(data_source, with_for_update=True)
        digest = hashlib.sha256(raw_bytes + f"\0{fmt}\0{options['mask_ip']}".encode()).hexdigest()
        uploads = dict((data_source.meta or {}).get("uploads") or {})
        if digest in uploads:
            cached = uploads[digest]
            return UploadResult(data_source_id=data_source.id, created_data_source=False,
                format=fmt, stored_filename=cached["stored_filename"], events_persisted=0,
                parse_stats={**cached["parse"], "duplicate": True}, mask_stats=cached["mask"])

        # ① 解码（原始内容只存在于内存）
        # ② 脱敏 —— 必须发生在这里，即任何写盘之前
        masker = Masker(
            mask_ip=options["mask_ip"],
            enabled=options["enabled"],
        )
        decoded, encoding = self._decode_details(raw_bytes)
        masked_text = masker.mask(decoded)

        # ③ 落盘：写的是脱敏后文本，原文永不落盘（红线 5）
        stored_path = self._store_masked(masked_text, filename, project_id)

        # ④ 解析
        parser = get_parser(fmt)
        records = parser.parse(masked_text, self.domain)

        # ⑤ 时间戳归一 + ⑥ 批量入库
        persisted, ts_bad = self._persist(project_id, data_source.id, records, now=now)

        parse_stats = parser.as_dict()
        parse_stats["encoding"] = encoding
        parse_stats["mask_ip"] = options["mask_ip"]
        # 时间戳归一失败的行也要如实计入坏行（红线 4）
        if ts_bad:
            parse_stats["bad_lines"] += ts_bad
            parse_stats["bad_line_samples"] = (
                parse_stats.get("bad_line_samples", []) + [f"<{ts_bad} 条时间戳无法解析>"]
            )[:20]
            denominator = parse_stats["parsed"] + parse_stats["bad_lines"]
            parse_stats["bad_line_rate"] = (
                round(parse_stats["bad_lines"] / denominator, 4) if denominator else 0.0
            )

        uploads[digest] = {"stored_filename": stored_path.name,
                           "parse": parse_stats, "mask": masker.stats.as_dict()}
        data_source.meta = {**(data_source.meta or {}), "uploads": uploads}
        return UploadResult(
            data_source_id=data_source.id,
            created_data_source=created,
            format=fmt,
            stored_filename=stored_path.name,
            events_persisted=persisted,
            parse_stats=parse_stats,
            mask_stats=masker.stats.as_dict(),
            masked_text=masked_text,
        )

    # ---------- 内部步骤 ----------

    def _enforce_size_limit(self, raw_bytes: bytes) -> None:
        if len(raw_bytes) > self.max_upload_bytes:
            raise UploadTooLargeError(
                f"上传大小 {len(raw_bytes)} 字节超过上限 {self.max_upload_bytes} 字节"
                f"（{self.max_upload_bytes // (1024 * 1024)} MiB）；请拆分后再上传"
            )

    @staticmethod
    def _decode(raw_bytes: bytes) -> str:
        """严格解码 UTF-8/BOM 或 GB18030，无法解码时拒绝上传。"""
        return UploadService._decode_details(raw_bytes)[0]

    @staticmethod
    def _decode_details(raw_bytes: bytes) -> tuple[str, str]:
        if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
            return raw_bytes.decode("utf-16"), "utf-16"
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return raw_bytes.decode(encoding), encoding
            except UnicodeDecodeError:
                continue
        raise ValueError("无法解码日志，请转换为 UTF-8 后上传")

    def _resolve_data_source(
        self, *, project_id: int, fmt: str, filename: str, data_source_id: int | None
    ) -> tuple[DataSource, bool]:
        """按计划第 522–525 行的简化口径解析/创建 DataSource。"""
        sources = DataSourceRepository(self.session)

        if data_source_id is not None:
            existing = sources.get(project_id, data_source_id)
            if existing is None:
                # 不属于本项目或不存在：明确报错，不退化成「新建一个」
                raise ValueError(
                    f"data_source_id={data_source_id} 不存在或不属于 project {project_id}"
                )
            return existing, False

        reuse = sources.find_one_by_format(project_id, fmt)
        if reuse is not None:
            return reuse, False

        created = sources.add(
            project_id,
            DataSource(
                project_id=project_id,
                type="file_upload",
                format=fmt,
                location=filename,
                meta={},
            ),
        )
        self.session.flush()
        return created, True

    def _store_masked(self, masked_text: str, filename: str, project_id: int) -> Path:
        """保存脱敏后文件。

        目录结构 uploads/<project_id>/；存储名使用时间戳与 UUID，不保留原始文件名。
        """
        target_dir = self.uploads_dir / str(project_id)
        target_dir.mkdir(parents=True, exist_ok=True)

        # 时间戳前缀避免同名覆盖，同时保留可读性
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        path = target_dir / f"{stamp}_{uuid4().hex}.log"

        # 顺序关键：这里写入的已经是 masked_text
        path.write_bytes(masked_text.encode("utf-8"))
        return path

    def _persist(
        self,
        project_id: int,
        source_id: int,
        records: list[Any],
        *,
        now: datetime | None = None,
    ) -> tuple[int, int]:
        """批量写入 Event。返回 (写入条数, 时间戳坏行数)。"""
        events = EventRepository(self.session)
        persisted = 0
        timestamp_bad = 0
        batch: list[Event] = []

        for record in records:
            event: Any = record.event
            try:
                raw_timestamp = event.timestamp
                raw = getattr(event, "raw", None) or {}
                if isinstance(raw_timestamp, datetime) and raw.get("ts_has_year") is False:
                    year = resolve_year_for_month_day(raw_timestamp.month, raw_timestamp.day,
                        now=(now or datetime.now(timezone.utc)).astimezone(get_zone(self.default_timezone)))
                    raw_timestamp = raw_timestamp.replace(year=year)
                ts = parse_timestamp(
                    raw_timestamp, default_timezone=self.default_timezone, now=now
                )
            except (TimestampUnparseableError, ValueError):
                # 不静默用 now() 替代（计划第 540 行）
                timestamp_bad += 1
                continue

            batch.append(
                Event(
                    project_id=project_id,
                    source_id=source_id,
                    timestamp=ts,
                    event_type=event.event_type,
                    severity=event.severity,
                    message=event.message,
                    payload=event.payload,
                    meta=_json_safe_meta(event),
                )
            )
            if len(batch) >= BATCH_SIZE:
                events.session.add_all(batch)
                self.session.flush()
                persisted += len(batch)
                batch = []

        if batch:
            events.session.add_all(batch)
            self.session.flush()
            persisted += len(batch)

        return persisted, timestamp_bad


def supported_formats() -> tuple[str, ...]:
    """V1 支持的格式。与 `enums.DATA_SOURCE_FORMATS` 保持一致。"""
    return enums.DATA_SOURCE_FORMATS
