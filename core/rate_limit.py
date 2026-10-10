from __future__ import annotations

import asyncio
import logging
import math
import os
import threading
import time
from collections import deque

import redis
import redis.asyncio
from redis.backoff import NoBackoff
from redis.retry import Retry

logger = logging.getLogger(__name__)

# 本模块拥有的 Redis key 前缀：reset 只 SCAN/DEL 自己的前缀，严禁 KEYS/FLUSHALL。
RATE_LIMIT_KEY_PREFIX = "cs:rl:"

# 不许拆成 INCR 再单独 EXPIRE 两步：两步之间掉线会留下永久计数。
_FIXED_WINDOW_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
end
if count > tonumber(ARGV[1]) then
    local ttl = redis.call('TTL', KEYS[1])
    if ttl < 0 then
        -- 计数存在但没有过期时间（异常残留），补一次过期，避免永久计数
        redis.call('EXPIRE', KEYS[1], ARGV[2])
        ttl = tonumber(ARGV[2])
    end
    return ttl
end
return 0
"""


class InMemoryRateLimiter:
    def __init__(self, max_requests: int, window_seconds: int):
        if max_requests <= 0:
            raise ValueError("max_requests must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._records: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._checks_since_purge = 0
        self._purge_every = 256

    async def check(self, key: str) -> int | None:
        return self._check(key)

    def _check(self, key: str) -> int | None:
        now = time.monotonic()
        key = str(key)
        with self._lock:
            records = self._records.get(key)
            if records is None:
                records = deque()
                self._records[key] = records
            else:
                while records and now - records[0] >= self.window_seconds:
                    records.popleft()
                if not records:
                    del self._records[key]
                    records = deque()
                    self._records[key] = records

            if len(records) >= self.max_requests:
                retry_after = int(math.ceil(self.window_seconds - (now - records[0])))
                return max(1, retry_after)
            records.append(now)

            self._checks_since_purge += 1
            if self._checks_since_purge >= self._purge_every:
                self._purge_expired_locked(now)
                self._checks_since_purge = 0
            return None

    async def purge_expired(self, now: float | None = None) -> int:
        return self._purge_expired(now if now is not None else time.monotonic())

    def _purge_expired(self, now: float) -> int:
        with self._lock:
            return self._purge_expired_locked(now)

    def _purge_expired_locked(self, now: float) -> int:
        removed = 0
        for key in list(self._records):
            records = self._records[key]
            while records and now - records[0] >= self.window_seconds:
                records.popleft()
            if not records:
                del self._records[key]
                removed += 1
        return removed

    @property
    def tracked_key_count(self) -> int:
        with self._lock:
            return len(self._records)

    @property
    def rate_limit_backend(self) -> str:
        return "memory"

    async def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._records.clear()
            else:
                self._records.pop(str(key), None)


class RedisRateLimiter:
    # Redis 连不上就回落内存实现继续限流，冷却 30 秒内不再等超时；宁可各算一份，也不放行、不整站 429。
    _FALLBACK_COOLDOWN_SECONDS = 30

    def __init__(self, max_requests: int, window_seconds: int, *,
                 host: str | None = None, port: int | None = None,
                 db: int | None = None, password: str | None = None):
        if max_requests <= 0:
            raise ValueError("max_requests must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._host = host or os.getenv("RATE_LIMIT_REDIS_HOST", "127.0.0.1")
        self._port = port if port is not None else int(os.getenv("RATE_LIMIT_REDIS_PORT", "6379"))
        self._db = db if db is not None else int(os.getenv("RATE_LIMIT_REDIS_DB", "0"))
        self._password = password if password is not None else (os.getenv("RATE_LIMIT_REDIS_PASSWORD", "") or None)
        self._timeout = float(os.getenv("RATE_LIMIT_REDIS_TIMEOUT_S", "1"))
        # protocol=2 兼容服务端 3.0.504；retry=0 是怕 redis-py 默认退避把一次失败拖长、绕过 30 秒冷却。
        self._retry_policy = Retry(NoBackoff(), 0)
        self._redis = redis.asyncio.Redis(
            host=self._host, port=self._port, db=self._db, password=self._password,
            protocol=2, socket_connect_timeout=self._timeout, socket_timeout=self._timeout,
            retry=self._retry_policy,
        )
        self._fallback = InMemoryRateLimiter(max_requests, window_seconds)
        self._down_until = 0.0
        self._degraded = False
        self._degraded_since_monotonic: float | None = None

    @property
    def rate_limit_backend(self) -> str:
        return "memory" if self._degraded else "redis"

    @property
    def degraded_since(self) -> float | None:
        return self._degraded_since_monotonic

    def _redis_key(self, key: str) -> str:
        return f"{RATE_LIMIT_KEY_PREFIX}{key}:w{self.window_seconds}"

    async def check(self, key: str) -> int | None:
        key = str(key)
        now = time.monotonic()
        if now < self._down_until:
            return await self._fallback.check(key)
        try:
            result = await self._redis.eval(
                _FIXED_WINDOW_LUA, 1, self._redis_key(key),
                self.max_requests, self.window_seconds,
            )
        except (redis.exceptions.RedisError, OSError, asyncio.TimeoutError) as exc:
            self._down_until = now + self._FALLBACK_COOLDOWN_SECONDS
            if not self._degraded:
                self._degraded = True
                self._degraded_since_monotonic = now
            logger.error(
                "rate_limit.redis_fallback key=%s cooldown_s=%d backend=memory error=%s",
                key, self._FALLBACK_COOLDOWN_SECONDS, exc,
            )
            return await self._fallback.check(key)
        if self._degraded:
            self._degraded = False
            self._degraded_since_monotonic = None
            logger.warning("rate_limit.recovered key=%s backend=redis", key)
        retry_after = int(result)
        if retry_after > 0:
            return max(1, retry_after)
        return None

    async def purge_expired(self, now: float | None = None) -> int:
        return 0

    async def reset(self, key: str | None = None) -> None:
        pattern = f"{RATE_LIMIT_KEY_PREFIX}{key}:w*" if key is not None else f"{RATE_LIMIT_KEY_PREFIX}*"
        client = redis.asyncio.Redis(
            host=self._host, port=self._port, db=self._db, password=self._password,
            protocol=2, socket_connect_timeout=self._timeout, socket_timeout=self._timeout,
            retry=self._retry_policy,
        )
        try:
            cursor = 0
            while True:
                cursor, keys = await client.scan(cursor=cursor, match=pattern, count=200)
                if keys:
                    await client.delete(*keys)
                if cursor == 0:
                    break
        finally:
            await client.aclose()


def create_rate_limiter(max_requests: int, window_seconds: int):
    backend = os.getenv("RATE_LIMIT_BACKEND", "memory").strip().lower()
    if backend == "redis":
        return RedisRateLimiter(max_requests, window_seconds)
    if backend == "memory":
        return InMemoryRateLimiter(max_requests, window_seconds)
    raise ValueError(f"未知 RATE_LIMIT_BACKEND={backend!r}，只支持 memory|redis")
