r"""computer_monitoring 的行解析（原始行 → 原始字段）。

使用 `re`、`datetime` 与 dateutil 的英文日期解析，**不调 LLM、不猜**（红线 1）。覆盖 `logs/` 里
真实存在的四类时间戳形态：

    Jun 14 15:16:01 combo sshd(pam_unix)[19939]: authentication failure; ...
    Jun 15 04:06:20 combo logrotate: ALERT exited abnormally with [1]
    Jul  1 09:29:02 host sandboxd[129] ([31211]): com.apple...deny network-outbound
    2015-07-29 17:41:44,747 - INFO  [QuorumPeer[myid=1]/...@774] - msg
    [Sun Dec 04 04:47:44 2005] [notice] ...            (Apache)

解析不了就返回 `None`，**不抛异常、不硬凑**——坏行要能被如实统计为坏行，
而不是被塞进一个看似正常的结构里（红线 4）。

两个曾经踩过的坑（都靠真实日志实测暴露，不是推测）：
- log4j 的 `[thread]` 里**可以嵌套方括号**（`[QuorumPeer[myid=1]/...]`），
  用 `[^\]]*` 会在内层 `]` 处截断，导致这些行整条解析失败；
- syslog 的 `[pid]` **不是必需的**（`logrotate: ALERT ...` 就没有），
  把它写成强制项会让这类行全部落空。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from dateutil.parser import parse as parse_datetime

# `Jun 14 15:16:01`（`Jul  1` 是双空格）—— syslog 缺年份
_SYSLOG_TS = r"[A-Z][a-z]{2} {1,2}\d{1,2} \d{2}:\d{2}:\d{2}"
# `2015-07-29 17:41:44,747` —— Zookeeper log4j 风格，带毫秒
_LOG4J_TS = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}"
# `[Sun Dec 04 04:47:44 2005]` —— Apache error log
_APACHE_TS = r"\[[A-Z][a-z]{2} [A-Z][a-z]{2} {1,2}\d{1,2} \d{2}:\d{2}:\d{2} \d{4}\]"

# --- syslog：先切出 ts / host / 其余，再把「其余」拆成 proc / pid / msg ---
_RE_SYSLOG = re.compile(
    rf"^(?P<ts>{_SYSLOG_TS})\s+(?P<host>\S+)\s+(?P<rest>.*)$"
)
# 进程名字符集必须容得下真实日志里出现过的东西：
#   `sshd(pam_unix)`        带括号限定
#   `Microsoft Word`        含空格的多词名
#   `BezelServices 1.19`    名字里带空格与版本号
#   ` -- root`              syslog 守护进程用 ` -- ` 标记（如 cron/login）
# pid 可有可无；后面可能还有一个 `([31211])` 这样的附注组。
_RE_SYSLOG_REST = re.compile(
    r"^\s*(?P<proc>[^\[\]:]*?)"
    r"(?:\s*\[(?P<pid>\d+)\])?"
    r"(?:\s*\(\[(?P<extra>\d+)\]\))?"
    # 附注组还有**不带方括号**的形态，macOS launchd 就是：
    #   `com.apple.xpc.launchd[1] (com.apple.xpc.launchd.domain.pid.WebContent.32502): ...`
    # 早先只认 `([31211])` 这一种，于是 Mac 真实日志里这类行整批判成坏行
    # （实测 19/2000）。它们恰恰是"哪个进程崩了"的关键行，丢掉可惜。
    # 必须排在 `[pid]` 之后：`([31211])` 会被更靠前的分支优先吃掉，语义不变。
    r"(?:\s*\((?P<extra_text>[^()]+)\))?"
    r"\s*:\s?(?P<msg>.*)$"
)

# --- log4j：`[thread]` 允许嵌套方括号 ---
_RE_LOG4J = re.compile(
    rf"^(?P<ts>{_LOG4J_TS})\s+-\s+(?P<level>[A-Z]+)\s+"
    rf"\[(?P<thread>(?:[^\[\]]|\[[^\]]*\])*)\]\s+-\s+(?P<msg>.*)$"
)

# --- Apache error log ---
_RE_APACHE = re.compile(
    rf"^(?P<ts>{_APACHE_TS})\s+\[(?P<level>[a-z]+)\]\s+(?P<msg>.*)$"
)

_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

#: 日志等级词 → 本项目三档 severity
_LEVEL_TO_SEVERITY = {
    "fatal": "high",
    "critical": "high",
    "crit": "high",
    "error": "high",
    "err": "high",
    "emerg": "high",
    "alert": "high",
    "warn": "medium",
    "warning": "medium",
    "notice": "low",
    "info": "low",
    "debug": "low",
    "trace": "low",
}

#: message 出现这些词就升级为 high —— 它们几乎总是真问题
_HIGH_SEVERITY_HINTS = re.compile(
    r"\b(crash|segfault|segmentation fault|oom[-_ ]?kill|out of memory|panic|"
    r"core dumped|fatal|failed|failure|denied|timeout|refused|corrupt)\b",
    re.IGNORECASE,
)


def parse_syslog_line(
    line: str, *, assume_year: int | None = None
) -> dict[str, Any] | None:
    """解析 syslog 风格行。

    **返回 naive datetime**：syslog 时间戳既不含量份也不含时区，本函数无权
    假定它是 UTC。时区由上层按 `.env` 的 `DEFAULT_TIMEZONE` 解释后转 UTC
    （计划第 539 行），年份缺失由上层按「当前年 + 跨年容差」补（第 538 行）。

    在这里硬编码 tzinfo=UTC 会让 DEFAULT_TIMEZONE 变成死配置，
    并把所有日志时间整体偏移一个时区 —— 而且偏移后看起来完全正常。
    """
    match = _RE_SYSLOG.match(line)
    if match is None:
        return None

    parts = match.group("ts").split()
    month = _MONTHS.get(parts[0])
    if month is None:
        return None
    day = int(parts[1])
    hour, minute, second = (int(x) for x in parts[2].split(":"))

    year = assume_year if assume_year is not None else datetime.now(timezone.utc).year
    try:
        # 刻意 naive：syslog 不含时区，上层按 DEFAULT_TIMEZONE 赋予
        ts = datetime(year, month, day, hour, minute, second)  # noqa: DTZ001
    except ValueError:
        return None  # 非法日期（如 2 月 30 日）明确判为坏行

    rest = _RE_SYSLOG_REST.match(match.group("rest"))
    if rest is None:
        return None

    pid_raw = rest.group("pid")
    return {
        "ts": ts,
        "ts_has_year": False,
        "ts_has_tz": False,
        "host": match.group("host"),
        "proc": rest.group("proc"),
        "pid": int(pid_raw) if pid_raw is not None else None,
        "extra": rest.group("extra") or rest.group("extra_text"),
        "msg": rest.group("msg").strip(),
        "format": "syslog",
    }


def parse_log4j_line(line: str) -> dict[str, Any] | None:
    """解析 `2015-07-29 17:41:44,747 - INFO  [thread] - msg`。"""
    match = _RE_LOG4J.match(line)
    if match is None:
        return None
    try:
        # 同上：log4j 时间戳不带时区
        ts = datetime.strptime(match.group("ts"), "%Y-%m-%d %H:%M:%S,%f")  # noqa: DTZ007
    except ValueError:
        return None
    return {
        "ts": ts,
        "ts_has_year": True,
        "ts_has_tz": False,
        "level": match.group("level"),
        "thread": match.group("thread"),
        "msg": match.group("msg").strip(),
        "format": "log4j",
    }


def parse_apache_line(line: str) -> dict[str, Any] | None:
    """解析 `[Sun Dec 04 04:47:44 2005] [notice] msg`。"""
    match = _RE_APACHE.match(line)
    if match is None:
        return None
    try:
        # 同上：Apache 时间戳不带时区
        ts = parse_datetime(match.group("ts").strip("[]"))
    except ValueError:
        return None
    return {
        "ts": ts,
        "ts_has_year": True,
        "ts_has_tz": False,
        "level": match.group("level"),
        "msg": match.group("msg").strip(),
        "format": "apache",
    }


#: 按顺序尝试；先匹配到的赢。顺序即优先级，故意显式写出来便于审阅。
PARSERS = (parse_log4j_line, parse_syslog_line, parse_apache_line)


def parse_line(line: str) -> dict[str, Any] | None:
    """依次尝试各格式；全部失败返回 None。"""
    stripped = line.strip()
    if not stripped:
        return None
    for parser in PARSERS:
        raw = parser(stripped)
        if raw is not None:
            return raw
    return None


# ============================================================
# 指标抽取（把 message 里的 `name=value` 变成结构化指标）
# ============================================================

#: 本领域识别的指标名与其单位。与 ComputerMonitoringDomain.metric_specs() 同口径。
#: 放在解析器里是因为**抽取发生在解析阶段**，解析器不该反向依赖领域类。
METRIC_UNITS: dict[str, str] = {
    "cpu_used": "%",
    "memory_used": "%",
    "disk_used": "%",
    "load_avg": "",
}

#: 别名：不同系统对同一指标的叫法不同，统一到规范名。
_METRIC_ALIASES: dict[str, str] = {
    "cpu": "cpu_used",
    "mem": "memory_used",
    "mem_used": "memory_used",
    "memory": "memory_used",
    "disk": "disk_used",
    "load": "load_avg",
    "loadavg": "load_avg",
}

#: 匹配 `cpu_used=92.4` / `memory_used: 88%` / `disk_used = 96`
_METRIC_PAIR = re.compile(
    r"\b(?P<name>[a-z][a-z0-9_]*)\s*[:=]\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>%|[a-zA-Z/]*)?"
)


def canonical_metric_name(name: str) -> str:
    """把别名归一到规范名；未知名字原样返回。"""
    return _METRIC_ALIASES.get(name.lower(), name.lower())


def extract_metrics(message: str) -> list[dict[str, Any]]:
    """从 message 里抽出已知指标。

    **为什么必须有这一步**：真实系统日志的指标写在消息文本里
    （`cpu_used=92.4%`），不是结构化 JSON 字段。没有它，`metric_name` / `value`
    永远是空的，`CPUSpike` / `MemoryGrowth` / `DiskFull` 三个 analyzer 在
    **任何真实日志上都不会触发** —— 它们会变成"写了但从不生效"的代码。
    上传解析管道与这里走的是同一条抽取路径，不存在"只有测试能触发"的旁路。
    """
    if not message:
        return []

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in _METRIC_PAIR.finditer(message):
        name = canonical_metric_name(match.group("name"))
        if name not in METRIC_UNITS or name in seen:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        seen.add(name)
        found.append(
            {
                "metric_name": name,
                "value": value,
                # 消息里没写单位就用该指标的约定单位
                "unit": match.group("unit") or METRIC_UNITS[name],
            }
        )
    return found


def infer_severity(raw: dict[str, Any]) -> str:
    """从显式等级词或 message 关键词推断严重度。显式等级词优先。"""
    level = str(raw.get("level", "")).lower()
    if level in _LEVEL_TO_SEVERITY:
        return _LEVEL_TO_SEVERITY[level]
    message = str(raw.get("msg", ""))
    if _HIGH_SEVERITY_HINTS.search(message):
        return "high"
    return "low"
