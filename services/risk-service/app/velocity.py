"""
Velocity tracking, swappable the same way the idempotency store is.

The in-memory version is exactly what RiskEngine used to do internally,
extracted here so it can be swapped for the Redis version without
RiskEngine's own rules changing at all.

The Redis version uses a sorted set per card: ZADD records an attempt with
its timestamp as the score, ZREMRANGEBYSCORE prunes anything older than
the window, ZCARD counts what's left. This is the same sliding-window
algorithm the in-memory version runs, just backed by a store every
replica shares, so an attacker spreading attempts across multiple pods
can't evade detection by hitting a different one each time.
"""

import threading
import time
import uuid
from abc import ABC, abstractmethod


class VelocityTracker(ABC):
    @abstractmethod
    def record_and_count_recent(self, card_number: str, window_seconds: float) -> int:
        """Records one attempt now, and returns how many attempts fall within the window."""
        ...


class InMemoryVelocityTracker(VelocityTracker):
    def __init__(self):
        self._recent_attempts = {}   # card_number -> [timestamps]
        self._lock = threading.Lock()

    def record_and_count_recent(self, card_number: str, window_seconds: float) -> int:
        now = time.monotonic()
        with self._lock:
            attempts = self._recent_attempts.setdefault(card_number, [])
            attempts.append(now)
            cutoff = now - window_seconds
            attempts[:] = [t for t in attempts if t >= cutoff]
            return len(attempts)


class RedisVelocityTracker(VelocityTracker):
    def __init__(self, redis_client):
        self._redis = redis_client

    def _key(self, card_number: str) -> str:
        return f"velocity:{card_number}"

    def record_and_count_recent(self, card_number: str, window_seconds: float) -> int:
        now = time.time()
        member = f"{now}-{uuid.uuid4().hex[:8]}"  # unique per attempt, even at the same timestamp
        key = self._key(card_number)

        pipe = self._redis.pipeline()
        pipe.zadd(key, {member: now})
        pipe.zremrangebyscore(key, 0, now - window_seconds)
        pipe.zcard(key)
        pipe.expire(key, int(window_seconds) + 5)  # safety net so an abandoned card's key doesn't linger forever
        _, _, count, _ = pipe.execute()
        return count
