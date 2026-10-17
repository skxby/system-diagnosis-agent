r"""脱敏（红线 5：脱敏必须发生在落盘之前）。

规则按计划第 543–551 行，**保守优先，避免误伤排障字段**：

覆盖：
- 邮箱
- 信用卡（**Luhn 校验通过才替换**）
- 电话号码（大陆手机号及带分隔符、国际前缀的形态；JSON 数字计数器保留）
- 明确前缀的密钥：`sk-…` / `ghp_…` / `AKIA…` / `xoxb-…` 等
- IP 地址（**可配置开关**：内网排障常需要它，默认关闭）

**刻意不做**（计划第 549–550 行）：
- 不用 `[A-Za-z0-9]{32,}` 这类宽匹配 —— 它会误伤 commit hash / trace id / session id；
- 不做 NER 人名机构识别（后移 V1.5）。

替换成**一致性占位符**：同一次 Run 内同一原值总是得到同一占位符，
便于跨项目关联；单用户全局一致（计划第 553 行）。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

#: 占位符使用 64 bit 哈希；旧的 8 hex 占位符不重写。
HASH_LEN = 16

#: 默认盐。单用户场景用固定盐即可让占位符「全局一致」（计划第 553 行）。
#: 换盐会使历史占位符与新占位符不再对应，故不要随意改。
DEFAULT_SALT = "logagent-v1"


def hash_token(value: str, *, salt: str = DEFAULT_SALT) -> str:
    """给原值生成稳定的短哈希（一致性占位符的核心）。"""
    digest = hashlib.sha256(f"{salt}\0{value}".encode()).hexdigest()
    return digest[:HASH_LEN]


def luhn_ok(value: str) -> bool:
    """Luhn 校验 + 防误伤启发式。

    **实测教训**：只靠 Luhn 会大量误伤。在 `logs/` 全量上跑，330 个「卡号」全是假的：
    - `1446249502000` —— 13 位 epoch 毫秒时间戳（Luhn 对 16 位数字约 10% 概率通过，
      13 位亦然），nginxplus 状态日志里每一行都有一个；
    - `0000000000000000` —— 内核 e820 内存映射地址。

    故加三道闸：
    1. 位数限定 15–19（排除 13 位时间戳；代价是罕见的 13 位老卡不识别，
       这是**刻意的保守取向**：误伤排障字段比漏一个罕见卡号危害更大）；
    2. 全同数字串不算卡号（`0000…`）；
    3. Luhn 校验。
    """
    if len(set(digits_only := re.sub(r"\D", "", value))) < 2:
        return False  # 全同数字：内存地址 / 填充值
    if not 15 <= len(digits_only) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(digits_only)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


# ============================================================
# 模式
# ============================================================

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

#: IP：前后不能再跟数字或点，避免吃掉版本号 `1.2.3.4.5` 的一部分
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")

#: 信用卡：两种真实形态——
#:   带分隔符：`4111 1111 1111 1111` / `4111-1111-1111-1111`（4 位一组）
#:   连续数字：15–19 位（**不匹配 13 位**，那是 epoch 毫秒时间戳的常见长度）
#: 仍需通过 Luhn 与全同数字检查（见 `luhn_ok`）。
CARD_RE = re.compile(
    r"(?<![\d\-])(?:"
    r"\d{4}(?:[ \-]\d{4}){3}"
    r"|"
    r"\d{15,19}"
    r")(?![\d\-])"
)

#: 电话：**必须**带国际前缀或分隔符。这一限制是刻意的——
#: 放宽会把日志里大量普通数字（端口、行号、耗时）当成电话，制造误伤。
PHONE_RE = re.compile(
    r"(?<![\d\-])"
    r"(?:\+\d{1,3}[ \-]?\d{2,4}[ \-]?\d{3,4}(?:[ \-]\d{2,4})?)"
    r"|(?<![\d\-])(?:\(\d{2,4}\)[ ]?\d{3,4}[ \-]\d{3,4})"
    r"(?![\d\-])"
)
MOBILE_RE = re.compile(r"(?<![A-Za-z0-9_])1[3-9]\d{9}(?![A-Za-z0-9_])")
IDENTITY_RE = re.compile(r"(?<![A-Za-z0-9_])[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![A-Za-z0-9_])")
BEARER_RE = re.compile(r"(?i)(\bbearer\s+)([A-Za-z0-9._~+/=-]+)")
URL_PASSWORD_RE = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:)([^\s/@]+)(?=@)")
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN ([A-Z ]*PRIVATE KEY)-----.*?-----END \1-----", re.DOTALL)

#: 明确前缀的密钥（计划第 548 行）。前缀本身就是强信号，无需额外校验。
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("github_pat", re.compile(r"\bghp_[A-Za-z0-9]{20,}\b")),
    ("github_oauth", re.compile(r"\bgho_[A-Za-z0-9]{20,}\b")),
    ("github_app", re.compile(r"\bghs_[A-Za-z0-9]{20,}\b")),
    ("aws_akid", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("google_api", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

#: 通用 `key=value` 形态：键名必须明确含 key/secret/token/password 才替换。
GENERIC_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b(\s*[:=]\s*)([\"']?)([A-Za-z0-9_\-/+.]{8,})([\"']?)"
)


def normalize_digits(value: str) -> str:
    """去掉分隔符，只留数字。用于卡号 / 电话 / IP 等「写法不同、值相同」的场合。

    没有这步，`4111 1111 1111 1111`、`4111-1111-1111-1111` 与
    `4111111111111111` 会得到三个不同占位符，破坏「一致性占位符」的承诺
    （计划第 553 行：便于跨项目关联）。
    """
    return re.sub(r"\D", "", value)


def normalize_email(value: str) -> str:
    """邮箱大小写不敏感，统一小写后再哈希。"""
    return value.strip().lower()


@dataclass
class MaskRule:
    """一条脱敏规则。

    `normalize` 在哈希前对原值做规范化：**写法不同但语义相同的值必须得到同一占位符**。
    """

    name: str
    pattern: re.Pattern[str]
    token_kind: str
    validator: Callable[[str], bool] | None = None
    normalize: Callable[[str], str] | None = None


@dataclass
class MaskStats:
    """误伤与漏检都要计数，写入 Run 元数据供调规则（计划第 561 行）。"""

    counts: dict[str, int] = field(default_factory=dict)
    total: int = 0

    def record(self, rule_name: str) -> None:
        self.counts[rule_name] = self.counts.get(rule_name, 0) + 1
        self.total += 1

    def merge(self, other: MaskStats) -> None:
        for key, value in other.counts.items():
            self.counts[key] = self.counts.get(key, 0) + value
        self.total += other.total

    def as_dict(self) -> dict[str, object]:
        return {"total": self.total, "by_rule": dict(sorted(self.counts.items()))}


def build_rules(*, mask_ip: bool) -> tuple[MaskRule, ...]:
    rules: list[MaskRule] = [
        MaskRule("private_key_block", PRIVATE_KEY_RE, "SECRET"),
        MaskRule("jwt", JWT_RE, "SECRET"),
        MaskRule("identity", IDENTITY_RE, "IDENTITY"),
        MaskRule("email", EMAIL_RE, "EMAIL", normalize=normalize_email),
        MaskRule("mobile", MOBILE_RE, "PHONE", normalize=normalize_digits),
        MaskRule("card", CARD_RE, "CARD", validator=luhn_ok, normalize=normalize_digits),
        MaskRule("phone", PHONE_RE, "PHONE", normalize=normalize_digits),
    ]
    rules.extend(
        MaskRule(f"secret_{kind}", pattern, "SECRET") for kind, pattern in SECRET_PATTERNS
    )
    if mask_ip:
        rules.append(MaskRule("ipv4", IPV4_RE, "IP", normalize=normalize_digits))
    return tuple(rules)


class Masker:
    """一致性脱敏器。同一实例内，相同原值 → 相同占位符。"""

    def __init__(
        self,
        *,
        mask_ip: bool = False,
        salt: str = DEFAULT_SALT,
        enabled: bool = True,
    ) -> None:
        self.mask_ip = mask_ip
        self.salt = salt
        self.enabled = enabled
        self._rules = build_rules(mask_ip=mask_ip)
        self.stats = MaskStats()

    def token_for(self, kind: str, raw: str) -> str:
        return f"[{kind}_{hash_token(raw, salt=self.salt)}]"

    def mask(self, text: str) -> str:
        """结构化输入按字段脱敏，保留数字计数器与合法 JSON 语法。"""
        if not self.enabled or not text:
            return text
        if text.lstrip().startswith(("{", "[")):
            try:
                raw = json.loads(text)
            except (ValueError, TypeError):
                lines = text.splitlines(keepends=True)
                if len(lines) > 1 and all(not line.strip() or line.lstrip().startswith("{") for line in lines):
                    return "".join(self.mask(line.rstrip("\r\n")) + ("\n" if line.endswith("\n") else "") for line in lines)
            else:
                masked = self._mask_json(raw)
                return json.dumps(masked, ensure_ascii=False) if masked != raw else text
        return self._mask_text(text)

    def _mask_json(self, value, key: str = ""):
        if isinstance(value, dict):
            return {self._mask_text(str(k)): self._mask_json(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [self._mask_json(item, key) for item in value]
        if key.lower() in {"phone", "mobile", "phone_number", "mobile_number", "id_card", "identity_number",
                           "card", "card_number", "credit_card", "card_no", "id_number", "identity_card",
                           "手机号", "手机号码", "电话号码", "身份证", "身份证号", "银行卡", "银行卡号",
                           "password", "passwd", "pwd", "secret", "token", "api_key", "apikey"} and value is not None:
            raw = str(value)
            if re.fullmatch(r"\[[A-Z]+_[0-9a-f]+\]", raw):
                return value
            self.stats.record("structured_sensitive")
            return self.token_for("SECRET", raw)
        return self._mask_text(value) if isinstance(value, str) else value

    def _mask_text(self, text: str) -> str:
        """脱敏一段文本。`enabled=False` 时原样返回。"""
        if not self.enabled or not text:
            return text

        def secret_value(match: re.Match[str]) -> str:
            if re.fullmatch(r"\[SECRET_[0-9a-f]+\]", match.group(2)):
                return match.group(0)
            self.stats.record("secret_credential")
            return match.group(1) + self.token_for("SECRET", match.group(2))

        text = BEARER_RE.sub(secret_value, text)
        text = URL_PASSWORD_RE.sub(secret_value, text)

        # IP 规则刻意排在最后（见 _apply 之后的注释），故此处跳过它
        for rule in self._rules:
            if rule.name == "ipv4":
                continue
            result = self._apply(rule, text)
            text = result

        # 通用 key=value：保留键名与分隔符，只替换值
        def replace_generic(match: re.Match[str]) -> str:
            secret = match.group(4)
            self.stats.record("secret_generic")
            return (
                f"{match.group(1)}{match.group(2)}{match.group(3)}"
                f"{self.token_for('SECRET', secret)}{match.group(5)}"
            )

        text = GENERIC_SECRET_RE.sub(replace_generic, text)

        # IP 放最后：邮箱域名、版本号里的点分数字先被前面的规则处理掉，
        # 否则 `1.2.3.4` 可能从版本号中间被切出来。
        if self.mask_ip:
            ip_rule = next(r for r in self._rules if r.name == "ipv4")
            text = self._apply(ip_rule, text)
        return text

    def _apply(self, rule: MaskRule, text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            raw = match.group(0)
            if rule.validator is not None and not rule.validator(raw):
                return raw  # 校验不过：保持原样，避免误伤
            self.stats.record(rule.name)
            # 规范化后再哈希：写法不同、语义相同的值必须得到同一占位符
            key = rule.normalize(raw) if rule.normalize is not None else raw
            return self.token_for(rule.token_kind, key)

        return rule.pattern.sub(replace, text)
