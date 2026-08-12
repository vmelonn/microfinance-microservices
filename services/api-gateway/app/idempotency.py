"""
Idempotency store, swappable between an in-memory dict (today's behavior,
zero setup, one process only) and Redis (safe across many replicas).

Both implementations fix a real race the original code had: the old
pattern was "check cache, process, then store result", three separate
steps. Two concurrent requests with the SAME brand-new idempotency key
could both pass the "not seen yet" check before either finished storing,
and both would get fully processed. That's the same category of race the
ledger's PRIMARY KEY on RRN was built to close, this store closes the
equivalent race at the API layer, via an atomic claim step before any
processing happens at all.

claim() has three possible outcomes:
  - "new"        -- this caller won the race, proceed with processing
  - "duplicate"  -- already fully processed; here's the cached response
  - "mismatch"   -- same key, but a DIFFERENT request body, reject, 400
  - "in_progress"-- another caller claimed it and hasn't finished yet
                    (a genuine, rare race, the caller should NOT process
                    again; treat like a 409 and let the client retry)
"""

import json
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class ClaimOutcome:
    status: str  # "new" | "duplicate" | "mismatch" | "in_progress"
    cached_response: Optional[dict] = None


class IdempotencyStore(ABC):
    @abstractmethod
    def claim(self, key: str, request_hash: str) -> ClaimOutcome:
        """Atomically checks-and-claims. Must be called BEFORE processing starts."""
        ...

    @abstractmethod
    def store_response(self, key: str, response: dict) -> None:
        """Called once processing finishes, to record the result for future duplicates."""
        ...

    @abstractmethod
    def release(self, key: str) -> None:
        """
        Drop a claim so the key can be used again.

        ONLY for work that definitively did not happen. A claim exists to
        stop a retry double-processing, so releasing one after something may
        have reached the switch would trade a stuck key for a double charge,
        which is far worse. See _release_on_definite_failure in main.py for
        the rule the gateway applies.
        """
        ...

    @abstractmethod
    def clear_all(self) -> None:
        """Sandbox/testing utility only, wipes every claim and cached response."""
        ...


class InMemoryIdempotencyStore(IdempotencyStore):
    """Today's behavior, made genuinely atomic via a single process-wide lock."""

    def __init__(self, ttl_seconds: int = 86400):
        self._entries = {}   # key -> {"request_hash": ..., "response": ... | None, "claimed_at": ...}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def _prune_expired(self):
        now = time.time()
        expired = [k for k, v in self._entries.items() if now - v["claimed_at"] > self._ttl]
        for k in expired:
            del self._entries[k]

    def claim(self, key: str, request_hash: str) -> ClaimOutcome:
        with self._lock:
            self._prune_expired()
            existing = self._entries.get(key)

            if existing is None:
                self._entries[key] = {"request_hash": request_hash, "response": None, "claimed_at": time.time()}
                return ClaimOutcome(status="new")

            if existing["request_hash"] != request_hash:
                return ClaimOutcome(status="mismatch")

            if existing["response"] is not None:
                return ClaimOutcome(status="duplicate", cached_response=existing["response"])

            return ClaimOutcome(status="in_progress")

    def store_response(self, key: str, response: dict) -> None:
        with self._lock:
            if key in self._entries:
                self._entries[key]["response"] = response

    def release(self, key: str) -> None:
        with self._lock:
            self._entries.pop(key, None)

    def clear_all(self) -> None:
        with self._lock:
            self._entries.clear()


class RedisIdempotencyStore(IdempotencyStore):
    """
    Same contract, backed by Redis so every replica shares one answer.
    Two keys per idempotency key: one for the claim (set atomically via
    SET NX), one for the eventual response, kept separate so "claimed
    but still processing" and "claimed and finished" are distinguishable.
    """

    def __init__(self, redis_client, ttl_seconds: int = 86400):
        self._redis = redis_client
        self._ttl = ttl_seconds

    def _hash_key(self, key: str) -> str:
        return f"idempotency:{key}:hash"

    def _response_key(self, key: str) -> str:
        return f"idempotency:{key}:response"

    def claim(self, key: str, request_hash: str) -> ClaimOutcome:
        # SET ... NX is Redis's atomic "only set if absent", this is the
        # operation that actually closes the race, the same way the
        # ledger's PRIMARY KEY closed it at the database level.
        claimed = self._redis.set(self._hash_key(key), request_hash, nx=True, ex=self._ttl)
        if claimed:
            return ClaimOutcome(status="new")

        existing_hash = self._redis.get(self._hash_key(key))
        if existing_hash is None:
            # Expired between the failed claim and this read, vanishingly
            # rare, but treat it as safe to retry as new rather than erroring.
            return self.claim(key, request_hash)

        existing_hash = existing_hash.decode("utf-8") if isinstance(existing_hash, bytes) else existing_hash
        if existing_hash != request_hash:
            return ClaimOutcome(status="mismatch")

        stored_response = self._redis.get(self._response_key(key))
        if stored_response is not None:
            if isinstance(stored_response, bytes):
                stored_response = stored_response.decode("utf-8")
            return ClaimOutcome(status="duplicate", cached_response=json.loads(stored_response))

        return ClaimOutcome(status="in_progress")

    def store_response(self, key: str, response: dict) -> None:
        self._redis.set(self._response_key(key), json.dumps(response), ex=self._ttl)

    def release(self, key: str) -> None:
        # BOTH keys. The hash key is what SET NX guards, so leaving it would
        # keep the claim; the response key should be absent anyway on a
        # failure, and deleting it costs nothing and removes any chance of a
        # stale response outliving the claim that produced it.
        self._redis.delete(self._hash_key(key), self._response_key(key))

    def clear_all(self) -> None:
        # SCAN instead of KEYS, KEYS blocks the whole Redis server while it
        # runs, which is fine for a tiny sandbox but a bad habit to build;
        # SCAN walks the keyspace incrementally without blocking anyone else.
        for key in self._redis.scan_iter(match="idempotency:*"):
            self._redis.delete(key)
