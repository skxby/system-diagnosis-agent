"""时间戳归一（计划第 534–541 行的硬规则）。

```text
统一存储 timezone-aware UTC。
syslog 缺年份 → 按当前年补（跨年边界需留意）；
缺时区       → 按 DEFAULT_TIMEZONE 解析后转 UTC；
无法解析     → 计入坏行，**不静默用当前时间替代**。
```

最后一条是红线 4 的直接体现：用 `now()` 兜底会让时间线分析建立在假数据上，
而且这种错误在报告里看起来完全正常。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re

MONTHS = {name: f"{index:02}" for index, name in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}

#: 判断「跨年」的容差：把当前年套到 (月,日) 上，若比现在晚超过这么多天，
#: 认为它其实属于上一年（例：1 月读 12 月 31 日的日志）。
YEAR_ROLLOVER_TOLERANCE_DAYS = 30


class TimestampUnparseableError(ValueError):
    """时间戳无法解析 —— 调用方应把它计入坏行，而不是替换成现在。"""


def get_zone(name: str) -> ZoneInfo:
    """取时区。名字非法时抛错而不是静默退回 UTC。"""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise TimestampUnparseableError(f"未知时区 {name!r}: {exc}") from exc


def resolve_year_for_month_day(
    month: int,
    day: int,
    *,
    now: datetime,
    tolerance_days: int = YEAR_ROLLOVER_TOLERANCE_DAYS,
) -> int:
    """给缺年份的 syslog 时间戳补年份，并处理跨年边界（计划第 538 行）。

    规则：先套当前年；若得到的日期比 `now` 晚超过 `tolerance_days` 天，
    说明它属于上一年。这样「1 月读 12 月日志」不会被错放到未来。
    """
    candidate = datetime(now.year, month, day, tzinfo=timezone.utc)
    if candidate - now > timedelta(days=tolerance_days):
        return now.year - 1
    return now.year


def to_utc(dt: datetime, *, default_timezone: str = "Asia/Shanghai") -> datetime:
    """把 datetime 统一成 timezone-aware UTC。

    naive 时间按 `default_timezone` 解释（计划第 539 行：缺时区按 .env 的
    DEFAULT_TIMEZONE 解析后转 UTC）；aware 时间直接换算。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=get_zone(default_timezone))
    return dt.astimezone(timezone.utc)


#: 供应商的高峰时段（本地时区的 weekday + [起, 止) 小时）。
#: 默认对应 DeepSeek 的定价口径：工作日 9:00–12:00、14:00–18:00（北京时间）
#: —— 这正是 `app/config.py` 里 `model_peak_price_multiplier` 注释写明的窗口。
#: 放在这里而不是写死在计价函数里：窗口是**策略**，换供应商就该只改这一处。
DEFAULT_PEAK_WINDOWS: tuple[tuple[int, int], ...] = ((9, 12), (14, 18))


def is_peak_time(
    moment: datetime,
    *,
    default_timezone: str = "Asia/Shanghai",
    windows: tuple[tuple[int, int], ...] = DEFAULT_PEAK_WINDOWS,
) -> bool:
    """`moment` 是否落在高峰时段（按本地时区的工作日与小时区间判断）。

    为什么要转本地时区：供应商的峰谷是按它自己的时区定义的，
    拿 UTC 直接比小时会把 9 点看成凌晨 1 点，峰谷判断整个错位 ——
    而算错的方向可能是**少算钱**（预算闸门就跟着失效）。
    """
    local = to_utc(moment, default_timezone=default_timezone).astimezone(
        get_zone(default_timezone)
    )
    if local.weekday() >= 5:  # 周六 / 周日不算高峰
        return False
    return any(start <= local.hour < end for start, end in windows)


def parse_timestamp(
    raw: str | datetime | None,
    *,
    default_timezone: str = "Asia/Shanghai",
    now: datetime | None = None,
) -> datetime:
    """把各种输入解析成 UTC datetime。

    支持 datetime（直接归一）与常见字符串形态；无法解析时抛
    `TimestampUnparseableError`，**绝不返回 now()**。
    """
    if isinstance(raw, datetime):
        return to_utc(raw, default_timezone=default_timezone)

    if raw is None:
        raise TimestampUnparseableError("时间戳为空")

    text = str(raw).strip()
    text = re.sub(r"\b(?:" + "|".join(MONTHS) + r")\b",
                  lambda match: MONTHS[match.group()], text)
    if not text:
        raise TimestampUnparseableError("时间戳为空字符串")

    # 带时区偏移的数字形态，如 `2015-07-29T17:41:44+08:00`
    try:
        return to_utc(datetime.fromisoformat(text), default_timezone=default_timezone)
    except ValueError:
        pass

    formats = (
        "%Y-%m-%d %H:%M:%S,%f",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%d/%m/%Y:%H:%M:%S %z",  # 与进程 locale 无关
        "%d/%m/%Y:%H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
    )
    for fmt in formats:
        try:

            # DEFAULT_TIMEZONE 赋予时区再转 UTC。直接在此假定 UTC 会让该配置失效。
            parsed = datetime.strptime(text, fmt)  # noqa: DTZ007
        except ValueError:
            continue
        return to_utc(parsed, default_timezone=default_timezone)

    # 缺年份的形态（syslog）单独处理。
    # **不要把 `%b %d %H:%M:%S` 交给 strptime**：CPython 已就「不含年份的日期」
    # 发出 DeprecationWarning，Python 3.15 会改变行为（可能直接抛错）。
    # 项目锁定 3.13，但升级时会静默炸掉，所以这里显式补年份后再解析。
    parsed = _parse_yearless(text, now=now)
    if parsed is not None:
        return to_utc(parsed, default_timezone=default_timezone)

    raise TimestampUnparseableError(f"无法解析的时间戳: {text!r}")


#: syslog 常见的两种写法（`Jul  1` 是双空格）
_YEARLESS_FORMATS = ("%m %d %H:%M:%S", "%m  %d %H:%M:%S")


def _parse_yearless(text: str, *, now: datetime | None) -> datetime | None:
    """解析不含年份的时间戳，并补上合理年份（含跨年处理）。"""
    for fmt in _YEARLESS_FORMATS:
        try:
            # 先借一个闰年的年份把「月日时分秒」解出来，避免 2 月 29 日在非闰年解析失败
            probe = datetime.strptime(f"2000 {text}", f"%Y {fmt}")  # noqa: DTZ007 - 同上，仅用于提取月日
        except ValueError:
            continue
        reference = now or datetime.now(timezone.utc)
        try:
            year = resolve_year_for_month_day(probe.month, probe.day, now=reference)
            return datetime(  # noqa: DTZ001
                year, probe.month, probe.day, probe.hour, probe.minute, probe.second
            )
        except ValueError:
            return None  # 该年不存在这一天（如非闰年的 2/29）
    return None
