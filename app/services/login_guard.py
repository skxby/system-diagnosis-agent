"""使用 Redis 共享登录尝试窗口；Web 多进程使用同一计数器。"""
from __future__ import annotations

import hashlib

from redis import Redis, RedisError
from app.config import get_settings

_COUNT = "local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],300) end; return n"


class LoginRateLimitError(ValueError):
    pass


class LoginGuardUnavailable(RuntimeError):
    pass


def check_login_attempt(email: str, address: str) -> None:
    client = Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, socket_timeout=2)
    try:
        for identity, limit in ((address, 50), (address + "\0" + email.lower(), 10)):
            key = "logagent:login:" + hashlib.sha256(
                (get_settings().database_url + "\0" + identity).encode()).hexdigest()
            if int(client.eval(_COUNT, 1, key)) > limit:
                raise LoginRateLimitError("登录尝试过多，请在五分钟后重试")
    except RedisError as exc:
        raise LoginGuardUnavailable("登录保护暂不可用，请检查 Redis") from exc
    finally:
        client.close()
