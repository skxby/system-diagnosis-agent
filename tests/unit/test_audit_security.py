"""审计回归：隐私、编码、日期与请求边界，均不依赖数据库。"""
from datetime import datetime, timezone

import httpx
import pytest
import json
from fastapi import FastAPI, UploadFile
from fastapi.testclient import TestClient

from app.api.middleware import RequestSafetyMiddleware
from app.api.security import hash_password, verify_password, create_access_token, decode_access_token, TokenError
from app.gateways.base import ModelRequestError
from app.gateways.deepseek import DeepSeekGateway
from app.analysis.idempotency import is_retryable
from app.services.upload_service import UploadService, MAX_UPLOAD_BYTES
from app.utils.masking import Masker
from app.utils.timestamps import parse_timestamp, TimestampUnparseableError


@pytest.mark.parametrize("sensitive", [
    "13812345678", "11010119900307123X", "Bearer short-token",
    "联系13812345678谢谢", "身份证11010119900307123X",
    "mysql://root:Passw0rd@localhost/db", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
    "-----BEGIN RSA PRIVATE KEY-----\nvery-private-body\n-----END RSA PRIVATE KEY-----",
])
def test_sensitive_forms_are_removed_and_masking_is_idempotent(sensitive):
    masked = Masker().mask(sensitive)
    assert masked != sensitive
    for secret in ["13812345678", "11010119900307123X", "short-token", "Passw0rd", "very-private-body"]:
        assert secret not in masked
    assert Masker().mask(masked) == masked


@pytest.mark.parametrize("text", ["pid=65535 port=8080 duration=1446249502000", "trace=abc123deadbeef cpu=100.0"])
def test_troubleshooting_identifiers_are_preserved(text):
    assert Masker().mask(text) == text


@pytest.mark.parametrize("secret", ["sk-" + "a" * 20, "ghp_" + "a" * 24,
    "gho_" + "a" * 24, "ghs_" + "a" * 24, "AKIA" + "A" * 16,
    "xoxb-" + "a" * 20, "AIza" + "a" * 35, "password=secretvalue123"])
def test_known_credentials_never_survive_masking(secret):
    assert Masker().mask(secret) != secret


def test_ip_switch_and_disabled_helper_are_explicit():
    assert Masker().mask("10.0.0.1") == "10.0.0.1"
    assert "10.0.0.1" not in Masker(mask_ip=True).mask("10.0.0.1")
    assert Masker(enabled=False).mask("user@example.com") == "user@example.com"


def test_credit_card_variants_share_one_placeholder():
    masker = Masker()
    assert len({masker.mask(value) for value in
                ("4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111")}) == 1


def test_json_numeric_counters_are_preserved_but_sensitive_numbers_are_masked():
    source = {"bytes": 13812345678, "mobile": 13812345678, "message": "联系 13812345678", "card_number": 4111111111111111}
    masked = Masker().mask(json.dumps(source))
    result = json.loads(masked)
    assert result["bytes"] == source["bytes"]
    assert result["mobile"].startswith("[SECRET_")
    assert result["card_number"].startswith("[SECRET_")
    assert "13812345678" not in result["message"]
    assert json.loads(Masker().mask(masked)) == result


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "gb18030", "utf-16"])
def test_upload_decodes_chinese_without_replacement(encoding):
    text = "ERROR 服务异常"
    assert UploadService._decode(text.encode(encoding)) == text


def test_invalid_encoding_is_explicitly_rejected():
    with pytest.raises(ValueError):
        UploadService._decode(b"\xff")


def test_syslog_year_boundary_and_spacing():
    reference = datetime(2026, 1, 3, tzinfo=timezone.utc)
    assert parse_timestamp("Dec 28 10:00:00", now=reference).year == 2025
    assert parse_timestamp("Jul  1 10:00:00", now=reference).month == 7
    assert parse_timestamp("2026-01-03 08:00:00").hour == 0


@pytest.mark.parametrize("raw,kwargs", [
    ("Feb 29 10:00:00", {"now": datetime(2026, 6, 1, tzinfo=timezone.utc)}),
    ("2026-01-03 08:00:00", {"default_timezone": "Mars/Unknown"}),
])
def test_invalid_dates_and_zones_never_fall_back_to_now(raw, kwargs):
    with pytest.raises(TimestampUnparseableError):
        parse_timestamp(raw, **kwargs)


def test_bcrypt_byte_boundary_and_expired_token():
    value = "a" * 72
    hashed = hash_password(value)
    assert verify_password(value, hashed)
    assert not verify_password(value + "x", hashed)
    assert not verify_password(value + "y", hashed)
    with pytest.raises(ValueError):
        hash_password("中" * 25)
    secret = "test-only-secret" * 4
    expired = create_access_token(subject=1, secret_key=secret,
                                  now=datetime(2000, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(TokenError):
        decode_access_token(expired, secret_key=secret)
    with pytest.raises(ValueError):
        create_access_token(subject=1, secret_key="")


def test_upload_body_is_bounded_before_parsing_and_never_spools():
    # 导入真实应用以安装与生产相同的 multipart 内存阈值。
    from app.main import app as production_app
    test_app = FastAPI()
    test_app.add_middleware(RequestSafetyMiddleware)

    @test_app.post("/upload")
    def upload(file: UploadFile):
        return {"rolled": file.file._rolled, "bytes": len(file.file.read())}

    with TestClient(test_app) as client:
        valid = client.post("/upload", files={"file": ("log.txt", b"a" * (2 * 1024 * 1024))})
        assert valid.json() == {"rolled": False, "bytes": 2 * 1024 * 1024}
        assert client.post("/upload", content=b"x" * (MAX_UPLOAD_BYTES + 65537)).status_code == 413
        assert client.post("/upload", content=b"x", headers={"Origin": "https://evil.invalid"}).status_code == 403


def test_model_401_is_not_retried_or_exposed():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, text="private-upstream-body")

    with httpx.Client(base_url="https://test.invalid", transport=httpx.MockTransport(handler)) as client:
        gateway = DeepSeekGateway(api_key="test", base_url="https://test.invalid", client=client)
        with pytest.raises(ModelRequestError) as error:
            gateway.generate("test", None, 10, model="test")
    assert not is_retryable(error.value)
    assert "private-upstream-body" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("actual,expected", [("L1", ["L1"]), ("L2", ["L2", "L1"])])
def test_fallback_does_not_repeat_a_tier_already_selected_by_complexity(actual, expected):
    from app.analysis.retry import call_with_fallback, RetryPolicy
    from app.gateways.base import ModelUnavailableError
    calls = []
    def attempt(cap):
        tier = min(actual, cap)
        calls.append(tier)
        error = ModelUnavailableError("offline")
        error.tier_used = tier
        raise error
    outcome = call_with_fallback(attempt, start_tier="L3", retry_policy=RetryPolicy(max_retries=0),
                                 rules_only_fallback=lambda: "rules")
    assert outcome.used_rules_only and calls == expected
