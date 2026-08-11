"""
Live request tracing, what a distributed system needs and a monolith did
not.

In the monolith, one purchase was one call stack and a traceback told you
everything. Here it is seven services, and "where did this request get to"
has no answer unless something records it.

HOW IT WORKS. Every service already carries a correlation ID through every
hop, including the SOAP one. Each service appends events to a Redis list
keyed by that ID; the gateway reads the list back. The result is a
per-request timeline: which layer, when, what happened.

WHY NOT OpenTelemetry AND JAEGER. That is the correct answer for a real
platform and the wrong one here. It needs a collector and a Jaeger backend
-- two more pods on a namespace already tight enough that nine parallel
builds could not be scheduled, and it puts the trace in a separate UI
rather than in the console next to the transaction that produced it. This is
about 120 lines and reuses the Redis that is already deployed.

THE RULES THIS MODULE FOLLOWS, because a debug feature must never be able to
break a payment:

  1. EVERY failure is swallowed. Redis down, wrong type, serialisation
     error, tracing degrades to nothing and the transaction proceeds. A
     trace that takes down the money path is worse than no trace.
  2. Nothing is emitted when no Redis is configured. Local runs and unit
     tests are unaffected and pay nothing.
  3. Events EXPIRE. Traces are for debugging the last few minutes, not an
     audit log, ops/audit_log.py is the audit log. Without a TTL this
     would grow until Redis evicted something that mattered.
  4. Payloads are MASKED through the same rules as the audit log. A trace
     visible in a browser must not be the one place a PIN leaks.
"""

from __future__ import annotations

import json
import time

from mfcommon.observability.audit import mask_payload
from mfcommon.observability.correlation import get_correlation_id

# Fifteen minutes. Long enough to inspect a transaction that just happened,
# short enough that a load test does not fill Redis with dead traces.
TRACE_TTL_SECONDS = 900

# Bounds a single trace. A retry storm or a runaway loop should not be able
# to push an unbounded list into a shared Redis.
MAX_EVENTS_PER_TRACE = 200

_redis = None
_service = "unknown"


def configure(redis_client, service_name: str) -> None:
    """Called once at startup. Passing None disables tracing entirely."""
    global _redis, _service
    _redis = redis_client
    _service = service_name


def _key(correlation_id: str) -> str:
    return f"trace:{correlation_id}"


def emit(stage: str, event: str, detail: dict | None = None, *, level: str = "info") -> None:
    """
    Record one step.

    stage  -- the layer, e.g. "gateway", "risk", "iso8583", "switch"
    event  -- what happened, short and human-readable
    detail, structured extras; masked before it leaves this process
    level  -- info | warn | error, so the console can colour it

    Deliberately returns None and raises nothing. Callers must never have to
    think about whether tracing worked.
    """
    if _redis is None:
        return

    correlation_id = get_correlation_id()
    if not correlation_id or correlation_id == "-":
        return

    try:
        record = {
            "ts": time.time(),
            "service": _service,
            "stage": stage,
            "event": event,
            "level": level,
            # Masked here rather than at the display layer, so a PIN never
            # reaches Redis in the first place, the browser is not the only
            # thing that can read it.
            "detail": mask_payload(detail or {}),
        }

        key = _key(correlation_id)
        pipe = _redis.pipeline()
        pipe.rpush(key, json.dumps(record))
        pipe.ltrim(key, -MAX_EVENTS_PER_TRACE, -1)
        pipe.expire(key, TRACE_TTL_SECONDS)
        pipe.execute()
    except Exception:
        # Intentionally silent. Logging here would turn a Redis outage into a
        # flood of log noise on the hot path of every transaction, and there
        # is nothing a caller could usefully do about it anyway.
        pass


def read(redis_client, correlation_id: str) -> list[dict]:
    """
    Read a trace back, oldest first. Used by the gateway's console endpoint.

    Takes the client explicitly rather than using the module global, because
    the reader is the gateway and the writers are seven different services,
    keeping them separate makes it obvious this is the read side.
    """
    try:
        raw = redis_client.lrange(_key(correlation_id), 0, -1)
    except Exception:
        return []

    events = []
    for item in raw:
        try:
            events.append(json.loads(item.decode() if isinstance(item, bytes) else item))
        except Exception:
            continue  # one malformed entry must not lose the whole trace
    return events


def recent_ids(redis_client, limit: int = 25) -> list[str]:
    """
    The most recently seen correlation IDs, newest first.

    Backed by a capped list the gateway pushes to, NOT by SCAN over
    `trace:*`. SCAN would work at this scale and become a problem at any
    other, and reaching for it here is how someone later reaches for KEYS.
    """
    try:
        raw = redis_client.lrange("trace:index", -limit, -1)
    except Exception:
        return []
    return [i.decode() if isinstance(i, bytes) else i for i in reversed(raw)]


def register(redis_client, correlation_id: str, summary: str = "") -> None:
    """Add a correlation ID to the recent-traces index. Gateway only."""
    try:
        entry = f"{correlation_id}|{int(time.time())}|{summary}"
        pipe = redis_client.pipeline()
        pipe.rpush("trace:index", entry)
        pipe.ltrim("trace:index", -100, -1)
        pipe.expire("trace:index", TRACE_TTL_SECONDS)
        pipe.execute()
    except Exception:
        pass
