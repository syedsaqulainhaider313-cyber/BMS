"""Domain-keyed token bucket rate throttling.

Provides ISP classification (Google, Yahoo, Microsoft, Apple, Default)
and token bucket rate limiting with atomic Redis Lua script support
and thread-safe in-memory fallback for offline test environments.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Protocol

from app.settings import settings

logger = logging.getLogger(__name__)


class RateLimiter(Protocol):
    def acquire(self, email: str, tokens: int = 1) -> tuple[bool, float]:
        """Attempt to acquire tokens for the email domain.

        Returns (allowed: bool, wait_time: float).
        """
        ...


def classify_isp(email: str) -> str:
    """Classifies recipient email domain into standard ISP cluster."""
    domain = email.split("@")[-1].lower().strip() if "@" in email else ""
    if domain in {"gmail.com", "googlemail.com"}:
        return "google"
    if domain in {
        "yahoo.com",
        "ymail.com",
        "rocketmail.com",
        "aol.com",
        "aim.com",
        "verizon.net",
    }:
        return "yahoo"
    if domain in {
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "passport.com",
    }:
        return "microsoft"
    if domain in {"icloud.com", "me.com", "mac.com"}:
        return "apple"
    return "default"


def get_isp_limits() -> dict[str, dict[str, float]]:
    return {
        "google": {
            "rate": getattr(settings, "isp_rate_google", 20.0),
            "capacity": float(getattr(settings, "isp_burst_google", 40)),
        },
        "yahoo": {
            "rate": getattr(settings, "isp_rate_yahoo", 10.0),
            "capacity": float(getattr(settings, "isp_burst_yahoo", 20)),
        },
        "microsoft": {
            "rate": getattr(settings, "isp_rate_microsoft", 10.0),
            "capacity": float(getattr(settings, "isp_burst_microsoft", 20)),
        },
        "apple": {
            "rate": getattr(settings, "isp_rate_apple", 5.0),
            "capacity": float(getattr(settings, "isp_burst_apple", 10)),
        },
        "default": {
            "rate": getattr(settings, "isp_rate_default", 10.0),
            "capacity": float(getattr(settings, "isp_burst_default", 20)),
        },
    }


class InMemoryTokenBucketLimiter:
    """Thread-safe in-memory token bucket limiter for offline testing and Redis failover."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, dict[str, float]] = {}

    def acquire(self, email: str, tokens: int = 1) -> tuple[bool, float]:
        isp = classify_isp(email)
        limits = get_isp_limits()[isp]
        rate = limits["rate"]
        capacity = limits["capacity"]

        now = time.time()
        with self._lock:
            state = self._buckets.setdefault(isp, {"tokens": capacity, "last_updated": now})
            elapsed = max(0.0, now - state["last_updated"])
            state["tokens"] = min(capacity, state["tokens"] + elapsed * rate)
            state["last_updated"] = now

            if state["tokens"] >= tokens:
                state["tokens"] -= tokens
                return True, 0.0
            else:
                needed = tokens - state["tokens"]
                wait_time = needed / rate
                return False, wait_time

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


class RedisTokenBucketLimiter:
    """Distributed Redis Token Bucket limiter running atomic Lua script."""

    def __init__(self, redis_client) -> None:
        self.redis = redis_client
        lua_path = os.path.join(os.path.dirname(__file__), "throttling_token_bucket.lua")
        with open(lua_path, "r", encoding="utf-8") as f:
            self._script = self.redis.register_script(f.read())

    def acquire(self, email: str, tokens: int = 1) -> tuple[bool, float]:
        isp = classify_isp(email)
        limits = get_isp_limits()[isp]
        key = f"bms:ratelimit:bucket:{isp}"
        now = time.time()

        try:
            result = self._script(
                keys=[key],
                args=[limits["rate"], limits["capacity"], now, tokens],
            )
            allowed = bool(result[0])
            wait_time = float(result[1])
            return allowed, wait_time
        except Exception as exc:
            logger.warning("redis_rate_limiter_failed_fallback_to_allow: %s", exc)
            return True, 0.0


_GLOBAL_IN_MEMORY_LIMITER = InMemoryTokenBucketLimiter()


def get_rate_limiter() -> RateLimiter:
    """Returns Redis rate limiter if available; otherwise returns in-memory fallback."""
    if not settings.redis_url or settings.app_env in {"test", "development"}:
        return _GLOBAL_IN_MEMORY_LIMITER

    try:
        from redis import Redis

        r = Redis.from_url(settings.redis_url, socket_timeout=1.0)
        r.ping()
        return RedisTokenBucketLimiter(r)
    except Exception as exc:
        logger.warning("redis_unavailable_using_in_memory_limiter: %s", exc)
        return _GLOBAL_IN_MEMORY_LIMITER
